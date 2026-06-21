"""Database access for admin video management."""

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dictation import DictationAttempt, DictationSentence
from app.models.room import RoomSession
from app.models.user import User
from app.models.video import Transcript, Video

TRANSCRIPTION_STATUSES = {"pending", "processing", "ready", "failed"}


class AdminVideoRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    def _apply_filters(self, query, *, status, active, level, language, curated, search):
        if status:
            normalized = status.strip().lower()
            if normalized == "active":
                query = query.where(Video.is_active.is_(True))
            elif normalized == "inactive":
                query = query.where(Video.is_active.is_(False))
            elif normalized in TRANSCRIPTION_STATUSES:
                query = query.where(Video.transcription_status == normalized)
        if active is not None:
            query = query.where(Video.is_active == active)
        if level:
            query = query.where(Video.level == level)
        if language:
            query = query.where(Video.language == language)
        if curated is not None:
            query = query.where(Video.is_curated == curated)
        if search:
            pattern = f"%{search.strip()}%"
            query = query.where(
                or_(
                    Video.title.ilike(pattern),
                    Video.channel.ilike(pattern),
                    Video.youtube_id.ilike(pattern),
                )
            )
        return query

    async def list_with_stats(
        self, *, status, active, level, language, curated, search, page, page_size
    ) -> tuple[list, int]:
        stats_sub = (
            select(
                DictationAttempt.video_id,
                func.count().filter(DictationAttempt.status == "completed").label("play_count"),
                func.max(DictationAttempt.score).label("best_score"),
            )
            .group_by(DictationAttempt.video_id)
            .subquery()
        )

        filters = dict(
            status=status,
            active=active,
            level=level,
            language=language,
            curated=curated,
            search=search,
        )
        count_q = self._apply_filters(select(func.count()).select_from(Video), **filters)
        total = (await self.db.execute(count_q)).scalar() or 0

        query = (
            select(
                Video,
                User,
                func.coalesce(stats_sub.c.play_count, 0).label("play_count"),
                stats_sub.c.best_score,
            )
            .outerjoin(User, Video.created_by == User.id)
            .outerjoin(stats_sub, Video.id == stats_sub.c.video_id)
        )
        query = self._apply_filters(query, **filters)
        query = (
            query.order_by(Video.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self.db.execute(query)).all()
        return rows, total

    async def get_by_id(self, video_id: str) -> Video | None:
        result = await self.db.execute(select(Video).where(Video.id == video_id))
        return result.scalar_one_or_none()

    async def get_creator(self, user_id: str) -> User | None:
        result = await self.db.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_transcripts_ordered(self, video_id: str) -> list[Transcript]:
        result = await self.db.execute(
            select(Transcript).where(Transcript.video_id == video_id).order_by(Transcript.index)
        )
        return list(result.scalars().all())

    async def delete_video_and_related(self, video: Video) -> None:
        attempts = await self.db.execute(
            select(DictationAttempt).where(DictationAttempt.video_id == video.id)
        )
        for attempt in attempts.scalars().all():
            await self.db.execute(
                delete(DictationSentence).where(DictationSentence.attempt_id == attempt.id)
            )
        await self.db.execute(
            delete(DictationAttempt).where(DictationAttempt.video_id == video.id)
        )
        await self.db.execute(delete(RoomSession).where(RoomSession.video_id == video.id))
        await self.db.delete(video)

    async def commit(self) -> None:
        await self.db.commit()

    async def refresh(self, instance) -> None:
        await self.db.refresh(instance)