"""Compare STT accuracy: Gemini vs AssemblyAI vs Google Cloud STT.

Downloads audio once, sends to both services, and prints side-by-side
segment + timestamp results for manual accuracy evaluation.

Usage:
    python -m scripts.test_stt_comparison [youtube_id]
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────

YOUTUBE_ID = "4yD_pY8yhB8"
ASSEMBLYAI_API_KEY = "8052f365120b485bab2ee70759843db6"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import get_settings

settings = get_settings()
GEMINI_API_KEY = settings.GEMINI_API_KEY


# ─── Audio extraction (shared) ───────────────────────────────────────────────

def extract_audio(video_id: str, out_dir: Path) -> tuple[Path, int]:
    url = f"https://www.youtube.com/watch?v={video_id}"
    wav_path = out_dir / f"{video_id}.wav"

    cmd = [
        "yt-dlp", "--quiet", "--no-warnings",
        "-x", "--audio-format", "wav",
        "--postprocessor-args", "ffmpeg:-ac 1 -ar 16000",
        "-o", str(wav_path),
        url,
    ]

    print(f"[Audio] Downloading {url}...")
    t0 = time.time()
    subprocess.run(cmd, check=True, timeout=120)
    print(f"[Audio] Downloaded in {time.time() - t0:.1f}s")

    if not wav_path.exists():
        candidates = list(out_dir.glob(f"{video_id}.*"))
        if candidates:
            wav_path = candidates[0]
        else:
            raise RuntimeError("yt-dlp produced no output file")

    with wave.open(str(wav_path), "rb") as wf:
        duration = wf.getnframes() // wf.getframerate()

    size_mb = wav_path.stat().st_size / (1024 * 1024)
    print(f"[Audio] {wav_path.name} | {size_mb:.1f} MB | {duration}s")
    return wav_path, duration


# ─── Gemini STT ──────────────────────────────────────────────────────────────

def run_gemini_stt(wav_path: Path, language: str = "en") -> list[dict]:
    from google import genai
    from google.genai import types

    if not GEMINI_API_KEY:
        print("[Gemini] GEMINI_API_KEY not set, skipping")
        return []

    client = genai.Client(api_key=GEMINI_API_KEY)

    print("[Gemini] Uploading audio to File API...")
    t0 = time.time()
    audio_file = client.files.upload(file=str(wav_path))
    print(f"[Gemini] Upload: {time.time() - t0:.1f}s")

    while audio_file.state.name == "PROCESSING":
        time.sleep(2)
        audio_file = client.files.get(name=audio_file.name)

    if audio_file.state.name != "ACTIVE":
        raise RuntimeError(f"Gemini file state: {audio_file.state.name}")

    prompt = f"""Listen to this audio and transcribe every spoken word with its precise timestamp.

Language: {language}

Return a JSON object with two fields:

1. "words" — an array of every spoken word with timing:
   [{{"word": "Hello", "start": 0.5, "end": 0.9}}, ...]

2. "sentences" — the same words grouped into grammatically complete, punctuated sentences:
   ["Hello, how are you?", "I am fine."]

Rules for words:
- Include EVERY spoken word, in order
- Timestamps (seconds) must be precise and monotonically increasing
- Skip non-speech audio (music, silence, sound effects)

Rules for sentences:
- Each sentence = one complete thought with proper punctuation
- Prefer 5-12 words per sentence
- Never merge two independent clauses into one sentence
- If a passage repeats (e.g. dictation exercise), include both occurrences"""

    print(f"[Gemini] Requesting transcription from gemini-2.5-flash...")
    t0 = time.time()
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[audio_file, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
    finally:
        try:
            client.files.delete(name=audio_file.name)
        except Exception:
            pass

    elapsed = time.time() - t0
    print(f"[Gemini] Response in {elapsed:.1f}s")

    text = (response.text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[Gemini] JSON parse error: {e}")
        print(f"[Gemini] Raw: {text[:500]}")
        return []

    words = data.get("words", [])
    raw_sentences = data.get("sentences", [])

    # Gemini may return sentences as strings or dicts — normalize
    sentences = []
    for s in raw_sentences:
        if isinstance(s, str):
            sentences.append(s)
        elif isinstance(s, dict):
            sentences.append(s.get("text") or s.get("sentence") or str(s))
        else:
            sentences.append(str(s))

    # Map sentences to word timestamps
    segments = _map_sentences_to_words(sentences, words)
    return segments, words


def _normalize(w: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "", w.lower())


def _map_sentences_to_words(sentences: list[str], words: list[dict]) -> list[dict]:
    segments = []
    word_idx = 0

    for sentence in sentences:
        sent_words = [_normalize(w) for w in sentence.split() if _normalize(w)]
        if not sent_words or word_idx >= len(words):
            continue

        start_idx = word_idx
        matched = 0
        saved_idx = word_idx
        last_matched_idx = word_idx
        max_scan = len(sent_words) * 3

        while word_idx < len(words) and matched < len(sent_words) and (word_idx - start_idx) < max_scan:
            stt_norm = _normalize(words[word_idx]["word"])
            sent_norm = sent_words[matched]
            if stt_norm == sent_norm or stt_norm.startswith(sent_norm) or sent_norm.startswith(stt_norm):
                matched += 1
                last_matched_idx = word_idx
            word_idx += 1

        if matched < len(sent_words) * 0.5:
            word_idx = saved_idx
            continue

        end_idx = last_matched_idx
        word_idx = last_matched_idx + 1
        start = words[start_idx]["start"]
        end = words[end_idx]["end"]

        if end_idx + 1 < len(words):
            next_start = words[end_idx + 1]["start"]
            if end > next_start:
                end = next_start - 0.05
                end = max(end, start + 0.1)

        segments.append({
            "text": sentence,
            "start": start,
            "end": end,
            "duration": end - start,
        })

    return segments


# ─── AssemblyAI STT ──────────────────────────────────────────────────────────

def run_assemblyai_stt(wav_path: Path, language: str = "en") -> list[dict]:
    import assemblyai as aai

    if not ASSEMBLYAI_API_KEY:
        print("[AssemblyAI] API key not set, skipping")
        return []

    aai.settings.api_key = ASSEMBLYAI_API_KEY

    print(f"[AssemblyAI] Uploading and transcribing...")
    t0 = time.time()

    config = aai.TranscriptionConfig(
        language_code=language,
        punctuate=True,
        format_text=True,
    )
    config.speech_models = ["universal-3-pro"]

    transcriber = aai.Transcriber()
    transcript = transcriber.transcribe(str(wav_path), config=config)

    elapsed = time.time() - t0
    print(f"[AssemblyAI] Response in {elapsed:.1f}s")

    if transcript.status == aai.TranscriptStatus.error:
        print(f"[AssemblyAI] Error: {transcript.error}")
        return []

    segments = []
    sentences = transcript.get_sentences()
    if sentences:
        for sent in sentences:
            start_sec = sent.start / 1000.0
            end_sec = sent.end / 1000.0
            segments.append({
                "text": sent.text,
                "start": start_sec,
                "end": end_sec,
                "duration": end_sec - start_sec,
            })

    # Also collect word-level data for detailed comparison
    word_data = []
    if transcript.words:
        for w in transcript.words:
            word_data.append({
                "word": w.text,
                "start": w.start / 1000.0,
                "end": w.end / 1000.0,
            })

    return segments, word_data


# ─── Google Cloud STT ─────────────────────────────────────────────────────────

def run_google_cloud_stt(wav_path: Path, language: str = "en-US") -> tuple[list[dict], list[dict]]:
    creds_raw = settings.GOOGLE_APPLICATION_CREDENTIALS
    if not creds_raw:
        print("[Google-STT] GOOGLE_APPLICATION_CREDENTIALS not set, skipping")
        return [], []

    project_root = Path(__file__).resolve().parent.parent
    creds_path = creds_raw if os.path.isabs(creds_raw) else str(project_root / creds_raw)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path

    from google.cloud import speech, storage

    bucket_name = settings.GCS_BUCKET_NAME
    if not bucket_name:
        print("[Google-STT] GCS_BUCKET_NAME not set, skipping")
        return [], []

    # Upload to GCS for long_running_recognize
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    blob_name = f"audio-ytb/test-stt/{wav_path.name}"
    blob = bucket.blob(blob_name)

    print(f"[Google-STT] Uploading to gs://{bucket_name}/{blob_name}...")
    blob.upload_from_filename(str(wav_path))
    gcs_uri = f"gs://{bucket_name}/{blob_name}"

    speech_client = speech.SpeechClient()
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

    print(f"[Google-STT] Running long_running_recognize...")
    t0 = time.time()
    operation = speech_client.long_running_recognize(config=config, audio=audio)
    response = operation.result(timeout=600)
    elapsed = time.time() - t0
    print(f"[Google-STT] Response in {elapsed:.1f}s, {len(response.results)} results")

    words = []
    for result in response.results:
        alt = result.alternatives[0]
        for w in alt.words:
            words.append({
                "word": w.word,
                "start": w.start_time.total_seconds(),
                "end": w.end_time.total_seconds(),
            })

    import re
    punctuated_parts = []
    for result in response.results:
        alt = result.alternatives[0]
        if alt.transcript:
            punctuated_parts.append(alt.transcript.strip())
    punctuated_text = " ".join(punctuated_parts)

    sentences_raw = re.split(r'(?<=[.?!])\s+', punctuated_text.strip())
    sentences_raw = [s.strip() for s in sentences_raw if s.strip()]

    segments = _map_sentences_to_words(sentences_raw, words)
    return segments, words


# ─── Display ──────────────────────────────────────────────────────────────────

def print_segments(label: str, segments: list[dict]):
    print(f"\n{'=' * 80}")
    print(f"  {label} — {len(segments)} segments")
    print(f"{'=' * 80}")
    for i, seg in enumerate(segments):
        print(
            f"  {i + 1:2d}. [{seg['start']:6.2f} → {seg['end']:6.2f}] "
            f"({seg['duration']:5.2f}s) | {seg['text']}"
        )


def print_words(label: str, words: list[dict], max_show: int = 50):
    print(f"\n{'─' * 80}")
    print(f"  {label} — Word-level timestamps ({len(words)} words, showing first {max_show})")
    print(f"{'─' * 80}")
    for i, w in enumerate(words[:max_show]):
        print(f"    [{w['start']:6.2f} → {w['end']:6.2f}] {w['word']}")
    if len(words) > max_show:
        print(f"    ... and {len(words) - max_show} more words")


def compare_summary(results: dict):
    print(f"\n{'#' * 80}")
    print(f"  COMPARISON SUMMARY")
    print(f"{'#' * 80}")

    for name, data in results.items():
        segs = data.get("segments", [])
        words = data.get("words", [])
        elapsed = data.get("elapsed", 0)
        if not segs:
            print(f"\n  {name}: NO RESULTS")
            continue

        total_text = " ".join(s["text"] for s in segs)
        word_count = len(total_text.split())
        avg_dur = sum(s["duration"] for s in segs) / len(segs) if segs else 0
        coverage = sum(s["duration"] for s in segs)

        print(f"\n  {name}:")
        print(f"    Segments:     {len(segs)}")
        print(f"    Words:        {word_count} (word-level: {len(words)})")
        print(f"    Avg duration: {avg_dur:.2f}s per segment")
        print(f"    Coverage:     {coverage:.1f}s of speech")
        print(f"    Processing:   {elapsed:.1f}s")

        # Check for overlaps
        overlaps = 0
        for i in range(len(segs) - 1):
            if segs[i]["end"] > segs[i + 1]["start"] + 0.01:
                overlaps += 1
        if overlaps:
            print(f"    ⚠ Overlaps:   {overlaps}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    video_id = sys.argv[1] if len(sys.argv) > 1 else YOUTUBE_ID
    print(f"\n{'#' * 80}")
    print(f"  STT Accuracy Comparison — youtube.com/watch?v={video_id}")
    print(f"{'#' * 80}")

    results = {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        wav_path, duration = extract_audio(video_id, tmp_path)
        print(f"[Audio] Duration: {duration}s")

        # ── Gemini STT ──
        print(f"\n{'━' * 80}")
        print("  TEST 1: Gemini 2.5 Flash (current pipeline)")
        print(f"{'━' * 80}")
        try:
            t0 = time.time()
            gemini_result = run_gemini_stt(wav_path, "en")
            gemini_elapsed = time.time() - t0
            if isinstance(gemini_result, tuple):
                gemini_segments, gemini_words = gemini_result
            else:
                gemini_segments, gemini_words = gemini_result, []
            print_segments("Gemini 2.5 Flash", gemini_segments)
            print_words("Gemini 2.5 Flash", gemini_words)
            results["Gemini 2.5 Flash"] = {
                "segments": gemini_segments,
                "words": gemini_words,
                "elapsed": gemini_elapsed,
            }
        except Exception as e:
            print(f"[Gemini] FAILED: {e}")
            import traceback; traceback.print_exc()

        # ── AssemblyAI STT ──
        print(f"\n{'━' * 80}")
        print("  TEST 2: AssemblyAI")
        print(f"{'━' * 80}")
        try:
            t0 = time.time()
            aai_result = run_assemblyai_stt(wav_path, "en")
            aai_elapsed = time.time() - t0
            if isinstance(aai_result, tuple):
                aai_segments, aai_words = aai_result
            else:
                aai_segments, aai_words = aai_result, []
            print_segments("AssemblyAI", aai_segments)
            print_words("AssemblyAI", aai_words)
            results["AssemblyAI"] = {
                "segments": aai_segments,
                "words": aai_words,
                "elapsed": aai_elapsed,
            }
        except Exception as e:
            print(f"[AssemblyAI] FAILED: {e}")
            import traceback; traceback.print_exc()

        # ── Google Cloud STT ──
        print(f"\n{'━' * 80}")
        print("  TEST 3: Google Cloud STT")
        print(f"{'━' * 80}")
        try:
            t0 = time.time()
            gcs_result = run_google_cloud_stt(wav_path, "en-US")
            gcs_elapsed = time.time() - t0
            if isinstance(gcs_result, tuple):
                gcs_segments, gcs_words = gcs_result
            else:
                gcs_segments, gcs_words = gcs_result, []
            if gcs_segments:
                print_segments("Google Cloud STT", gcs_segments)
                print_words("Google Cloud STT", gcs_words)
                results["Google Cloud STT"] = {
                    "segments": gcs_segments,
                    "words": gcs_words,
                    "elapsed": gcs_elapsed,
                }
            else:
                print("[Google-STT] No results (may need GCS for long audio)")
        except Exception as e:
            print(f"[Google-STT] FAILED: {e}")
            import traceback; traceback.print_exc()

    compare_summary(results)


if __name__ == "__main__":
    main()