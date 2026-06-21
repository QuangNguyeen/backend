"""Business logic for the learner dashboard: stats, heatmap, trends, weak words."""

from datetime import date, timedelta

from app.models.user import User
from app.repositories.dashboard_repository import DashboardRepository
from app.schemas.dictation import (
    AccuracyPoint,
    DashboardFullResponse,
    DashboardStatsResponse,
    HeatmapDay,
    HistoryEntryResponse,
)
from app.services.stats_service import compute_streaks


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


class DashboardService:
    def __init__(self, repo: DashboardRepository):
        self.repo = repo

    async def get_dashboard(self, user: User) -> DashboardFullResponse:
        today = date.today()
        year_start = date(today.year, 1, 1)

        # ── Aggregate stats ──────────────────────────────────────────────────
        total_sessions = await self.repo.count_completed_attempts(user.id)
        total_sentences = await self.repo.count_sentences(user.id)
        avg_accuracy = await self.repo.average_sentence_score(user.id)
        total_videos = await self.repo.count_distinct_videos(user.id)
        total_duration_sec = await self.repo.sum_completed_duration(user.id)

        # ── Heatmap (contiguous days from year start) ────────────────────────
        day_counts = await self.repo.heatmap_day_counts(user.id, year_start)
        heatmap: list[HeatmapDay] = []
        d = year_start
        while d <= today:
            count = day_counts.get(d, 0)
            heatmap.append(
                HeatmapDay(date=d.isoformat(), count=count, level=_count_to_level(count))
            )
            d += timedelta(days=1)

        # ── Streaks ──────────────────────────────────────────────────────────
        active_dates = set(day_counts.keys())
        current_streak, longest_streak = compute_streaks(active_dates, today)

        # ── Accuracy trend (last 20 completed, chronological) ────────────────
        trend_rows = list(reversed(await self.repo.accuracy_trend_rows(user.id, limit=20)))
        accuracy_trend = []
        for i, row in enumerate(trend_rows):
            ts = row.completed_at or row.updated_at
            label = ts.strftime("%b %d") if ts else f"Session {i + 1}"
            score_pct = round((row.score or 0) * 100, 1)
            accuracy_trend.append(AccuracyPoint(date=label, score=score_pct, accuracy=score_pct))

        return DashboardFullResponse(
            stats=DashboardStatsResponse(
                total_sessions=total_sessions,
                total_sentences=total_sentences,
                total_time_minutes=round(total_duration_sec / 60, 1),
                average_accuracy=round(avg_accuracy * 100, 1),
                total_videos=total_videos,
                current_streak=current_streak,
                longest_streak=longest_streak,
            ),
            heatmap=heatmap,
            accuracy_trend=accuracy_trend,
        )

    async def get_weak_words(self, user: User, limit: int) -> list[dict]:
        rows = await self.repo.weak_word_summaries(user.id)

        word_agg: dict[str, dict] = {}
        for error_summary, completed_at in rows:
            if not error_summary or "top_words" not in error_summary:
                continue
            for entry in error_summary["top_words"]:
                w = entry["word"]
                c = entry["count"]
                if w not in word_agg:
                    word_agg[w] = {"word": w, "count": 0, "last_seen_at": None}
                word_agg[w]["count"] += c
                ts = completed_at.isoformat() if completed_at else None
                if ts and (
                    word_agg[w]["last_seen_at"] is None or ts > word_agg[w]["last_seen_at"]
                ):
                    word_agg[w]["last_seen_at"] = ts

        return sorted(word_agg.values(), key=lambda x: x["count"], reverse=True)[:limit]

    async def get_history(
        self, user: User, limit: int, offset: int
    ) -> list[HistoryEntryResponse]:
        rows = await self.repo.history_rows(user.id, limit, offset)

        entries = []
        for attempt, video_title, thumbnail in rows:
            total = attempt.total_sentences or 0
            current = attempt.current_sentence_index or 0
            entries.append(
                HistoryEntryResponse(
                    id=attempt.id,
                    video_title=video_title,
                    video_thumbnail=thumbnail or "",
                    type="dictation",
                    status=attempt.status,
                    score=round((attempt.score or 0) * 100, 1)
                    if attempt.score is not None
                    else None,
                    progress_str=f"{current}/{total}",
                    completed_at=attempt.completed_at.isoformat() if attempt.completed_at else None,
                    updated_at=attempt.updated_at.isoformat(),
                )
            )

        return entries