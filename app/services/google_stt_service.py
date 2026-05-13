"""Google Cloud Speech-to-Text service.

Extracts audio from YouTube via yt-dlp, uploads to GCS,
runs long_running_recognize for word-level timestamps,
then uses Gemini to split into proper sentences and maps
timestamps back onto each sentence.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from google.cloud import speech, storage

from app.config import get_settings
from app.services.youtube_service import TranscriptSegment

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
_creds_set = False


def _ensure_credentials():
    global _creds_set
    if _creds_set:
        return
    settings = get_settings()
    raw = settings.GOOGLE_APPLICATION_CREDENTIALS
    if raw:
        path = raw if os.path.isabs(raw) else os.path.join(_PROJECT_ROOT, raw)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path
        logger.info("[STT] Set GOOGLE_APPLICATION_CREDENTIALS=%s", path)
    _creds_set = True

MAX_STT_DURATION = 300  # 5 minutes

_GEMINI_MODEL = "gemini-2.5-flash"

_SENTENCE_SPLIT_PROMPT = """You are an expert English language teacher specialized in transcript analysis and grammar correction.

Your task: Given a raw transcript (no punctuation), add PROPER PUNCTUATION and split it
into GRAMMATICALLY CORRECT, NATURAL-SOUNDING sentences.

CRITICAL RULES - DO NOT VIOLATE:

1. **PRESERVE ALL WORDS**:
   - Keep every single word exactly as spoken
   - Only add punctuation marks
   - Never change, remove, or add words

2. **SENTENCE BOUNDARIES** - When to split:
   - Period (.) = Statement ends -> new sentence
   - Question mark (?) = Question ends -> new sentence
   - Exclamation mark (!) = Emphasis -> new sentence
   - "And" connecting TWO INDEPENDENT CLAUSES -> consider splitting
   - "But" = contrast -> usually new sentence
   - "Because", "since" = reason -> keep in SAME sentence with comma
   - "Although", "though" = concession -> keep in SAME sentence with comma

3. **REPETITION HANDLING**:
   - If same word/phrase repeats = DEFINITE sentence boundary
   - First occurrence = intro/topic, second = explanation/detail

4. **TIME & PLACE ADVERBIALS** signal new focus:
   "Each week", "Every day", "When I" -> often new sentence

5. **CLAUSE STRUCTURE**:
   - Independent + dependent clause -> SAME sentence with comma
   - Two independent clauses -> DIFFERENT sentences
   - Subject change -> NEW sentence

6. **QUALITY CHECKS** - Each sentence must:
   - Have a clear subject and verb
   - Express one complete idea
   - No fragments, no run-on sentences

7. **OUTPUT FORMAT**:
   - Return ONLY valid JSON array of strings
   - Each string = one complete sentence with proper punctuation
   - No markdown code blocks, no explanations

Raw transcript:
"{raw_text}"
"""


def _extract_audio(video_id: str, out_dir: Path) -> tuple[Path, int]:
    """Download audio from YouTube as mono 16kHz WAV via yt-dlp + ffmpeg."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    wav_path = out_dir / f"{video_id}.wav"

    cmd = [
        "yt-dlp",
        "--quiet", "--no-warnings",
        "-x",
        "--audio-format", "wav",
        "--postprocessor-args", "ffmpeg:-ac 1 -ar 16000",
        "-o", str(wav_path),
        url,
    ]

    logger.info("[STT] Extracting audio for %s → %s", video_id, wav_path)
    t0 = time.time()
    subprocess.run(cmd, check=True, timeout=120)
    logger.info("[STT] yt-dlp completed in %.1fs", time.time() - t0)

    if not wav_path.exists():
        candidates = list(out_dir.glob(f"{video_id}.*"))
        logger.warning("[STT] Expected %s not found, candidates: %s", wav_path.name, candidates)
        if candidates:
            wav_path = candidates[0]
        else:
            raise RuntimeError(f"yt-dlp produced no output file for {video_id}")

    file_size_mb = wav_path.stat().st_size / (1024 * 1024)

    import wave
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        channels = wf.getnchannels()
        duration = frames // rate

    logger.info(
        "[STT] Audio extracted: %s | %.1f MB | %ds | %dHz | %dch",
        wav_path.name, file_size_mb, duration, rate, channels,
    )
    return wav_path, duration


def _find_existing_gcs(video_id: str, bucket_name: str) -> str | None:
    """Check if audio for this video already exists in GCS."""
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    prefix = f"audio-ytb/{video_id}/"
    blobs = list(bucket.list_blobs(prefix=prefix, max_results=1))
    if blobs:
        uri = f"gs://{bucket_name}/{blobs[0].name}"
        logger.info("[STT] Found existing audio in GCS: %s", uri)
        return uri
    return None


def _upload_to_gcs(local_path: Path, bucket_name: str, video_id: str) -> str:
    """Upload a file to GCS and return the gs:// URI."""
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob_name = f"audio-ytb/{video_id}/{local_path.name}"
    blob = bucket.blob(blob_name)

    file_size_mb = local_path.stat().st_size / (1024 * 1024)
    logger.info("[STT] Uploading %.1f MB to gs://%s/%s", file_size_mb, bucket_name, blob_name)
    t0 = time.time()
    blob.upload_from_filename(str(local_path))
    uri = f"gs://{bucket_name}/{blob_name}"
    logger.info("[STT] Upload completed in %.1fs → %s", time.time() - t0, uri)
    return uri


# ── Step 1: STT → word-level timestamps + punctuated transcript ───────────

def _get_words_from_stt(gcs_uri: str, language: str) -> tuple[list[dict], str]:
    """Run long_running_recognize and return (word timestamps, punctuated transcript).

    Enables both word_time_offsets and automatic_punctuation in a single STT
    call so the fallback path can split by punctuation without a second call.
    """
    client = speech.SpeechClient()

    audio = speech.RecognitionAudio(uri=gcs_uri)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code=language,
        enable_automatic_punctuation=True,
        enable_word_time_offsets=True,
        audio_channel_count=1,
        model="latest_long",
        use_enhanced=True,
    )

    logger.info("[STT] Starting long_running_recognize (lang=%s, uri=%s)", language, gcs_uri)
    t0 = time.time()
    operation = client.long_running_recognize(config=config, audio=audio)
    response = operation.result(timeout=600)
    elapsed = time.time() - t0
    logger.info("[STT] STT completed in %.1fs, %d results", elapsed, len(response.results))

    words: list[dict] = []
    punctuated_parts: list[str] = []

    for result in response.results:
        alt = result.alternatives[0]
        if alt.transcript:
            punctuated_parts.append(alt.transcript.strip())
        for w in alt.words:
            words.append({
                "word": w.word,
                "start": w.start_time.total_seconds(),
                "end": w.end_time.total_seconds(),
            })

    punctuated_text = " ".join(punctuated_parts)

    logger.info("[STT] Extracted %d words with timestamps, punctuated text %d chars",
                len(words), len(punctuated_text))
    if words:
        logger.info(
            "[STT] First word: [%.2f→%.2f] %s | Last word: [%.2f→%.2f] %s",
            words[0]["start"], words[0]["end"], words[0]["word"],
            words[-1]["start"], words[-1]["end"], words[-1]["word"],
        )

    return words, punctuated_text


# ── Step 2: Gemini → sentence splitting ──────────────────────────────────

def _split_sentences_with_gemini(raw_text: str) -> list[str]:
    """Use Gemini to split raw transcript into punctuated sentences."""
    from google import genai

    settings = get_settings()
    if not settings.GEMINI_API_KEY:
        logger.warning("[STT] GEMINI_API_KEY not configured, falling back to STT punctuation")
        return []

    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    prompt = _SENTENCE_SPLIT_PROMPT.replace("{raw_text}", raw_text)

    logger.info("[STT] Sending %d chars to Gemini (%s) for sentence splitting", len(raw_text), _GEMINI_MODEL)
    t0 = time.time()

    try:
        response = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=prompt,
        )
    except Exception as e:
        logger.warning("[STT] Gemini API error, falling back to STT punctuation: %s", e)
        return []

    response_text = (response.text or "").strip()
    elapsed = time.time() - t0
    logger.info("[STT] Gemini responded in %.1fs (%d chars)", elapsed, len(response_text))

    if response_text.startswith("```"):
        response_text = response_text.split("\n", 1)[1]
        response_text = response_text.rsplit("```", 1)[0].strip()

    try:
        sentences = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error("[STT] Gemini returned invalid JSON: %s | Response: %s", e, response_text[:200])
        return []

    logger.info("[STT] Gemini split transcript into %d sentences", len(sentences))
    return sentences


# ── Step 3: Map sentences onto word timestamps ──────────────────────────

def _normalize_word(w: str) -> str:
    """Strip punctuation and normalize for matching."""
    return re.sub(r"[^a-z0-9]", "", w.lower())


def _map_sentences_to_words(
    sentences: list[str],
    words: list[dict],
) -> list[TranscriptSegment]:
    """Map Gemini sentences back onto STT word timestamps.

    Uses a scan window to prevent one sentence from consuming all remaining
    STT words when a word mismatch causes the pointer to overshoot.
    """
    segments: list[TranscriptSegment] = []
    word_idx = 0

    for sent_num, sentence in enumerate(sentences):
        sentence_words = [_normalize_word(w) for w in sentence.split() if _normalize_word(w)]

        if not sentence_words or word_idx >= len(words):
            continue

        start_idx = word_idx
        matched = 0
        saved_idx = word_idx
        max_scan = len(sentence_words) * 3

        while word_idx < len(words) and matched < len(sentence_words) and (word_idx - start_idx) < max_scan:
            stt_norm = _normalize_word(words[word_idx]["word"])
            sent_norm = sentence_words[matched]
            if stt_norm == sent_norm or stt_norm.startswith(sent_norm) or sent_norm.startswith(stt_norm):
                matched += 1
            word_idx += 1

        if matched < len(sentence_words) * 0.5:
            logger.warning("[STT] Sentence %d: poor match %d/%d, skipping: %s", sent_num + 1, matched, len(sentence_words), sentence[:60])
            word_idx = saved_idx
            continue

        if matched < len(sentence_words):
            logger.warning(
                "[STT] Sentence %d: partial match %d/%d words: %s",
                sent_num + 1, matched, len(sentence_words), sentence[:60],
            )

        end_idx = word_idx - 1
        start = words[start_idx]["start"]
        end = words[end_idx]["end"]

        segments.append(TranscriptSegment(
            text=sentence,
            start=start,
            duration=end - start,
        ))

        logger.info(
            "[STT] Sentence %2d | %6.2f → %6.2f (%5.2fs) | %d words | %s",
            sent_num + 1, start, end, end - start, matched,
            sentence[:80] + ("…" if len(sentence) > 80 else ""),
        )

    logger.info("[STT] Mapped %d/%d sentences to timestamps", len(segments), len(sentences))
    return segments


# ── Fallback: split by STT auto-punctuation ────────────────────────────

def _split_by_stt_punctuation(
    punctuated_text: str,
    words: list[dict],
) -> list[TranscriptSegment]:
    """Split STT-punctuated transcript into sentences at .?! boundaries,
    then align each sentence onto the word-level timestamps."""
    if not punctuated_text or not words:
        return []

    sentences = re.split(r'(?<=[.?!])\s+', punctuated_text.strip())
    sentences = [s.strip() for s in sentences if s.strip()]

    if not sentences:
        return []

    segments: list[TranscriptSegment] = []
    word_idx = 0

    for sent_num, sentence in enumerate(sentences):
        sent_words = [_normalize_word(w) for w in sentence.split() if _normalize_word(w)]
        if not sent_words or word_idx >= len(words):
            continue

        start_idx = word_idx
        matched = 0
        max_scan = len(sent_words) * 3

        while word_idx < len(words) and matched < len(sent_words) and (word_idx - start_idx) < max_scan:
            stt_norm = _normalize_word(words[word_idx]["word"])
            target = sent_words[matched]
            if stt_norm == target or stt_norm.startswith(target) or target.startswith(stt_norm):
                matched += 1
            word_idx += 1

        if matched == 0:
            word_idx = start_idx + 1
            continue

        end_idx = word_idx - 1
        start = words[start_idx]["start"]
        end = words[end_idx]["end"]

        segments.append(TranscriptSegment(
            text=sentence,
            start=start,
            duration=end - start,
        ))

        logger.info(
            "[STT] Align %2d | %6.2f → %6.2f (%5.2fs) | matched %d/%d | %s",
            sent_num + 1, start, end, end - start, matched, len(sent_words),
            sentence[:80] + ("…" if len(sentence) > 80 else ""),
        )

    logger.info("[STT] Fallback produced %d segments from punctuated text", len(segments))
    return segments


# ── Post-processing: merge short fragments ─────────────────────────────

MIN_WORDS = 5
MAX_MERGED_DURATION = 15.0


def _merge_short_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """Merge consecutive short segments until each has at least MIN_WORDS.

    Runs multiple passes so chains of fragments (e.g. "Easy." + "Learning
    English." + "Daily listening.") all collapse into one segment.
    """
    if len(segments) <= 1:
        return segments

    changed = True
    result = list(segments)

    while changed:
        changed = False
        merged: list[TranscriptSegment] = []
        i = 0

        while i < len(result):
            cur = result[i]
            cur_words = len(cur.text.split())

            # Short segment: try merging with the next one
            if cur_words < MIN_WORDS and i + 1 < len(result):
                nxt = result[i + 1]
                combined_end = nxt.start + nxt.duration
                combined_dur = combined_end - cur.start

                if combined_dur <= MAX_MERGED_DURATION:
                    cur_text = cur.text.rstrip(" .,?!") + " " + nxt.text
                    merged.append(TranscriptSegment(
                        text=cur_text,
                        start=cur.start,
                        duration=combined_dur,
                    ))
                    i += 2
                    changed = True
                    continue

            merged.append(cur)
            i += 1

        # Trailing short fragment: merge backward
        if len(merged) >= 2 and len(merged[-1].text.split()) < MIN_WORDS:
            last = merged.pop()
            prev = merged[-1]
            combined_end = last.start + last.duration
            combined_dur = combined_end - prev.start
            if combined_dur <= MAX_MERGED_DURATION:
                merged[-1] = TranscriptSegment(
                    text=prev.text.rstrip(" .,?!") + " " + last.text,
                    start=prev.start,
                    duration=combined_dur,
                )
                changed = True
            else:
                merged.append(last)

        result = merged

    if len(result) != len(segments):
        logger.info("[STT] Merged short fragments: %d → %d segments", len(segments), len(result))
        for i, seg in enumerate(result):
            logger.info(
                "[STT] Merged %2d | %6.2f → %6.2f (%5.2fs) | %d words | %s",
                i + 1, seg.start, seg.start + seg.duration, seg.duration,
                len(seg.text.split()),
                seg.text[:80] + ("…" if len(seg.text) > 80 else ""),
            )

    return result


# ── Public API ───────────────────────────────────────────────────────────

def transcribe_youtube_video(
    video_id: str,
    language: str = "en-US",
    video_duration: int = 0,
) -> list[TranscriptSegment]:
    """Full pipeline: extract audio → GCS → STT words → Gemini sentences → timestamps."""
    from app.core.exceptions import BadRequestError

    _ensure_credentials()

    logger.info("[STT] === Starting STT pipeline for %s (reported duration=%ds) ===", video_id, video_duration)

    if video_duration > MAX_STT_DURATION:
        raise BadRequestError(
            f"AI transcription is limited to {MAX_STT_DURATION // 60} minutes. "
            f"This video is {video_duration // 60}m {video_duration % 60}s. "
            f"Please choose a shorter video or one with manual subtitles."
        )

    settings = get_settings()
    if not settings.GCS_BUCKET_NAME:
        raise BadRequestError("Google Cloud Storage is not configured (GCS_BUCKET_NAME missing).")

    logger.info("[STT] Config: bucket=%s, project=%s", settings.GCS_BUCKET_NAME, settings.GCP_PROJECT_ID)

    pipeline_t0 = time.time()

    # Skip extract + upload if audio already exists in GCS
    gcs_uri = _find_existing_gcs(video_id, settings.GCS_BUCKET_NAME)
    if gcs_uri:
        logger.info("[STT] Reusing existing audio, skipping download + upload")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wav_path, actual_duration = _extract_audio(video_id, tmp_path)

            if actual_duration > MAX_STT_DURATION:
                raise BadRequestError(
                    f"AI transcription is limited to {MAX_STT_DURATION // 60} minutes. "
                    f"Detected audio duration is {actual_duration // 60}m {actual_duration % 60}s. "
                    f"Please choose a shorter video or one with manual subtitles."
                )

            gcs_uri = _upload_to_gcs(wav_path, settings.GCS_BUCKET_NAME, video_id)

    # Step 1: Single STT call — words + punctuated transcript
    words, punctuated_text = _get_words_from_stt(gcs_uri, language)

    if not words:
        logger.warning("[STT] No words from STT, returning empty")
        return []

    # Step 2: Try Gemini for best sentence splitting
    raw_text = " ".join(w["word"] for w in words)
    sentences = _split_sentences_with_gemini(raw_text)

    # Step 3: Map sentences to word timestamps
    if sentences:
        segments = _map_sentences_to_words(sentences, words)
    else:
        # Fallback: split using STT auto-punctuation (no second API call)
        logger.info("[STT] Using STT auto-punctuation fallback (no Gemini)")
        segments = _split_by_stt_punctuation(punctuated_text, words)

    # Step 4: Merge short fragments ("Easy." + "Learning English." → one segment)
    segments = _merge_short_segments(segments)

    logger.info(
        "[STT] === Pipeline complete for %s: %d segments in %.1fs (audio at %s) ===",
        video_id, len(segments), time.time() - pipeline_t0, gcs_uri,
    )
    return segments
