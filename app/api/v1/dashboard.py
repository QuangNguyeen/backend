from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.dictation import DictationAttempt, DictationSentence
from app.models.video import Video
from app.schemas.dictation import DashboardStatsResponse, HistoryEntryResponse

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/stats", response_model=DashboardStatsResponse)
async def get_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Total completed attempts
    result = await db.execute(
        select(func.count()).where(
            DictationAttempt.user_id == current_user.id,
            DictationAttempt.status == "completed",
        )
    )
    total_sessions = result.scalar() or 0

    # Average accuracy
    result = await db.execute(
        select(func.avg(DictationSentence.score))
        .join(DictationAttempt)
        .where(DictationAttempt.user_id == current_user.id)
    )
    avg_accuracy = result.scalar() or 0.0

    # Total videos practiced
    result = await db.execute(
        select(func.count(func.distinct(DictationAttempt.video_id))).where(
            DictationAttempt.user_id == current_user.id
        )
    )
    total_videos = result.scalar() or 0

    return DashboardStatsResponse(
        total_sessions=total_sessions,
        total_time_minutes=0,  # TODO: compute from duration_seconds
        average_accuracy=round(avg_accuracy * 100, 1),
        total_videos=total_videos,
        streak_days=current_user.streak_days,
    )


@router.get("/history", response_model=list[HistoryEntryResponse])
async def get_history(
    limit: int = 20,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DictationAttempt, Video.title)
        .join(Video, DictationAttempt.video_id == Video.id)
        .where(
            DictationAttempt.user_id == current_user.id,
            DictationAttempt.status == "completed",
        )
        .order_by(DictationAttempt.completed_at.desc())
        .limit(limit)
        .offset(offset)
    )

    entries = []
    for attempt, video_title in result.all():
        entries.append(HistoryEntryResponse(
            id=attempt.id,
            video_title=video_title,
            type="dictation",
            score=round((attempt.score or 0) * 100, 1),
            duration_minutes=0,  # TODO: compute from duration_seconds
            completed_at=(attempt.completed_at or attempt.created_at).isoformat(),
        ))

    return entries
