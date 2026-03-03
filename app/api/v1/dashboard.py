from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.session import DictationSession, SentenceResult
from app.models.video import Video
from app.schemas.dictation import DashboardStatsResponse, HistoryEntryResponse

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/stats", response_model=DashboardStatsResponse)
async def get_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Total sessions
    result = await db.execute(
        select(func.count()).where(DictationSession.user_id == current_user.id)
    )
    total_sessions = result.scalar() or 0

    # Average accuracy
    result = await db.execute(
        select(func.avg(SentenceResult.score)).join(DictationSession).where(
            DictationSession.user_id == current_user.id
        )
    )
    avg_accuracy = result.scalar() or 0.0

    # Total videos
    result = await db.execute(
        select(func.count(func.distinct(DictationSession.video_id))).where(
            DictationSession.user_id == current_user.id
        )
    )
    total_videos = result.scalar() or 0

    return DashboardStatsResponse(
        total_sessions=total_sessions,
        total_time_minutes=0,  # TODO: compute from session durations
        average_accuracy=round(avg_accuracy * 100, 1),
        total_videos=total_videos,
        streak=current_user.streak,
    )


@router.get("/history", response_model=list[HistoryEntryResponse])
async def get_history(
    limit: int = 20,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DictationSession, Video.title)
        .join(Video, DictationSession.video_id == Video.id)
        .where(DictationSession.user_id == current_user.id)
        .order_by(DictationSession.started_at.desc())
        .limit(limit)
        .offset(offset)
    )

    entries = []
    for session, video_title in result.all():
        entries.append(HistoryEntryResponse(
            id=session.id,
            video_title=video_title,
            type="dictation",
            score=round(session.score * 100, 1),
            duration_minutes=0,  # TODO
            completed_at=session.started_at.isoformat(),
        ))

    return entries
