"""Database access for admin user management."""

from datetime import date

from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dictation import DictationAttempt, DictationSentence
from app.models.user import User
from app.models.vocabulary import SavedWord


class AdminUserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _apply_filters(self, query, *, is_admin, is_active, search):
        if is_admin is not None:
            query = query.where(User.is_admin == is_admin)
        if is_active is not None:
            query = query.where(User.is_active == is_active)
        if search:
            pattern = f"%{search.strip()}%"
            query = query.where(
                or_(User.email.ilike(pattern), User.display_name.ilike(pattern))
            )
        return query

    async def list_with_counts(
        self, *, is_admin, is_active, search, page, page_size
    ) -> tuple[list, int]:
        sessions_sub = (
            select(
                DictationAttempt.user_id,
                func.count()
                .filter(DictationAttempt.status == "completed")
                .label("total_sessions"),
            )
            .group_by(DictationAttempt.user_id)
            .subquery()
        )
        vocabulary_sub = (
            select(SavedWord.user_id, func.count().label("total_vocabulary"))
            .where(SavedWord.deleted_at.is_(None))
            .group_by(SavedWord.user_id)
            .subquery()
        )

        count_q = self._apply_filters(
            select(func.count()).select_from(User),
            is_admin=is_admin,
            is_active=is_active,
            search=search,
        )
        total = (await self.db.execute(count_q)).scalar() or 0

        query = (
            select(
                User,
                func.coalesce(sessions_sub.c.total_sessions, 0).label("total_sessions"),
                func.coalesce(vocabulary_sub.c.total_vocabulary, 0).label("total_vocabulary"),
            )
            .outerjoin(sessions_sub, User.id == sessions_sub.c.user_id)
            .outerjoin(vocabulary_sub, User.id == vocabulary_sub.c.user_id)
        )
        query = self._apply_filters(query, is_admin=is_admin, is_active=is_active, search=search)
        query = (
            query.order_by(User.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self.db.execute(query)).all()
        return rows, total

    async def get_by_id(self, user_id: str) -> User | None:
        result = await self.db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def email_belongs_to_other(self, email: str, user_id: str) -> bool:
        result = await self.db.execute(
            select(User).where(func.lower(User.email) == email, User.id != user_id)
        )
        return result.scalar_one_or_none() is not None

    # ── Per-user stats ──────────────────────────────────────────────────────

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
            .join(DictationAttempt, DictationSentence.attempt_id == DictationAttempt.id)
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