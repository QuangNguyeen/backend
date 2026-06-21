"""Database access for admin analytics (traffic, study hours, engagement, etc.)."""

from datetime import date

from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dictation import DictationAttempt
from app.models.user import User
from app.models.video import Video
from app.models.vocabulary import SavedWord


class AdminAnalyticsRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _scalar(self, query, default=0):
        return (await self.db.execute(query)).scalar() or default

    # ── Active users (raw rows for service-side bucketing) ───────────────────

    async def login_hours(self, day: date) -> list:
        return (
            await self.db.execute(
                select(
                    User.id,
                    func.extract("hour", User.last_login_at).label("hour"),
                ).where(cast(User.last_login_at, Date) == day)
            )
        ).all()

    async def attempt_hours(self, day: date) -> list:
        return (
            await self.db.execute(
                select(
                    DictationAttempt.user_id,
                    func.extract("hour", DictationAttempt.updated_at).label("hour"),
                ).where(cast(DictationAttempt.updated_at, Date) == day)
            )
        ).all()

    async def login_days(self, start: date) -> list:
        return (
            await self.db.execute(
                select(
                    User.id,
                    cast(User.last_login_at, Date).label("day"),
                ).where(cast(User.last_login_at, Date) >= start)
            )
        ).all()

    async def attempt_days(self, start: date) -> list:
        return (
            await self.db.execute(
                select(
                    DictationAttempt.user_id,
                    cast(DictationAttempt.updated_at, Date).label("day"),
                ).where(cast(DictationAttempt.updated_at, Date) >= start)
            )
        ).all()

    # ── Traffic: new users ───────────────────────────────────────────────────

    async def new_users_by_hour(self, day: date) -> list:
        return (
            await self.db.execute(
                select(
                    func.extract("hour", User.created_at).label("hour"),
                    func.count(User.id).label("new_users"),
                )
                .where(cast(User.created_at, Date) == day)
                .group_by("hour")
            )
        ).all()

    async def new_users_by_day(self, start: date) -> list:
        return (
            await self.db.execute(
                select(
                    cast(User.created_at, Date).label("day"),
                    func.count(User.id).label("new_users"),
                )
                .where(cast(User.created_at, Date) >= start)
                .group_by("day")
            )
        ).all()

    # ── Study hours ──────────────────────────────────────────────────────────

    async def study_seconds_by_hour(self, day: date) -> list:
        return (
            await self.db.execute(
                select(
                    func.extract("hour", DictationAttempt.updated_at).label("hour"),
                    func.coalesce(func.sum(DictationAttempt.duration_seconds), 0).label(
                        "total_seconds"
                    ),
                    func.count(func.distinct(DictationAttempt.user_id)).label("active_users"),
                )
                .where(
                    DictationAttempt.status == "completed",
                    cast(DictationAttempt.updated_at, Date) == day,
                )
                .group_by("hour")
            )
        ).all()

    async def study_seconds_by_day(self, start: date) -> list:
        return (
            await self.db.execute(
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

    # ── Top learners ─────────────────────────────────────────────────────────

    async def top_learners(self, start: date, limit: int) -> list:
        attempts_subquery = (
            select(
                DictationAttempt.user_id,
                func.coalesce(func.sum(DictationAttempt.duration_seconds), 0).label(
                    "study_seconds"
                ),
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
        return (
            await self.db.execute(
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

    # ── Content health ───────────────────────────────────────────────────────

    async def video_status_counts(self) -> list:
        return (
            await self.db.execute(
                select(Video.transcription_status, func.count(Video.id)).group_by(
                    Video.transcription_status
                )
            )
        ).all()

    async def video_level_counts(self) -> list:
        return (
            await self.db.execute(
                select(Video.level, func.count(Video.id))
                .where(Video.level.is_not(None))
                .group_by(Video.level)
                .order_by(Video.level)
            )
        ).all()

    async def total_videos(self) -> int:
        return await self._scalar(select(func.count()).select_from(Video))

    async def curated_videos(self) -> int:
        return await self._scalar(select(func.count()).where(Video.is_curated.is_(True)))

    # ── Engagement ───────────────────────────────────────────────────────────

    async def count_attempts_since(self, start: date) -> int:
        return await self._scalar(
            select(func.count()).where(cast(DictationAttempt.updated_at, Date) >= start)
        )

    async def count_completed_since(self, start: date) -> int:
        return await self._scalar(
            select(func.count()).where(
                DictationAttempt.status == "completed",
                cast(DictationAttempt.updated_at, Date) >= start,
            )
        )

    async def sum_completed_duration_since(self, start: date) -> int:
        return await self._scalar(
            select(func.coalesce(func.sum(DictationAttempt.duration_seconds), 0)).where(
                DictationAttempt.status == "completed",
                cast(DictationAttempt.updated_at, Date) >= start,
            )
        )

    async def count_active_users_since(self, start: date) -> int:
        return await self._scalar(
            select(func.count(func.distinct(DictationAttempt.user_id))).where(
                cast(DictationAttempt.updated_at, Date) >= start,
            )
        )

    async def count_repeat_users_since(self, start: date) -> int:
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
        return await self._scalar(select(func.count()).select_from(repeat_subquery))

    async def count_saved_words_since(self, start: date) -> int:
        return await self._scalar(
            select(func.count()).where(
                SavedWord.deleted_at.is_(None),
                cast(SavedWord.created_at, Date) >= start,
            )
        )

    # ── Recent activity ──────────────────────────────────────────────────────

    async def recent_signups(self, limit: int) -> list:
        return (
            await self.db.execute(
                select(User.id, User.display_name, User.created_at)
                .order_by(User.created_at.desc())
                .limit(limit)
            )
        ).all()

    async def recent_logins(self, limit: int) -> list:
        return (
            await self.db.execute(
                select(User.id, User.display_name, User.last_login_at)
                .where(User.last_login_at.is_not(None))
                .order_by(User.last_login_at.desc())
                .limit(limit)
            )
        ).all()

    async def recent_sessions(self, limit: int) -> list:
        return (
            await self.db.execute(
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

    async def recent_videos(self, limit: int) -> list:
        return (
            await self.db.execute(
                select(Video.id, Video.title, Video.created_at, User.display_name)
                .outerjoin(User, Video.created_by == User.id)
                .order_by(Video.created_at.desc())
                .limit(limit)
            )
        ).all()

    async def recent_failed_transcriptions(self, limit: int) -> list:
        return (
            await self.db.execute(
                select(Video.id, Video.title, Video.created_at, Video.transcription_error)
                .where(Video.transcription_status == "failed")
                .order_by(Video.created_at.desc())
                .limit(limit)
            )
        ).all()