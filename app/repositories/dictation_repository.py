"""Database access for dictation attempts, sentences, and history."""

from datetime import datetime

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dictation import DictationAttempt, DictationSentence
from app.models.video import Transcript, UserPracticeVideo, Video


class DictationRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Transcripts ─────────────────────────────────────────────────────────

    async def get_transcripts_by_video(self, video_id: str) -> list[Transcript]:
        result = await self.db.execute(select(Transcript).where(Transcript.video_id == video_id))
        return list(result.scalars().all())

    async def get_video(self, video_id: str) -> Video | None:
        result = await self.db.execute(select(Video).where(Video.id == video_id))
        return result.scalar_one_or_none()

    async def has_user_practice(self, user_id: str, video_id: str) -> bool:
        result = await self.db.execute(
            select(UserPracticeVideo.id)
            .where(
                UserPracticeVideo.user_id == user_id,
                UserPracticeVideo.video_id == video_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def get_transcripts_ordered(self, video_id: str) -> list[Transcript]:
        result = await self.db.execute(
            select(Transcript).where(Transcript.video_id == video_id).order_by(Transcript.index)
        )
        return list(result.scalars().all())

    async def get_transcript_by_position(self, video_id: str, offset: int) -> Transcript | None:
        result = await self.db.execute(
            select(Transcript)
            .where(Transcript.video_id == video_id)
            .order_by(Transcript.index)
            .offset(offset)
            .limit(1)
        )
        return result.scalar_one_or_none()

    # ── Attempts ────────────────────────────────────────────────────────────

    async def get_attempt_by_user_video(
        self, user_id: str, video_id: str
    ) -> DictationAttempt | None:
        result = await self.db.execute(
            select(DictationAttempt).where(
                DictationAttempt.user_id == user_id,
                DictationAttempt.video_id == video_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_attempt(self, session_id: str, user_id: str) -> DictationAttempt | None:
        result = await self.db.execute(
            select(DictationAttempt).where(
                DictationAttempt.id == session_id,
                DictationAttempt.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    # ── Sentences ───────────────────────────────────────────────────────────

    async def get_sentences_by_attempt(self, attempt_id: str) -> list[DictationSentence]:
        result = await self.db.execute(
            select(DictationSentence).where(DictationSentence.attempt_id == attempt_id)
        )
        return list(result.scalars().all())

    async def get_sentences_by_attempt_ordered(self, attempt_id: str) -> list[DictationSentence]:
        result = await self.db.execute(
            select(DictationSentence)
            .where(DictationSentence.attempt_id == attempt_id)
            .order_by(DictationSentence.sentence_index)
        )
        return list(result.scalars().all())

    async def get_sentence(self, attempt_id: str, sentence_index: int) -> DictationSentence | None:
        result = await self.db.execute(
            select(DictationSentence).where(
                DictationSentence.attempt_id == attempt_id,
                DictationSentence.sentence_index == sentence_index,
            )
        )
        return result.scalar_one_or_none()

    async def delete_sentences_for_attempt(self, attempt_id: str) -> None:
        result = await self.db.execute(
            select(DictationSentence).where(DictationSentence.attempt_id == attempt_id)
        )
        for sentence in result.scalars().all():
            await self.db.delete(sentence)

    # ── History & summary ───────────────────────────────────────────────────

    def _attempt_filters(
        self,
        *,
        user_id: str,
        status: str,
        video_id: str | None,
        practice_mode: str | None,
        from_dt: datetime | None,
        to_dt: datetime | None,
    ):
        filters = [
            DictationAttempt.user_id == user_id,
            DictationAttempt.status == status,
        ]
        if video_id:
            filters.append(DictationAttempt.video_id == video_id)
        if practice_mode:
            filters.append(DictationAttempt.practice_mode == practice_mode)
        if from_dt:
            filters.append(DictationAttempt.created_at >= from_dt)
        if to_dt:
            filters.append(DictationAttempt.created_at <= to_dt)
        return filters

    async def count_attempts(self, **filters) -> int:
        clauses = self._attempt_filters(**filters)
        result = await self.db.execute(
            select(sa_func.count()).select_from(DictationAttempt).where(*clauses)
        )
        return result.scalar() or 0

    async def get_attempts_with_video(
        self, *, order_by_completed: bool, page: int, page_size: int, **filters
    ) -> list:
        clauses = self._attempt_filters(**filters)
        order_col = (
            DictationAttempt.completed_at.desc()
            if order_by_completed
            else DictationAttempt.updated_at.desc()
        )
        query = (
            select(DictationAttempt, Video.title, Video.thumbnail_url)
            .join(Video, DictationAttempt.video_id == Video.id)
            .where(*clauses)
            .order_by(order_col)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self.db.execute(query)
        return result.all()

    async def get_attempts_summary(self, **filters):
        clauses = self._attempt_filters(**filters)
        result = await self.db.execute(
            select(
                sa_func.count(),
                sa_func.coalesce(sa_func.sum(DictationAttempt.duration_seconds), 0),
                sa_func.coalesce(sa_func.avg(DictationAttempt.score), 0),
            ).where(*clauses)
        )
        return result.one()

    # ── Transaction helpers ─────────────────────────────────────────────────

    def add(self, instance) -> None:
        self.db.add(instance)

    async def commit(self) -> None:
        await self.db.commit()

    async def refresh(self, instance) -> None:
        await self.db.refresh(instance)
