import logging

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from app.celery_app import celery
from app.config import get_settings
from app.models.video import Transcript, Video
from app.services.google_stt_service import transcribe_youtube_video
from app.services.level_service import analyze_level, estimate_cefr_with_speech_rate
from app.services import youtube_service

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
):
    """Run the full STT + Gemini pipeline in a Celery worker.

    Updates the video's transcription_status through:
      pending → processing → ready | failed
    """
    engine = _get_sync_engine()

    with Session(engine) as db:
        db.execute(
            update(Video)
            .where(Video.id == video_db_id)
            .values(transcription_status="processing")
        )
        db.commit()

    try:
        lang_code = language if "-" in language else f"{language}-US"
        stt_segments = transcribe_youtube_video(youtube_id, lang_code, video_duration)

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

            db.query(Transcript).filter(Transcript.video_id == video_db_id).delete()

            for idx, seg in enumerate(stt_segments):
                db.add(Transcript(
                    video_id=video_db_id,
                    language=language,
                    index=idx,
                    text=seg.text,
                    start_time=seg.start,
                    end_time=seg.end,
                ))

            full_text = youtube_service.get_full_text(stt_segments)
            if language == "en" and video_duration > 0:
                level = estimate_cefr_with_speech_rate(full_text, duration_seconds=video_duration, language=language)
            else:
                level = analyze_level(full_text, language=language)

            db.execute(
                update(Video)
                .where(Video.id == video_db_id)
                .values(
                    transcription_status="ready",
                    level=level,
                    is_auto_generated=True,
                )
            )
            db.commit()

        logger.info(
            "[CELERY] STT pipeline complete for %s: %d segments, level=%s",
            youtube_id, len(stt_segments), level,
        )
        return {
            "status": "ready",
            "video_id": video_db_id,
            "segments": len(stt_segments),
            "level": level,
        }

    except Exception as exc:
        logger.error("[CELERY] STT pipeline failed for %s: %s", youtube_id, exc, exc_info=True)
        _mark_failed(video_db_id, str(exc))
        raise self.retry(exc=exc, countdown=30) if self.request.retries < self.max_retries else exc


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