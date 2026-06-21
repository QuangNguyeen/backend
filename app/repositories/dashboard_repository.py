"""Database access for dashboard aggregates, heatmap, trends, and history."""

from datetime import date

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dictation import DictationAttempt, DictationSentence
from app.models.video import Video


class DashboardRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def count_completed_attempts(self, user_id: str) -> int:
        result = await self.db.execute(
            select(func.count()).where(
                DictationAttempt.user_id == user_id,
                DictationAttempt.status == "completed",
            )
        )
        return result.scalar() or 0

    async def count_sentences(self, user_id: str) -> int:
        result = await self.db.execute(
            select(func.count())
            .select_from(DictationSentence)
            .join(DictationAttempt)
            .where(DictationAttempt.user_id == user_id)
        )
        return result.scalar() or 0

    async def average_sentence_score(self, user_id: str) -> float:
        result = await self.db.execute(
            select(func.avg(DictationSentence.score))
            .join(DictationAttempt)
            .where(DictationAttempt.user_id == user_id)
        )
        return result.scalar() or 0.0

    async def count_distinct_videos(self, user_id: str) -> int:
        result = await self.db.execute(
            select(func.count(func.distinct(DictationAttempt.video_id))).where(
                DictationAttempt.user_id == user_id
            )
        )
        return result.scalar() or 0

    async def sum_completed_duration(self, user_id: str) -> int:
        result = await self.db.execute(
            select(func.sum(DictationAttempt.duration_seconds)).where(
                DictationAttempt.user_id == user_id,
                DictationAttempt.status == "completed",
            )
        )
        return result.scalar() or 0

    async def heatmap_day_counts(self, user_id: str, year_start: date) -> dict[date, int]:
        result = await self.db.execute(
            select(
                cast(DictationAttempt.updated_at, Date).label("day"),
                func.count().label("cnt"),
            )
            .where(
                DictationAttempt.user_id == user_id,
                cast(DictationAttempt.updated_at, Date) >= year_start,
            )
            .group_by("day")
        )
        return {row.day: row.cnt for row in result.all() if row.day is not None}

    async def accuracy_trend_rows(self, user_id: str, limit: int = 20) -> list:
        result = await self.db.execute(
            select(
                DictationAttempt.completed_at,
                DictationAttempt.updated_at,
                DictationAttempt.score,
            )
            .where(
                DictationAttempt.user_id == user_id,
                DictationAttempt.status == "completed",
                DictationAttempt.score.is_not(None),
            )
            .order_by(DictationAttempt.completed_at.desc().nullslast())
            .limit(limit)
        )
        return result.all()

    async def weak_word_summaries(self, user_id: str) -> list:
        result = await self.db.execute(
            select(DictationAttempt.error_summary, DictationAttempt.completed_at).where(
                DictationAttempt.user_id == user_id,
                DictationAttempt.status == "completed",
                DictationAttempt.error_summary.is_not(None),
            )
        )
        return result.all()

    async def history_rows(self, user_id: str, limit: int, offset: int) -> list:
        result = await self.db.execute(
            select(DictationAttempt, Video.title, Video.thumbnail_url)
            .join(Video, DictationAttempt.video_id == Video.id)
            .where(DictationAttempt.user_id == user_id)
            .order_by(DictationAttempt.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return result.all()