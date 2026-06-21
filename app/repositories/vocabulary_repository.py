"""Database access for saved words, the global word cache, and import jobs."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.import_job import ImportJob
from app.models.vocabulary import SavedWord
from app.models.word_cache import WordCache


class VocabularyRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Word cache ──────────────────────────────────────────────────────────

    async def get_cached_word(self, word: str) -> WordCache | None:
        result = await self.db.execute(
            select(WordCache)
            .where(WordCache.word == word, WordCache.vietnamese_meaning.isnot(None))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_cached_words(self, words: list[str]) -> dict[str, WordCache]:
        result = await self.db.execute(
            select(WordCache).where(
                WordCache.word.in_(words),
                WordCache.vietnamese_meaning.isnot(None),
            )
        )
        return {row.word: row for row in result.scalars().all()}

    async def upsert_word_cache(self, values: dict) -> None:
        """Insert a global cache row, ignoring conflicts on (word, context_hash).

        Does not commit — the caller controls the transaction boundary.
        """
        stmt = pg_insert(WordCache).values(**values).on_conflict_do_nothing(
            index_elements=["word", "context_hash"]
        )
        await self.db.execute(stmt)

    # ── Saved words ─────────────────────────────────────────────────────────

    async def saved_word_exists(self, user_id: str, lemma: str) -> bool:
        result = await self.db.execute(
            select(SavedWord.id)
            .where(
                SavedWord.user_id == user_id,
                SavedWord.word == lemma,
                SavedWord.deleted_at.is_(None),
            )
            .limit(1)
        )
        return result.scalar() is not None

    async def get_active_saved_word_by_lemma(self, user_id: str, lemma: str) -> SavedWord | None:
        result = await self.db.execute(
            select(SavedWord).where(
                SavedWord.user_id == user_id,
                SavedWord.word == lemma,
                SavedWord.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_saved_lemmas(self, user_id: str, lemmas: list[str]) -> set[str]:
        result = await self.db.execute(
            select(SavedWord.word).where(
                SavedWord.user_id == user_id,
                SavedWord.word.in_(lemmas),
                SavedWord.deleted_at.is_(None),
            )
        )
        return set(result.scalars().all())

    async def list_saved_words(
        self, user_id: str, video_id: str | None, limit: int, offset: int
    ) -> list[SavedWord]:
        query = select(SavedWord).where(
            SavedWord.user_id == user_id,
            SavedWord.deleted_at.is_(None),
        )
        if video_id:
            query = query.where(SavedWord.video_id == video_id)
        query = query.order_by(SavedWord.created_at.desc()).limit(limit).offset(offset)
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_due_cards(self, user_id: str, now: datetime, limit: int = 50) -> list[SavedWord]:
        result = await self.db.execute(
            select(SavedWord)
            .where(
                SavedWord.user_id == user_id,
                SavedWord.deleted_at.is_(None),
                SavedWord.next_review_at <= now,
            )
            .order_by(SavedWord.next_review_at)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_due(self, user_id: str, now: datetime) -> int:
        result = await self.db.execute(
            select(func.count()).where(
                SavedWord.user_id == user_id,
                SavedWord.deleted_at.is_(None),
                SavedWord.next_review_at <= now,
            )
        )
        return result.scalar() or 0

    async def get_saved_word_by_id(self, user_id: str, word_id: str) -> SavedWord | None:
        result = await self.db.execute(
            select(SavedWord).where(
                SavedWord.id == word_id,
                SavedWord.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_active_saved_word_by_id(self, user_id: str, word_id: str) -> SavedWord | None:
        result = await self.db.execute(
            select(SavedWord).where(
                SavedWord.id == word_id,
                SavedWord.user_id == user_id,
                SavedWord.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_saved_word_by_id_any_user(self, word_id: str) -> SavedWord | None:
        result = await self.db.execute(select(SavedWord).where(SavedWord.id == word_id))
        return result.scalar_one_or_none()

    async def get_all_saved_words(self, user_id: str) -> list[SavedWord]:
        result = await self.db.execute(
            select(SavedWord)
            .where(SavedWord.user_id == user_id, SavedWord.deleted_at.is_(None))
            .order_by(SavedWord.created_at.desc())
        )
        return list(result.scalars().all())

    # ── Import jobs ─────────────────────────────────────────────────────────

    async def get_import_job(self, user_id: str, job_id: str) -> ImportJob | None:
        result = await self.db.execute(
            select(ImportJob).where(
                ImportJob.id == job_id,
                ImportJob.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    # ── Transaction helpers ─────────────────────────────────────────────────

    def add(self, instance) -> None:
        self.db.add(instance)

    async def commit(self) -> None:
        await self.db.commit()

    async def refresh(self, instance) -> None:
        await self.db.refresh(instance)