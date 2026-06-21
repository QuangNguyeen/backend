import logging
import time
from datetime import UTC, datetime

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from app.celery_app import celery
from app.config import get_settings
from app.events import publish_video_event_sync
from app.models.video import Transcript, Video
from app.services.assemblyai_service import transcribe_with_assemblyai
from app.services.level_service import calculate_audio_difficulty, difficulty_update_values
from app.services.stt_audio_service import VideoUnavailableError

logger = logging.getLogger(__name__)

_engine = None


def _get_sync_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(settings.DATABASE_URL_SYNC, pool_pre_ping=True)
    return _engine


@celery.task(bind=True, name="transcription.run_stt_pipeline", max_retries=1)
def run_stt_pipeline(
    self,
    video_db_id: str,
    youtube_id: str,
    language: str,
    video_duration: int,
    max_segment_duration: float = 10.0,
    title: str | None = None,
    channel: str | None = None,
    prompt_context: str | None = None,
):
    """Run the AssemblyAI STT pipeline in a Celery worker.

    Updates the video's transcription_status through:
      pending → processing → ready | failed
    """
    pipeline_t0 = time.time()
    engine = _get_sync_engine()

    logger.info(
        "[CELERY] ▶ Task started for youtube=%s (db=%s, lang=%s, duration=%ds, retry=%d/%d)",
        youtube_id,
        video_db_id,
        language,
        video_duration,
        self.request.retries,
        self.max_retries,
    )

    with Session(engine) as db:
        db.execute(
            update(Video).where(Video.id == video_db_id).values(transcription_status="processing")
        )
        db.commit()
    logger.info("[CELERY] Status → processing for %s", youtube_id)

    try:
        logger.info("[CELERY] Calling transcribe_with_assemblyai for %s...", youtube_id)
        stt_t0 = time.time()
        stt_segments = transcribe_with_assemblyai(
            youtube_id,
            language,
            video_duration,
            max_segment_duration=max_segment_duration,
            prompt_context={
                "title": title,
                "channel": channel,
                "captions": prompt_context,
            },
        )
        logger.info(
            "[CELERY] AssemblyAI returned %d segments in %.1fs for %s",
            len(stt_segments) if stt_segments else 0,
            time.time() - stt_t0,
            youtube_id,
        )

        if not stt_segments:
            logger.error("[CELERY] STT pipeline returned empty for %s", youtube_id)
            _mark_failed(video_db_id, "STT pipeline returned no segments")
            return {"status": "failed", "video_id": video_db_id}

        with Session(engine) as db:
            video_exists = db.execute(
                select(Video.id).where(Video.id == video_db_id)
            ).scalar_one_or_none()
            if not video_exists:
                logger.warning("[CELERY] Video %s was deleted during STT, aborting", video_db_id)
                return {"status": "aborted", "video_id": video_db_id}

            old_count = db.query(Transcript).filter(Transcript.video_id == video_db_id).delete()
            logger.info("[CELERY] Deleted %d old transcripts for %s", old_count, youtube_id)

            for idx, seg in enumerate(stt_segments):
                db.add(
                    Transcript(
                        video_id=video_db_id,
                        language=language,
                        index=idx,
                        text=seg.text,
                        start_time=seg.start,
                        end_time=seg.end,
                    )
                )

            difficulty = calculate_audio_difficulty(
                None,
                stt_segments,
                options={"duration_seconds": video_duration, "language": language},
            )
            level = difficulty["level"]
            difficulty_values = difficulty_update_values(difficulty)
            difficulty_values["difficulty_updated_at"] = datetime.now(UTC)

            db.execute(
                update(Video)
                .where(Video.id == video_db_id)
                .values(
                    transcription_status="ready",
                    level=level,
                    is_auto_generated=False,
                    **difficulty_values,
                )
            )
            db.commit()
        publish_video_event_sync(
            "video.transcription_ready",
            video_db_id,
            {"transcription_status": "ready", "level": level},
        )

        elapsed = time.time() - pipeline_t0
        logger.info(
            "[CELERY] ✔ Pipeline complete for %s: %d segments, level=%s, total=%.1fs",
            youtube_id,
            len(stt_segments),
            level,
            elapsed,
        )
        return {
            "status": "ready",
            "video_id": video_db_id,
            "segments": len(stt_segments),
            "level": level,
        }

    except VideoUnavailableError as exc:
        elapsed = time.time() - pipeline_t0
        logger.warning(
            "[CELERY] ✘ Video unavailable for %s after %.1fs (permanent, no retry): %s",
            youtube_id,
            elapsed,
            exc,
        )
        _mark_failed(video_db_id, str(exc))
        return {"status": "failed", "video_id": video_db_id, "reason": "video_unavailable"}

    except Exception as exc:
        elapsed = time.time() - pipeline_t0
        logger.error(
            "[CELERY] ✘ Pipeline failed for %s after %.1fs (retry %d/%d): %s",
            youtube_id,
            elapsed,
            self.request.retries,
            self.max_retries,
            exc,
            exc_info=True,
        )
        _mark_failed(video_db_id, str(exc))
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30)
        raise


def _mark_failed(video_db_id: str, error_msg: str):
    engine = _get_sync_engine()
    with Session(engine) as db:
        db.execute(
            update(Video)
            .where(Video.id == video_db_id)
            .values(
                transcription_status="failed",
                transcription_error=error_msg[:500],
            )
        )
        db.commit()
    publish_video_event_sync(
        "video.transcription_failed",
        video_db_id,
        {"transcription_status": "failed"},
    )
    logger.info("[CELERY] Status → failed for %s: %s", video_db_id, error_msg[:100])
