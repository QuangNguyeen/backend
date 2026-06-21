"""Database access for the admin stats summary."""

from datetime import date

from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dictation import DictationAttempt
from app.models.user import User
from app.models.video import Video
from app.models.vocabulary import SavedWord


class AdminStatsRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _count(self, query) -> int:
        return (await self.db.execute(query)).scalar() or 0

    async def count_users(self) -> int:
        return await self._count(select(func.count()).select_from(User))

    async def count_videos(self) -> int:
        return await self._count(select(func.count()).select_from(Video))

    async def count_sessions(self) -> int:
        return await self._count(select(func.count()).select_from(DictationAttempt))

    async def count_vocabulary(self) -> int:
        return await self._count(
            select(func.count()).where(SavedWord.deleted_at.is_(None))
        )

    async def count_transcriptions_with_status(self, status: str) -> int:
        return await self._count(
            select(func.count()).where(Video.transcription_status == status)
        )

    async def count_new_users(self, day: date) -> int:
        return await self._count(
            select(func.count()).where(cast(User.created_at, Date) == day)
        )

    async def count_active_users(self, day: date) -> int:
        return await self._count(
            select(func.count(func.distinct(User.id)))
            .select_from(User)
            .outerjoin(DictationAttempt, DictationAttempt.user_id == User.id)
            .where(
                or_(
                    cast(User.last_login_at, Date) == day,
                    cast(DictationAttempt.updated_at, Date) == day,
                )
            )
        )