from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user
from app.database import get_db
from app.models.dictation import DictationAttempt
from app.models.user import User
from app.models.video import Video
from app.models.vocabulary import SavedWord
from app.schemas.admin import (
    AdminContentHealthResponse,
    AdminEngagementResponse,
    AdminRecentActivity,
    AdminRecentActivityResponse,
    AdminStudyHoursPoint,
    AdminStudyHoursResponse,
    AdminTopLearner,
    AdminTopLearnersResponse,
    AdminTrafficPoint,
    AdminTrafficResponse,
)

router = APIRouter(prefix="/analytics")

TIME_RANGE_DAYS = {
    "1d": 1,
    "7d": 7,
    "30d": 30,
    "90d": 90,
}


def _range_start(time_range: str) -> date:
    days = TIME_RANGE_DAYS.get(time_range, 7)
    return date.today() - timedelta(days=days - 1)


def _day_series(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def _hour_series_for_today() -> list[int]:
    return list(range(datetime.now().hour + 1))


def _hour_point_date(day: date, hour: int) -> str:
    return f"{day.isoformat()}T{hour:02d}:00:00"


async def _scalar(db: AsyncSession, query, default=0):
    return (await db.execute(query)).scalar() or default


def _add_active_user_bucket(buckets, bucket, user_id: str | None) -> None:
    if bucket is None or not user_id:
        return
    buckets.setdefault(bucket, set()).add(str(user_id))


async def _active_users_by_hour(db: AsyncSession, day: date) -> dict[int, int]:
    buckets: dict[int, set[str]] = {}

    login_rows = (
        await db.execute(
            select(
                User.id,
                func.extract("hour", User.last_login_at).label("hour"),
            ).where(cast(User.last_login_at, Date) == day)
        )
    ).all()
    for row in login_rows:
        _add_active_user_bucket(buckets, int(row.hour), row.id)

    attempt_rows = (
        await db.execute(
            select(
                DictationAttempt.user_id,
                func.extract("hour", DictationAttempt.updated_at).label("hour"),
            ).where(cast(DictationAttempt.updated_at, Date) == day)
        )
    ).all()
    for row in attempt_rows:
        _add_active_user_bucket(buckets, int(row.hour), row.user_id)

    return {hour: len(user_ids) for hour, user_ids in buckets.items()}


async def _active_users_by_day(db: AsyncSession, start: date) -> dict[date, int]:
    buckets: dict[date, set[str]] = {}

    login_rows = (
        await db.execute(
            select(
                User.id,
                cast(User.last_login_at, Date).label("day"),
            ).where(cast(User.last_login_at, Date) >= start)
        )
    ).all()
    for row in login_rows:
        _add_active_user_bucket(buckets, row.day, row.id)

    attempt_rows = (
        await db.execute(
            select(
                DictationAttempt.user_id,
                cast(DictationAttempt.updated_at, Date).label("day"),
            ).where(cast(DictationAttempt.updated_at, Date) >= start)
        )
    ).all()
    for row in attempt_rows:
        _add_active_user_bucket(buckets, row.day, row.user_id)

    return {day: len(user_ids) for day, user_ids in buckets.items()}


@router.get("/traffic", response_model=AdminTrafficResponse)
async def get_admin_traffic(
    time_range: str = Query("7d", pattern="^(1d|7d|30d|90d)$"),
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    start = _range_start(time_range)
    today = date.today()

    if time_range == "1d":
        active_by_hour = await _active_users_by_hour(db, today)
        new_rows = (
            await db.execute(
                select(
                    func.extract("hour", User.created_at).label("hour"),
                    func.count(User.id).label("new_users"),
                )
                .where(cast(User.created_at, Date) == today)
                .group_by("hour")
            )
        ).all()
        new_by_hour = {
            int(row.hour): int(row.new_users or 0) for row in new_rows if row.hour is not None
        }

        points = [
            AdminTrafficPoint(
                date=_hour_point_date(today, hour),
                active_users=active_by_hour.get(hour, 0),
                new_users=new_by_hour.get(hour, 0),
            )
            for hour in _hour_series_for_today()
        ]
        return AdminTrafficResponse(points=points, time_range=time_range)

    active_by_day = await _active_users_by_day(db, start)
    new_rows = (
        await db.execute(
            select(
                cast(User.created_at, Date).label("day"),
                func.count(User.id).label("new_users"),
            )
            .where(cast(User.created_at, Date) >= start)
            .group_by("day")
        )
    ).all()
    new_by_day = {row.day: int(row.new_users or 0) for row in new_rows if row.day}

    points = [
        AdminTrafficPoint(
            date=day.isoformat(),
            active_users=active_by_day.get(day, 0),
            new_users=new_by_day.get(day, 0),
        )
        for day in _day_series(start, today)
    ]
    return AdminTrafficResponse(points=points, time_range=time_range)


@router.get("/study-hours", response_model=AdminStudyHoursResponse)
async def get_admin_study_hours(
    time_range: str = Query("7d", pattern="^(1d|7d|30d|90d)$"),
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    start = _range_start(time_range)
    today = date.today()

    if time_range == "1d":
        rows = (
            await db.execute(
                select(
                    func.extract("hour", DictationAttempt.updated_at).label("hour"),
                    func.coalesce(
                        func.sum(DictationAttempt.duration_seconds),
                        0,
                    ).label("total_seconds"),
                    func.count(func.distinct(DictationAttempt.user_id)).label("active_users"),
                )
                .where(
                    DictationAttempt.status == "completed",
                    cast(DictationAttempt.updated_at, Date) == today,
                )
                .group_by("hour")
            )
        ).all()
        by_hour = {
            int(row.hour): {
                "total_seconds": float(row.total_seconds or 0),
                "active_users": int(row.active_users or 0),
            }
            for row in rows
            if row.hour is not None
        }

        points: list[AdminStudyHoursPoint] = []
        for hour in _hour_series_for_today():
            item = by_hour.get(hour, {"total_seconds": 0.0, "active_users": 0})
            total_minutes = round(item["total_seconds"] / 60, 1)
            avg_minutes = (
                round(total_minutes / item["active_users"], 1) if item["active_users"] else 0.0
            )
            points.append(
                AdminStudyHoursPoint(
                    date=_hour_point_date(today, hour),
                    total_minutes=total_minutes,
                    avg_minutes_per_user=avg_minutes,
                )
            )

        return AdminStudyHoursResponse(points=points, time_range=time_range)

    rows = (
        await db.execute(
            select(
                cast(DictationAttempt.updated_at, Date).label("day"),
                func.coalesce(func.sum(DictationAttempt.duration_seconds), 0).label(
                    "total_seconds"
                ),
                func.count(func.distinct(DictationAttempt.user_id)).label("active_users"),
            )
            .where(
                DictationAttempt.status == "completed",
                cast(DictationAttempt.updated_at, Date) >= start,
            )
            .group_by("day")
        )
    ).all()
    by_day = {
        row.day: {
            "total_seconds": float(row.total_seconds or 0),
            "active_users": int(row.active_users or 0),
        }
        for row in rows
        if row.day
    }

    points: list[AdminStudyHoursPoint] = []
    for day in _day_series(start, today):
        item = by_day.get(day, {"total_seconds": 0.0, "active_users": 0})
        total_minutes = round(item["total_seconds"] / 60, 1)
        avg_minutes = (
            round(total_minutes / item["active_users"], 1) if item["active_users"] else 0.0
        )
        points.append(
            AdminStudyHoursPoint(
                date=day.isoformat(),
                total_minutes=total_minutes,
                avg_minutes_per_user=avg_minutes,
            )
        )

    return AdminStudyHoursResponse(points=points, time_range=time_range)


@router.get("/top-learners", response_model=AdminTopLearnersResponse)
async def get_admin_top_learners(
    time_range: str = Query("7d", pattern="^(1d|7d|30d|90d)$"),
    limit: int = Query(10, ge=1, le=50),
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    start = _range_start(time_range)

    attempts_subquery = (
        select(
            DictationAttempt.user_id,
            func.coalesce(func.sum(DictationAttempt.duration_seconds), 0).label("study_seconds"),
            func.count(DictationAttempt.id).label("sessions"),
            func.coalesce(func.avg(DictationAttempt.score), 0).label("avg_accuracy"),
        )
        .where(
            DictationAttempt.status == "completed",
            cast(DictationAttempt.updated_at, Date) >= start,
        )
        .group_by(DictationAttempt.user_id)
        .subquery()
    )

    rows = (
        await db.execute(
            select(
                User.id,
                User.display_name,
                User.email,
                User.streak_days,
                func.coalesce(attempts_subquery.c.study_seconds, 0).label("study_seconds"),
                func.coalesce(attempts_subquery.c.sessions, 0).label("sessions"),
                func.coalesce(attempts_subquery.c.avg_accuracy, 0).label("avg_accuracy"),
            )
            .outerjoin(attempts_subquery, attempts_subquery.c.user_id == User.id)
            .where(
                or_(
                    attempts_subquery.c.sessions > 0,
                    cast(User.last_login_at, Date) >= start,
                    cast(User.created_at, Date) >= start,
                )
            )
            .order_by(
                func.coalesce(attempts_subquery.c.study_seconds, 0).desc(),
                func.coalesce(attempts_subquery.c.sessions, 0).desc(),
                User.last_login_at.desc().nullslast(),
                User.created_at.desc(),
            )
            .limit(limit)
        )
    ).all()

    learners = [
        AdminTopLearner(
            user_id=row.id,
            display_name=row.display_name,
            email=row.email,
            study_minutes=round(float(row.study_seconds or 0) / 60, 1),
            sessions=int(row.sessions or 0),
            avg_accuracy=round(float(row.avg_accuracy or 0) * 100, 1),
            streak=int(row.streak_days or 0),
        )
        for row in rows
    ]
    return AdminTopLearnersResponse(learners=learners, time_range=time_range)


@router.get("/content-health", response_model=AdminContentHealthResponse)
async def get_admin_content_health(
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    status_rows = (
        await db.execute(
            select(Video.transcription_status, func.count(Video.id)).group_by(
                Video.transcription_status
            )
        )
    ).all()
    status_counts = {status or "ready": int(count or 0) for status, count in status_rows}

    level_rows = (
        await db.execute(
            select(Video.level, func.count(Video.id))
            .where(Video.level.is_not(None))
            .group_by(Video.level)
            .order_by(Video.level)
        )
    ).all()
    levels = {level: int(count or 0) for level, count in level_rows if level}

    total_videos = await _scalar(db, select(func.count()).select_from(Video))
    curated = await _scalar(db, select(func.count()).where(Video.is_curated.is_(True)))

    return AdminContentHealthResponse(
        total_videos=int(total_videos),
        ready=status_counts.get("ready", 0),
        pending=status_counts.get("pending", 0),
        processing=status_counts.get("processing", 0),
        failed=status_counts.get("failed", 0),
        curated=int(curated),
        levels=levels,
    )


@router.get("/engagement", response_model=AdminEngagementResponse)
async def get_admin_engagement(
    time_range: str = Query("7d", pattern="^(1d|7d|30d|90d)$"),
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    start = _range_start(time_range)

    total_attempts = await _scalar(
        db,
        select(func.count()).where(cast(DictationAttempt.updated_at, Date) >= start),
    )
    completed_attempts = await _scalar(
        db,
        select(func.count()).where(
            DictationAttempt.status == "completed",
            cast(DictationAttempt.updated_at, Date) >= start,
        ),
    )
    total_duration = await _scalar(
        db,
        select(func.coalesce(func.sum(DictationAttempt.duration_seconds), 0)).where(
            DictationAttempt.status == "completed",
            cast(DictationAttempt.updated_at, Date) >= start,
        ),
    )
    active_users = await _scalar(
        db,
        select(func.count(func.distinct(DictationAttempt.user_id))).where(
            cast(DictationAttempt.updated_at, Date) >= start,
        ),
    )

    repeat_subquery = (
        select(DictationAttempt.user_id)
        .where(
            DictationAttempt.status == "completed",
            cast(DictationAttempt.updated_at, Date) >= start,
        )
        .group_by(DictationAttempt.user_id)
        .having(func.count(DictationAttempt.id) >= 2)
        .subquery()
    )
    repeat_users = await _scalar(
        db,
        select(func.count()).select_from(repeat_subquery),
    )

    saved_words = await _scalar(
        db,
        select(func.count()).where(
            SavedWord.deleted_at.is_(None),
            cast(SavedWord.created_at, Date) >= start,
        ),
    )

    completion_rate = (completed_attempts / total_attempts * 100) if total_attempts else 0.0
    avg_session_duration = (
        (float(total_duration) / 60 / completed_attempts) if completed_attempts else 0.0
    )
    repeat_rate = (repeat_users / active_users * 100) if active_users else 0.0
    vocab_save_rate = (saved_words / completed_attempts) if completed_attempts else 0.0

    return AdminEngagementResponse(
        completion_rate=round(completion_rate, 1),
        avg_session_duration=round(avg_session_duration, 1),
        repeat_rate=round(repeat_rate, 1),
        vocab_save_rate=round(vocab_save_rate, 1),
    )


@router.get("/recent-activity", response_model=AdminRecentActivityResponse)
async def get_admin_recent_activity(
    limit: int = Query(20, ge=1, le=100),
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    activities: list[AdminRecentActivity] = []

    signup_rows = (
        await db.execute(
            select(User.id, User.display_name, User.created_at)
            .order_by(User.created_at.desc())
            .limit(limit)
        )
    ).all()
    activities.extend(
        AdminRecentActivity(
            id=f"user_signup:{row.id}",
            type="user_signup",
            description=f"{row.display_name} joined DictaLearn",
            user_name=row.display_name,
            timestamp=row.created_at,
        )
        for row in signup_rows
    )

    login_rows = (
        await db.execute(
            select(User.id, User.display_name, User.last_login_at)
            .where(User.last_login_at.is_not(None))
            .order_by(User.last_login_at.desc())
            .limit(limit)
        )
    ).all()
    activities.extend(
        AdminRecentActivity(
            id=f"user_login:{row.id}:{row.last_login_at.isoformat()}",
            type="user_login",
            description=f"{row.display_name} signed in",
            user_name=row.display_name,
            timestamp=row.last_login_at,
        )
        for row in login_rows
        if row.last_login_at is not None
    )

    session_rows = (
        await db.execute(
            select(
                DictationAttempt.id,
                DictationAttempt.updated_at,
                DictationAttempt.score,
                User.display_name,
                Video.title,
            )
            .join(User, DictationAttempt.user_id == User.id)
            .join(Video, DictationAttempt.video_id == Video.id)
            .where(DictationAttempt.status == "completed")
            .order_by(DictationAttempt.updated_at.desc())
            .limit(limit)
        )
    ).all()
    activities.extend(
        AdminRecentActivity(
            id=f"session_completed:{row.id}",
            type="session_completed",
            description=(
                f"{row.display_name} completed {row.title}"
                + (f" with {round(float(row.score) * 100, 1)}%" if row.score is not None else "")
            ),
            user_name=row.display_name,
            timestamp=row.updated_at,
        )
        for row in session_rows
    )

    video_rows = (
        await db.execute(
            select(Video.id, Video.title, Video.created_at, User.display_name)
            .outerjoin(User, Video.created_by == User.id)
            .order_by(Video.created_at.desc())
            .limit(limit)
        )
    ).all()
    activities.extend(
        AdminRecentActivity(
            id=f"video_added:{row.id}",
            type="video_added",
            description=f"Video added: {row.title}",
            user_name=row.display_name,
            timestamp=row.created_at,
        )
        for row in video_rows
    )

    failed_rows = (
        await db.execute(
            select(Video.id, Video.title, Video.created_at, Video.transcription_error)
            .where(Video.transcription_status == "failed")
            .order_by(Video.created_at.desc())
            .limit(limit)
        )
    ).all()
    activities.extend(
        AdminRecentActivity(
            id=f"transcription_failed:{row.id}",
            type="transcription_failed",
            description=(
                f"Transcription failed for {row.title}"
                + (f": {row.transcription_error[:90]}" if row.transcription_error else "")
            ),
            user_name=None,
            timestamp=row.created_at,
        )
        for row in failed_rows
    )

    activities.sort(key=lambda item: item.timestamp, reverse=True)
    return AdminRecentActivityResponse(activities=activities[:limit])
