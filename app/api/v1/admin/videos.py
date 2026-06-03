import math
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user
from app.core.exceptions import NotFoundError
from app.events import publish_video_event
from app.database import get_db
from app.models.dictation import DictationAttempt, DictationSentence
from app.models.room import RoomSession
from app.models.user import User
from app.models.video import Transcript, Video
from app.schemas.admin import (
    AdminDifficultyRecalculationResponse,
    AdminPatchVideoRequest,
    AdminRetryTranscriptionResponse,
    AdminVideoListResponse,
    AdminVideoResponse,
)
from app.services.level_service import calculate_audio_difficulty, difficulty_update_values
from app.tasks.transcription import run_stt_pipeline

router = APIRouter(prefix="/videos")

TRANSCRIPTION_STATUSES = {"pending", "processing", "ready", "failed"}


def _apply_video_filters(
    query,
    *,
    status: str | None,
    active: bool | None,
    level: str | None,
    language: str | None,
    curated: bool | None,
    search: str | None,
):
    if status:
        normalized = status.strip().lower()
        if normalized == "active":
            query = query.where(Video.is_active.is_(True))
        elif normalized == "inactive":
            query = query.where(Video.is_active.is_(False))
        elif normalized in TRANSCRIPTION_STATUSES:
            query = query.where(Video.transcription_status == normalized)
    if active is not None:
        query = query.where(Video.is_active == active)
    if level:
        query = query.where(Video.level == level)
    if language:
        query = query.where(Video.language == language)
    if curated is not None:
        query = query.where(Video.is_curated == curated)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(
            or_(
                Video.title.ilike(pattern),
                Video.channel.ilike(pattern),
                Video.youtube_id.ilike(pattern),
            )
        )
    return query


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


@router.get("", response_model=AdminVideoListResponse)
async def list_admin_videos(
    status: str | None = None,
    active: bool | None = None,
    level: str | None = None,
    language: str | None = None,
    curated: bool | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    stats_sub = (
        select(
            DictationAttempt.video_id,
            func.count().filter(DictationAttempt.status == "completed").label("play_count"),
            func.max(DictationAttempt.score).label("best_score"),
        )
        .group_by(DictationAttempt.video_id)
        .subquery()
    )

    count_q = _apply_video_filters(
        select(func.count()).select_from(Video),
        status=status,
        active=active,
        level=level,
        language=language,
        curated=curated,
        search=search,
    )
    total = (await db.execute(count_q)).scalar() or 0
    total_pages = max(1, math.ceil(total / page_size))

    query = (
        select(
            Video,
            User,
            func.coalesce(stats_sub.c.play_count, 0).label("play_count"),
            stats_sub.c.best_score,
        )
        .outerjoin(User, Video.created_by == User.id)
        .outerjoin(stats_sub, Video.id == stats_sub.c.video_id)
    )
    query = _apply_video_filters(
        query,
        status=status,
        active=active,
        level=level,
        language=language,
        curated=curated,
        search=search,
    )
    query = query.order_by(Video.created_at.desc()).offset((page - 1) * page_size).limit(page_size)

    rows = (await db.execute(query)).all()
    return AdminVideoListResponse(
        items=[
            _serialize_video(
                row[0],
                creator=row[1],
                play_count=row[2] or 0,
                best_score=row[3],
            )
            for row in rows
        ],
        total=total,
        page=page,
        total_pages=total_pages,
    )


@router.patch("/{video_id}", response_model=AdminVideoResponse)
async def patch_admin_video(
    video_id: str,
    body: AdminPatchVideoRequest,
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    video = (await db.execute(select(Video).where(Video.id == video_id))).scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")

    if body.is_active is not None:
        video.is_active = body.is_active
    if body.is_curated is not None:
        video.is_curated = body.is_curated
    if "level" in body.model_fields_set:
        video.level = body.level

    await db.commit()
    await db.refresh(video)

    changes = body.model_dump(exclude_unset=True)
    await publish_video_event("video.updated", video.id, changes)

    creator = None
    if video.created_by:
        creator = (
            await db.execute(select(User).where(User.id == video.created_by))
        ).scalar_one_or_none()
    return _serialize_video(video, creator=creator)


@router.delete("/{video_id}", status_code=204)
async def delete_admin_video(
    video_id: str,
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    video = (await db.execute(select(Video).where(Video.id == video_id))).scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")

    attempts = await db.execute(
        select(DictationAttempt).where(DictationAttempt.video_id == video_id)
    )
    for attempt in attempts.scalars().all():
        await db.execute(
            delete(DictationSentence).where(DictationSentence.attempt_id == attempt.id)
        )

    await db.execute(delete(DictationAttempt).where(DictationAttempt.video_id == video_id))
    await db.execute(delete(RoomSession).where(RoomSession.video_id == video_id))
    await db.delete(video)
    await db.commit()

    await publish_video_event("video.deleted", video_id)

    return Response(status_code=204)


@router.post("/{video_id}/retry-transcription", response_model=AdminRetryTranscriptionResponse)
async def retry_admin_transcription(
    video_id: str,
    max_segment_duration: float = Query(10.0, ge=3.0, le=30.0),
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    video = (await db.execute(select(Video).where(Video.id == video_id))).scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")

    video.transcription_status = "pending"
    video.transcription_error = None
    await db.commit()

    task = run_stt_pipeline.delay(
        video_db_id=video.id,
        youtube_id=video.youtube_id,
        language=video.language,
        video_duration=video.duration,
        max_segment_duration=max_segment_duration,
    )

    return AdminRetryTranscriptionResponse(
        video_id=video.id,
        status="pending",
        task_id=task.id,
    )


@router.post(
    "/{video_id}/recalculate-difficulty", response_model=AdminDifficultyRecalculationResponse
)
async def recalculate_admin_video_difficulty(
    video_id: str,
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    video = (await db.execute(select(Video).where(Video.id == video_id))).scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")

    transcript_rows = (
        (
            await db.execute(
                select(Transcript).where(Transcript.video_id == video_id).order_by(Transcript.index)
            )
        )
        .scalars()
        .all()
    )
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

    await db.commit()

    await publish_video_event("video.updated", video.id, {
        "difficulty_score": analysis["score"],
        "difficulty_level": analysis["level"],
        "difficulty_label": analysis["label"],
        "level": analysis["level"],
    })

    return AdminDifficultyRecalculationResponse(
        video_id=video.id,
        difficulty_score=analysis["score"],
        difficulty_level=analysis["level"],
        difficulty_label=analysis["label"],
        factors=analysis["factors"],
        explanation=analysis["explanation"],
        recommendedModes=analysis["recommendedModes"],
    )
