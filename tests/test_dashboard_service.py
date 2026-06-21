"""Unit tests for dashboard helpers and provider-free service paths."""

from datetime import datetime

import pytest

from app.models.dictation import DictationAttempt
from app.models.user import User
from app.services.dashboard_service import DashboardService, _count_to_level


def _user():
    return User(id="u1", email="u@e.com", display_name="U", preferred_language="en")


def test_count_to_level_buckets():
    assert _count_to_level(0) == 0
    assert _count_to_level(1) == 1
    assert _count_to_level(3) == 2
    assert _count_to_level(5) == 3
    assert _count_to_level(99) == 4


class FakeDashboardRepository:
    def __init__(self):
        self.weak_rows = []
        self.history = []

    async def weak_word_summaries(self, user_id):
        return self.weak_rows

    async def history_rows(self, user_id, limit, offset):
        return self.history


@pytest.mark.asyncio
async def test_weak_words_aggregates_counts_and_last_seen():
    repo = FakeDashboardRepository()
    t1 = datetime(2026, 1, 1)
    t2 = datetime(2026, 2, 1)
    repo.weak_rows = [
        ({"top_words": [{"word": "cat", "count": 2}, {"word": "dog", "count": 1}]}, t1),
        ({"top_words": [{"word": "cat", "count": 3}]}, t2),
    ]
    service = DashboardService(repo)

    result = await service.get_weak_words(_user(), limit=10)

    # "cat" aggregated across both rows (2 + 3) → ranked first.
    assert result[0]["word"] == "cat"
    assert result[0]["count"] == 5
    assert result[0]["last_seen_at"] == t2.isoformat()
    assert result[1] == {"word": "dog", "count": 1, "last_seen_at": t1.isoformat()}


@pytest.mark.asyncio
async def test_weak_words_respects_limit():
    repo = FakeDashboardRepository()
    repo.weak_rows = [
        ({"top_words": [{"word": f"w{i}", "count": i}]}, datetime(2026, 1, 1)) for i in range(1, 6)
    ]
    service = DashboardService(repo)

    result = await service.get_weak_words(_user(), limit=2)
    assert len(result) == 2
    # Highest counts first.
    assert [r["word"] for r in result] == ["w5", "w4"]


@pytest.mark.asyncio
async def test_history_formats_progress_and_score():
    repo = FakeDashboardRepository()
    attempt = DictationAttempt(
        id="a1", user_id="u1", video_id="v1", status="completed",
        score=0.823, total_sentences=10, current_sentence_index=10,
        completed_at=datetime(2026, 1, 2), updated_at=datetime(2026, 1, 2),
    )
    repo.history = [(attempt, "My Video", "thumb.jpg")]
    service = DashboardService(repo)

    entries = await service.get_history(_user(), limit=20, offset=0)

    assert len(entries) == 1
    e = entries[0]
    assert e.video_title == "My Video"
    assert e.progress_str == "10/10"
    assert e.score == 82.3
    assert e.type == "dictation"