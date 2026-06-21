"""Business logic for admin video management."""

import math
from datetime import UTC, datetime

from app.core.exceptions import NotFoundError
from app.events import publish_video_event
from app.models.user import User
from app.models.video import Video
from app.repositories.admin_video_repository import AdminVideoRepository
from app.schemas.admin import (
    AdminDifficultyRecalculationResponse,
    AdminPatchVideoRequest,
    AdminRetryTranscriptionResponse,
    AdminVideoListResponse,
    AdminVideoResponse,
)
from app.services.level_service import calculate_audio_difficulty, difficulty_update_values
from app.tasks.transcription import run_stt_pipeline


def _serialize_video(
    video: Video,
    *,
    creator: User | None = None,
    play_count: int = 0,
    best_score: float | None = None,
) -> AdminVideoResponse:
    return AdminVideoResponse(
        id=video.id,
        youtube_id=video.youtube_id,
        title=video.title,
        channel=video.channel,
        duration=video.duration,
        language=video.language,
        level=video.level,
        difficulty_score=video.difficulty_score,
        difficulty_level=video.difficulty_level,
        difficulty_label=video.difficulty_label,
        difficulty_factors=video.difficulty_factors,
        is_curated=video.is_curated,
        is_active=video.is_active,
        publish_status=getattr(video, "publish_status", None) or "published",
        published_at=video.published_at,
        reviewed_by=video.reviewed_by,
        reviewed_at=video.reviewed_at,
        review_note=video.review_note,
        is_auto_generated=video.is_auto_generated,
        transcription_status=video.transcription_status,
        transcription_error=video.transcription_error,
        thumbnail_url=video.thumbnail_url,
        created_by=video.created_by,
        created_by_email=creator.email if creator else None,
        created_by_name=creator.display_name if creator else None,
        created_at=video.created_at,
        play_count=play_count,
        best_score=round(best_score * 100, 1) if best_score is not None else None,
    )


class AdminVideoService:
    def __init__(self, repo: AdminVideoRepository):
        self.repo = repo

    async def list_videos(
        self, *, status, active, level, language, curated, search, page, page_size
    ) -> AdminVideoListResponse:
        rows, total = await self.repo.list_with_stats(
            status=status,
            active=active,
            level=level,
            language=language,
            curated=curated,
            search=search,
            page=page,
            page_size=page_size,
        )
        total_pages = max(1, math.ceil(total / page_size))
        return AdminVideoListResponse(
            items=[
                _serialize_video(
                    row[0], creator=row[1], play_count=row[2] or 0, best_score=row[3]
                )
                for row in rows
            ],
            total=total,
            page=page,
            total_pages=total_pages,
        )

    async def patch_video(
        self, video_id: str, body: AdminPatchVideoRequest
    ) -> AdminVideoResponse:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")

        if body.is_active is not None:
            video.is_active = body.is_active
        if body.is_curated is not None:
            video.is_curated = body.is_curated
        if "level" in body.model_fields_set:
            video.level = body.level

        await self.repo.commit()
        await self.repo.refresh(video)

        changes = body.model_dump(exclude_unset=True)
        await publish_video_event("video.updated", video.id, changes)

        creator = None
        if video.created_by:
            creator = await self.repo.get_creator(video.created_by)
        return _serialize_video(video, creator=creator)

    async def delete_video(self, video_id: str) -> None:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")

        await self.repo.delete_video_and_related(video)
        await self.repo.commit()

        await publish_video_event("video.deleted", video_id)

    async def retry_transcription(
        self, video_id: str, max_segment_duration: float
    ) -> AdminRetryTranscriptionResponse:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")

        video.transcription_status = "pending"
        video.transcription_error = None
        await self.repo.commit()

        task = run_stt_pipeline.delay(
            video_db_id=video.id,
            youtube_id=video.youtube_id,
            language=video.language,
            video_duration=video.duration,
            max_segment_duration=max_segment_duration,
            title=video.title,
            channel=video.channel,
        )

        return AdminRetryTranscriptionResponse(
            video_id=video.id,
            status="pending",
            task_id=task.id,
        )

    async def recalculate_difficulty(
        self, video_id: str
    ) -> AdminDifficultyRecalculationResponse:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")

        transcript_rows = await self.repo.get_transcripts_ordered(video_id)
        if not transcript_rows:
            raise NotFoundError("No transcript found for this video")

        analysis = calculate_audio_difficulty(
            video,
            list(transcript_rows),
            options={"duration_seconds": video.duration, "language": video.language},
        )
        video.level = analysis["level"]
        for field, value in difficulty_update_values(analysis).items():
            setattr(video, field, value)
        video.difficulty_updated_at = datetime.now(UTC)

        await self.repo.commit()

        await publish_video_event(
            "video.updated",
            video.id,
            {
                "difficulty_score": analysis["score"],
                "difficulty_level": analysis["level"],
                "difficulty_label": analysis["label"],
                "level": analysis["level"],
            },
        )

        return AdminDifficultyRecalculationResponse(
            video_id=video.id,
            difficulty_score=analysis["score"],
            difficulty_level=analysis["level"],
            difficulty_label=analysis["label"],
            factors=analysis["factors"],
            explanation=analysis["explanation"],
            recommendedModes=analysis["recommendedModes"],
        )
