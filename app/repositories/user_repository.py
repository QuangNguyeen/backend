"""Database access for users and their aggregated learning stats."""

from datetime import date

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dictation import DictationAttempt, DictationSentence
from app.models.user import User
from app.models.vocabulary import SavedWord


class UserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, user_id: str) -> User | None:
        result = await self.db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def count_completed_attempts(self, user_id: str) -> int:
        result = await self.db.execute(
            select(func.count()).where(
                DictationAttempt.user_id == user_id,
                DictationAttempt.status == "completed",
            )
        )
        return result.scalar() or 0

    async def average_sentence_score(self, user_id: str) -> float:
        result = await self.db.execute(
            select(func.avg(DictationSentence.score))
            .join(DictationAttempt)
            .where(DictationAttempt.user_id == user_id)
        )
        return result.scalar() or 0.0

    async def count_saved_words(self, user_id: str) -> int:
        result = await self.db.execute(
            select(func.count()).where(
                SavedWord.user_id == user_id,
                SavedWord.deleted_at.is_(None),
            )
        )
        return result.scalar() or 0

    async def active_attempt_dates(self, user_id: str) -> set[date]:
        rows = (
            await self.db.execute(
                select(cast(DictationAttempt.updated_at, Date))
                .where(DictationAttempt.user_id == user_id)
                .group_by(cast(DictationAttempt.updated_at, Date))
            )
        ).all()
        return {row[0] for row in rows if row[0] is not None}

    async def commit(self) -> None:
        await self.db.commit()

    async def refresh(self, instance) -> None:
        await self.db.refresh(instance)