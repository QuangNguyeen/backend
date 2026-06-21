"""AssemblyAI Speech-to-Text service."""

import logging
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import assemblyai as aai

from app.config import get_settings
from app.services.stt_audio_service import (
    STT_MAX_DURATION_SECONDS,
    VideoUnavailableError,
    download_native_audio,
    enforce_non_overlapping,
    extract_wav_audio,
    get_direct_audio_url,
)
from app.services.youtube_service import (
    SEGMENT_LIMITS,
    SPLIT_MARKERS,
    TranscriptSegment,
    merge_segments_smart,
    process_transcript_segments,
)

logger = logging.getLogger(__name__)

_BASE_PROMPT = (
    "Transcribe the audio verbatim and with maximum accuracy. Capture exactly "
    "what is spoken, word for word, without omitting, adding, paraphrasing, or "
    "translating any words. Spell proper nouns, personal names, brands, places, "
    "and technical or domain-specific terms correctly. Render numbers, dates, "
    "currencies, and acronyms the natural way they are spoken, and keep "
    "contractions as pronounced. Apply correct punctuation, capitalization, and "
    "sentence boundaries. When a passage is unclear, choose the most plausible "
    "real word from context instead of guessing randomly or inventing content. "
    "Produce precise, tightly aligned timestamps so the start and end of every "
    "word and sentence match the exact moment it is spoken in the audio, and do "
    "not pad silence into adjacent segments."
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'_-]{2,}")
_STRONG_BOUNDARY_RE = re.compile(r"[.!?;:][\"')\]]*$")
_SOFT_BOUNDARY_RE = re.compile(r"[,][\"')\]]*$")
_SEGMENT_TAIL_GUARD_SECONDS = 0.18


@dataclass(frozen=True)
class _TimedWord:
    text: str
    start: float
    end: float


def _is_language_known(language: str | None) -> bool:
    return bool(language and language.strip().lower() not in {"auto", "und", "unknown"})


_MAX_CAPTION_PROMPT_CHARS = 2000


def _build_prompt(prompt_context: dict[str, str | None] | None) -> str:
    if not prompt_context:
        return _BASE_PROMPT

    parts = [
        _BASE_PROMPT,
        "Use the following context only to spell names and terminology "
        "correctly; do not copy it verbatim and transcribe only what is "
        "actually spoken:",
    ]
    if prompt_context.get("title"):
        parts.append(f"Title: {prompt_context['title']}")
    if prompt_context.get("channel"):
        parts.append(f"Channel: {prompt_context['channel']}")
    if prompt_context.get("captions"):
        captions = prompt_context["captions"].strip()
        if len(captions) > _MAX_CAPTION_PROMPT_CHARS:
            captions = captions[:_MAX_CAPTION_PROMPT_CHARS].rsplit(" ", 1)[0] + " …"
        parts.append(f"Reference auto-caption text (may contain errors): {captions}")
    return "\n".join(parts)


def _keyterms_from_context(prompt_context: dict[str, str | None] | None) -> list[str] | None:
    if not prompt_context:
        return None

    terms: list[str] = []
    seen: set[str] = set()
    text = " ".join(v for v in prompt_context.values() if v)
    for match in _WORD_RE.finditer(text):
        word = match.group(0).strip("'_-")
        if len(word) < 4:
            continue
        looks_distinctive = word[0].isupper() or word.isupper() or len(word) >= 7
        if not looks_distinctive:
            continue
        key = word.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(word)
        if len(terms) >= 100:
            break
    return terms or None


def _build_config(
    language: str,
    prompt_context: dict[str, str | None] | None,
) -> aai.TranscriptionConfig:
    config_kwargs = {
        "punctuate": True,
        "format_text": True,
        "speech_models": ["universal-3-pro", "universal-2"],
        "prompt": _build_prompt(prompt_context),
        "keyterms_prompt": _keyterms_from_context(prompt_context),
    }
    if _is_language_known(language):
        config_kwargs["language_code"] = language
    else:
        config_kwargs["language_detection"] = True
    return aai.TranscriptionConfig(**config_kwargs)


def _raise_if_failed(transcript, source: str) -> None:
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI transcription failed from {source}: {transcript.error}")


def _transcribe_local_file(transcriber, path, config):
    logger.info("[AssemblyAI] Uploading and transcribing %s...", path.name)
    return transcriber.transcribe(str(path), config=config)


def _field(item, name: str, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _seconds_from_ms(value) -> float | None:
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _sentence_text(sentence) -> str:
    return str(_field(sentence, "text", "") or "").strip()


def _timed_words_from_items(raw_words, display_text: str | None = None) -> list[_TimedWord]:
    raw_words = list(raw_words or [])
    if not raw_words:
        return []

    display_tokens = (display_text or "").split()
    use_display_tokens = len(display_tokens) == len(raw_words)
    timed_words: list[_TimedWord] = []

    for index, raw_word in enumerate(raw_words):
        text = display_tokens[index] if use_display_tokens else str(_field(raw_word, "text", ""))
        text = text.strip()
        start = _seconds_from_ms(_field(raw_word, "start"))
        end = _seconds_from_ms(_field(raw_word, "end"))
        if not text or start is None or end is None:
            continue
        start = max(0.0, start)
        end = max(start + 0.02, end)
        timed_words.append(_TimedWord(text=text, start=start, end=end))

    return timed_words


def _timed_words_from_sentence(sentence) -> list[_TimedWord]:
    return _timed_words_from_items(
        _field(sentence, "words", None),
        _sentence_text(sentence),
    )


def _timed_words_from_transcript(transcript) -> list[_TimedWord]:
    try:
        raw_words = _field(transcript, "words", None)
    except ValueError:
        raw_words = None
    if not raw_words:
        return []

    try:
        display_text = _field(transcript, "text", "") or ""
    except ValueError:
        display_text = ""

    words = _timed_words_from_items(raw_words, str(display_text))
    return sorted(words, key=lambda word: (word.start, word.end))


def _boundary_score(words: list[_TimedWord], split_index: int) -> int | None:
    previous = words[split_index - 1].text.rstrip()
    current = words[split_index].text.lower().strip(".,!?;:\"'()[]")

    if _STRONG_BOUNDARY_RE.search(previous):
        return 0
    if _SOFT_BOUNDARY_RE.search(previous):
        return 1
    if current in SPLIT_MARKERS:
        return 2
    return None


def _choose_word_split(
    words: list[_TimedWord],
    *,
    start: int,
    max_segment_duration: float,
) -> int:
    config = SEGMENT_LIMITS["dictation"]
    total = len(words)
    remaining = total - start
    if remaining <= config.hard_max_words:
        duration = words[-1].end - words[start].start
        if duration <= max_segment_duration:
            return total

    min_end = min(total, start + max(1, config.min_words))
    preferred_end = min(total, start + config.preferred_max_words)
    hard_end = min(total, start + config.hard_max_words)

    while (
        hard_end > min_end
        and words[hard_end - 1].end - words[start].start > max_segment_duration
    ):
        hard_end -= 1
    hard_end = max(min_end, hard_end)

    candidates: list[tuple[int, int]] = []
    for split_index in range(min_end, hard_end + 1):
        score = _boundary_score(words, split_index) if split_index < total else 0
        if score is not None:
            candidates.append((score, split_index))

    if candidates:
        return min(candidates, key=lambda item: (item[0], abs(item[1] - preferred_end), -item[1]))[
            1
        ]
    return min(preferred_end, hard_end)


def _segment_from_words(words: list[_TimedWord], start: int, end: int) -> TranscriptSegment:
    chunk = words[start:end]
    text = " ".join(word.text for word in chunk).strip()
    segment_start = chunk[0].start
    segment_end = chunk[-1].end
    return TranscriptSegment(
        text=text,
        start=segment_start,
        duration=max(0.05, segment_end - segment_start),
    )


def _word_aligned_segments(
    sentences,
    *,
    max_segment_duration: float,
) -> tuple[list[TranscriptSegment], bool]:
    segments: list[TranscriptSegment] = []
    used_word_timestamps = False

    for sentence in sentences:
        words = _timed_words_from_sentence(sentence)
        if not words:
            start_sec = _seconds_from_ms(_field(sentence, "start")) or 0.0
            end_sec = _seconds_from_ms(_field(sentence, "end")) or start_sec
            segments.append(
                TranscriptSegment(
                    text=_sentence_text(sentence),
                    start=max(0.0, start_sec),
                    duration=max(0.05, end_sec - start_sec),
                )
            )
            continue

        used_word_timestamps = True
        start = 0
        while start < len(words):
            end = _choose_word_split(
                words,
                start=start,
                max_segment_duration=max(0.5, max_segment_duration),
            )
            if end <= start:
                end = min(len(words), start + 1)
            segments.append(_segment_from_words(words, start, end))
            start = end

    return segments, used_word_timestamps


def _word_stream_segments(
    words: list[_TimedWord],
    *,
    max_segment_duration: float,
) -> list[TranscriptSegment]:
    if not words:
        return []

    segments: list[TranscriptSegment] = []
    start = 0
    while start < len(words):
        end = _choose_word_split(
            words,
            start=start,
            max_segment_duration=max(0.5, max_segment_duration),
        )
        if end <= start:
            end = min(len(words), start + 1)
        segments.append(_segment_from_words(words, start, end))
        start = end
    return segments


def transcribe_with_assemblyai(
    video_id: str,
    language: str = "en",
    video_duration: int = 0,
    max_segment_duration: float = 10.0,
    prompt_context: dict[str, str | None] | None = None,
) -> list[TranscriptSegment]:
    """Transcribe YouTube audio using AssemblyAI.

    Pipeline: direct YouTube audio URL -> native file upload fallback -> WAV fallback
    -> sentence-level segments with precise timestamps.
    """
    from app.core.exceptions import BadRequestError

    settings = get_settings()
    if not settings.ASSEMBLYAI_API_KEY:
        raise BadRequestError("ASSEMBLYAI_API_KEY not configured for transcription.")

    if video_duration > STT_MAX_DURATION_SECONDS:
        raise BadRequestError(
            f"AI transcription is limited to {STT_MAX_DURATION_SECONDS // 60} minutes. "
            f"This video is {video_duration // 60}m {video_duration % 60}s. "
            f"Please choose a shorter video or one with manual subtitles."
        )

    aai.settings.api_key = settings.ASSEMBLYAI_API_KEY
    config = _build_config(language, prompt_context)
    transcriber = aai.Transcriber()
    pipeline_t0 = time.time()
    logger.info(
        "[AssemblyAI] === Starting pipeline for %s (duration=%ds) ===", video_id, video_duration
    )

    try:
        audio_url = get_direct_audio_url(video_id)
        logger.info("[AssemblyAI] Transcribing direct YouTube audio URL for %s", video_id)
        t0 = time.time()
        transcript = transcriber.transcribe(audio_url, config=config)
        _raise_if_failed(transcript, "direct URL")
        logger.info("[AssemblyAI] Direct URL transcription completed in %.1fs", time.time() - t0)
    except VideoUnavailableError:
        raise
    except Exception as direct_error:
        logger.warning(
            "[AssemblyAI] Direct URL transcription failed for %s, falling back to upload: %s",
            video_id,
            direct_error,
        )
        with tempfile.TemporaryDirectory() as tmp:
            audio_dir = Path(tmp)
            try:
                native_audio = download_native_audio(video_id, audio_dir)
                if native_audio.duration_seconds > STT_MAX_DURATION_SECONDS:
                    raise BadRequestError(
                        f"AI transcription is limited to {STT_MAX_DURATION_SECONDS // 60} minutes. "
                        f"Detected audio is {native_audio.duration_seconds // 60}m "
                        f"{native_audio.duration_seconds % 60}s. "
                        f"Please choose a shorter video or one with manual subtitles."
                    )
                t0 = time.time()
                transcript = _transcribe_local_file(transcriber, native_audio.path, config)
                _raise_if_failed(transcript, "native audio upload")
                logger.info(
                    "[AssemblyAI] Native audio transcription completed in %.1fs",
                    time.time() - t0,
                )
            except (BadRequestError, VideoUnavailableError):
                raise
            except Exception as native_error:
                logger.warning(
                    "[AssemblyAI] Native audio upload failed for %s, falling back to WAV: %s",
                    video_id,
                    native_error,
                )
                wav_audio = extract_wav_audio(video_id, audio_dir)
                if wav_audio.duration_seconds > STT_MAX_DURATION_SECONDS:
                    raise BadRequestError(
                        f"AI transcription is limited to {STT_MAX_DURATION_SECONDS // 60} minutes. "
                        f"Detected audio is {wav_audio.duration_seconds // 60}m "
                        f"{wav_audio.duration_seconds % 60}s. "
                        f"Please choose a shorter video or one with manual subtitles."
                    )
                t0 = time.time()
                transcript = _transcribe_local_file(transcriber, wav_audio.path, config)
                _raise_if_failed(transcript, "WAV fallback")
                logger.info(
                    "[AssemblyAI] WAV fallback transcription completed in %.1fs",
                    time.time() - t0,
                )

    transcript_words = _timed_words_from_transcript(transcript)
    if transcript_words:
        segments = _word_stream_segments(
            transcript_words,
            max_segment_duration=max_segment_duration,
        )
        used_word_timestamps = True
        logger.info(
            "[AssemblyAI] Got %d top-level words from transcript; using word stream alignment",
            len(transcript_words),
        )
    else:
        sentences = transcript.get_sentences()
        if not sentences:
            logger.warning("[AssemblyAI] No sentences returned for %s", video_id)
            return []

        segments, used_word_timestamps = _word_aligned_segments(
            sentences,
            max_segment_duration=max_segment_duration,
        )
        logger.info(
            "[AssemblyAI] Got %d sentences from transcript; word-level timing=%s",
            len(sentences),
            used_word_timestamps,
        )

    if not segments:
        logger.warning("[AssemblyAI] No usable transcript segments returned for %s", video_id)
        return []

    if not used_word_timestamps:
        segments = process_transcript_segments(segments, mode="dictation")
        segments = merge_segments_smart(
            segments,
            max_duration=max_segment_duration,
            mode="dictation",
        )

    segments = enforce_non_overlapping(
        segments,
        min_gap_seconds=_SEGMENT_TAIL_GUARD_SECONDS,
    )

    for i, segment in enumerate(segments):
        logger.info(
            "[AssemblyAI] Final segment %2d | %6.2f -> %6.2f (%5.2fs) | %s",
            i + 1,
            segment.start,
            segment.end,
            segment.duration,
            segment.text[:80] + ("..." if len(segment.text) > 80 else ""),
        )

    total_elapsed = time.time() - pipeline_t0
    logger.info(
        "[AssemblyAI] === Pipeline complete for %s: %d segments in %.1fs ===",
        video_id,
        len(segments),
        total_elapsed,
    )
    return segments
