import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, cast, Date

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.dictation import DictationAttempt, DictationSentence
from app.models.video import Video
from app.schemas.dictation import (
    DashboardFullResponse,
    DashboardStatsResponse,
    HeatmapDay,
    AccuracyPoint,
    HistoryEntryResponse,
)
from app.services.stats_service import compute_streaks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


def _count_to_level(count: int) -> int:
    """Map attempt count to 0-4 intensity level."""
    if count == 0:
        return 0
    if count <= 1:
        return 1
    if count <= 3:
        return 2
    if count <= 5:
        return 3
    return 4


@router.get("/full", response_model=DashboardFullResponse)
async def get_dashboard(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Single endpoint returning all dashboard data: stats, heatmap, accuracy trend."""
    today = date.today()
    year_start = date(today.year, 1, 1)

    # ── Aggregate stats ──────────────────────────────────────────────────────

    result = await db.execute(
        select(func.count()).where(
            DictationAttempt.user_id == current_user.id,
            DictationAttempt.status == "completed",
        )
    )
    total_sessions = result.scalar() or 0

    result = await db.execute(
        select(func.count())
        .select_from(DictationSentence)
        .join(DictationAttempt)
        .where(DictationAttempt.user_id == current_user.id)
    )
    total_sentences = result.scalar() or 0

    result = await db.execute(
        select(func.avg(DictationSentence.score))
        .join(DictationAttempt)
        .where(DictationAttempt.user_id == current_user.id)
    )
    avg_accuracy = result.scalar() or 0.0

    result = await db.execute(
        select(func.count(func.distinct(DictationAttempt.video_id))).where(
            DictationAttempt.user_id == current_user.id
        )
    )
    total_videos = result.scalar() or 0

    # ── Heatmap (last 365 days, keyed by activity date) ──────────────────────

    result = await db.execute(
        select(
            cast(DictationAttempt.updated_at, Date).label("day"),
            func.count().label("cnt"),
        )
        .where(
            DictationAttempt.user_id == current_user.id,
            cast(DictationAttempt.updated_at, Date) >= year_start,
        )
        .group_by("day")
    )
    day_counts: dict[date, int] = {}
    for row in result.all():
        if row.day is not None:
            day_counts[row.day] = row.cnt

    # Build full 365-day array — react-activity-calendar needs contiguous dates
    heatmap: list[HeatmapDay] = []
    d = year_start
    while d <= today:
        count = day_counts.get(d, 0)
        heatmap.append(HeatmapDay(
            date=d.isoformat(),
            count=count,
            level=_count_to_level(count),
        ))
        d += timedelta(days=1)

    # ── Streaks ──────────────────────────────────────────────────────────────

    active_dates = set(day_counts.keys())
    current_streak, longest_streak = compute_streaks(active_dates, today)

    # ── Accuracy trend (last 20 completed sessions, chronological) ───────────

    result = await db.execute(
        select(
            DictationAttempt.completed_at,
            DictationAttempt.updated_at,
            DictationAttempt.score,
        )
        .where(
            DictationAttempt.user_id == current_user.id,
            DictationAttempt.status == "completed",
            DictationAttempt.score.is_not(None),
        )
        .order_by(DictationAttempt.completed_at.desc().nullslast())
        .limit(20)
    )
    trend_rows = list(reversed(result.all()))
    accuracy_trend = []
    for i, row in enumerate(trend_rows):
        ts = row.completed_at or row.updated_at
        label = ts.strftime("%b %d") if ts else f"Session {i + 1}"
        score_pct = round((row.score or 0) * 100, 1)
        accuracy_trend.append(AccuracyPoint(
            date=label,
            score=score_pct,
            accuracy=score_pct,
        ))

    return DashboardFullResponse(
        stats=DashboardStatsResponse(
            total_sessions=total_sessions,
            total_sentences=total_sentences,
            total_time_minutes=0,
            average_accuracy=round(avg_accuracy * 100, 1),
            total_videos=total_videos,
            current_streak=current_streak,
            longest_streak=longest_streak,
        ),
        heatmap=heatmap,
        accuracy_trend=accuracy_trend,
    )


@router.get("/history", response_model=list[HistoryEntryResponse])
async def get_history(
    limit: int = 20,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Recent sessions (both completed and in-progress), with video metadata."""
    result = await db.execute(
        select(DictationAttempt, Video.title, Video.thumbnail_url)
        .join(Video, DictationAttempt.video_id == Video.id)
        .where(DictationAttempt.user_id == current_user.id)
        .order_by(DictationAttempt.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )

    entries = []
    for attempt, video_title, thumbnail in result.all():
        total = attempt.total_sentences or 0
        current = attempt.current_sentence_index or 0
        entries.append(HistoryEntryResponse(
            id=attempt.id,
            video_title=video_title,
            video_thumbnail=thumbnail or "",
            type="dictation",
            status=attempt.status,
            score=round((attempt.score or 0) * 100, 1) if attempt.score is not None else None,
            progress_str=f"{current}/{total}",
            completed_at=attempt.completed_at.isoformat() if attempt.completed_at else None,
            updated_at=attempt.updated_at.isoformat(),
        ))

    return entries