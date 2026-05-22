"""AssemblyAI Speech-to-Text service.

Replaces the Gemini-based STT pipeline with AssemblyAI for faster
transcription with built-in sentence detection and word-level timestamps.

Reuses _extract_audio() from google_stt_service for YouTube audio download.
"""

import logging
import tempfile
import time
from pathlib import Path

import assemblyai as aai

from app.config import get_settings
from app.services.google_stt_service import (
    MAX_STT_DURATION,
    _extract_audio,
    _merge_short_segments,
    _enforce_non_overlapping,
)
from app.services.youtube_service import TranscriptSegment

logger = logging.getLogger(__name__)


def transcribe_with_assemblyai(
    video_id: str,
    language: str = "en",
    video_duration: int = 0,
) -> list[TranscriptSegment]:
    """Transcribe YouTube audio using AssemblyAI.

    Pipeline: extract audio via yt-dlp → AssemblyAI transcribe
    → sentence-level segments with precise timestamps.
    """
    from app.core.exceptions import BadRequestError

    settings = get_settings()
    if not settings.ASSEMBLYAI_API_KEY:
        raise BadRequestError("ASSEMBLYAI_API_KEY not configured for transcription.")

    if video_duration > MAX_STT_DURATION:
        raise BadRequestError(
            f"AI transcription is limited to {MAX_STT_DURATION // 60} minutes. "
            f"This video is {video_duration // 60}m {video_duration % 60}s. "
            f"Please choose a shorter video or one with manual subtitles."
        )

    aai.settings.api_key = settings.ASSEMBLYAI_API_KEY
    pipeline_t0 = time.time()
    logger.info("[AssemblyAI] === Starting pipeline for %s (duration=%ds) ===", video_id, video_duration)

    # Step 1: Extract audio
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        wav_path, actual_duration = _extract_audio(video_id, tmp_path)

        if actual_duration > MAX_STT_DURATION:
            raise BadRequestError(
                f"AI transcription is limited to {MAX_STT_DURATION // 60} minutes. "
                f"Detected audio is {actual_duration // 60}m {actual_duration % 60}s. "
                f"Please choose a shorter video or one with manual subtitles."
            )

        # Step 2: Transcribe with AssemblyAI
        config = aai.TranscriptionConfig(
            language_code=language,
            punctuate=True,
            format_text=True,
        )
        config.speech_models = ["universal-3-pro"]

        transcriber = aai.Transcriber()

        logger.info("[AssemblyAI] Uploading and transcribing %s...", wav_path.name)
        t0 = time.time()
        transcript = transcriber.transcribe(str(wav_path), config=config)
        elapsed = time.time() - t0
        logger.info("[AssemblyAI] Transcription completed in %.1fs", elapsed)

    if transcript.status == aai.TranscriptStatus.error:
        logger.error("[AssemblyAI] Transcription failed: %s", transcript.error)
        raise RuntimeError(f"AssemblyAI transcription failed: {transcript.error}")

    # Step 3: Get sentence-level segments
    sentences = transcript.get_sentences()
    if not sentences:
        logger.warning("[AssemblyAI] No sentences returned for %s", video_id)
        return []

    logger.info("[AssemblyAI] Got %d sentences from transcript", len(sentences))

    # Step 4: Convert to TranscriptSegment (AssemblyAI uses milliseconds)
    segments: list[TranscriptSegment] = []
    for i, sent in enumerate(sentences):
        start_sec = sent.start / 1000.0
        end_sec = sent.end / 1000.0
        duration_sec = end_sec - start_sec

        segments.append(TranscriptSegment(
            text=sent.text,
            start=start_sec,
            duration=duration_sec,
        ))

        logger.info(
            "[AssemblyAI] Sentence %2d | %6.2f → %6.2f (%5.2fs) | %s",
            i + 1, start_sec, end_sec, duration_sec,
            sent.text[:80] + ("…" if len(sent.text) > 80 else ""),
        )

    # Step 5: Merge short fragments (< 5 words)
    segments = _merge_short_segments(segments)

    # Step 6: Guarantee no overlapping audio ranges
    segments = _enforce_non_overlapping(segments)

    total_elapsed = time.time() - pipeline_t0
    logger.info(
        "[AssemblyAI] === Pipeline complete for %s: %d segments in %.1fs ===",
        video_id, len(segments), total_elapsed,
    )
    return segments
