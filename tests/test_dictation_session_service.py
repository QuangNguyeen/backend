"""Unit tests for DictationService methods that need no external providers."""

from datetime import UTC, datetime

import pytest

from app.core.exceptions import NotFoundError
from app.models.dictation import DictationAttempt, DictationSentence
from app.models.user import User
from app.services.dictation_session_service import DictationService


class FakeDictationRepository:
    def __init__(self):
        self.attempts: dict[str, DictationAttempt] = {}
        self.sentences: dict[str, list[DictationSentence]] = {}
        self.summary_row = (0, 0, 0.0)
        self.attempt_count = 0
        self.history_rows: list = []
        self.committed = 0

    async def get_attempt(self, session_id, user_id):
        return self.attempts.get(session_id)

    async def get_sentences_by_attempt(self, attempt_id):
        return self.sentences.get(attempt_id, [])

    async def count_attempts(self, **filters):
        return self.attempt_count

    async def get_attempts_with_video(self, **kwargs):
        return self.history_rows

    async def get_attempts_summary(self, **filters):
        return self.summary_row

    async def commit(self):
        self.committed += 1


def _user():
    return User(id="u1", email="u@e.com", display_name="U", preferred_language="en")


@pytest.mark.asyncio
async def test_complete_session_not_found_raises():
    service = DictationService(FakeDictationRepository())
    with pytest.raises(NotFoundError):
        await service.complete_session(_user(), "missing")


@pytest.mark.asyncio
async def test_complete_session_idempotent_when_already_completed(monkeypatch):
    published = []

    async def fake_publish(*args):
        published.append(args)

    monkeypatch.setattr(
        "app.services.dictation_session_service.publish_user_recommendation_event",
        fake_publish,
    )
    repo = FakeDictationRepository()
    repo.attempts["s1"] = DictationAttempt(
        id="s1", user_id="u1", video_id="v1", status="completed", score=0.873
    )
    service = DictationService(repo)

    result = await service.complete_session(_user(), "s1")

    assert result == {"status": "completed", "score": 87.3}
    assert repo.committed == 0  # no write on the idempotent path
    assert published == []


@pytest.mark.asyncio
async def test_complete_session_aggregates_mean_score(monkeypatch):
    published = []

    async def fake_publish(*args):
        published.append(args)

    monkeypatch.setattr(
        "app.services.dictation_session_service.publish_user_recommendation_event",
        fake_publish,
    )
    repo = FakeDictationRepository()
    attempt = DictationAttempt(
        id="s1",
        user_id="u1",
        video_id="v1",
        status="in_progress",
        total_sentences=2,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    repo.attempts["s1"] = attempt
    repo.sentences["s1"] = [
        DictationSentence(attempt_id="s1", sentence_index=0, score=1.0, word_diff=[]),
        DictationSentence(attempt_id="s1", sentence_index=1, score=0.5, word_diff=[]),
    ]
    service = DictationService(repo)

    result = await service.complete_session(_user(), "s1")

    assert attempt.status == "completed"
    assert attempt.score == 0.75
    assert result == {"status": "completed", "score": 75.0}
    assert repo.committed == 1
    assert published == [("u1", "attempt_completed", "v1")]


@pytest.mark.asyncio
async def test_attempts_summary_scales_average_score():
    repo = FakeDictationRepository()
    repo.summary_row = (5, 1200, 0.824)
    service = DictationService(repo)

    summary = await service.get_attempts_summary(_user(), None, None, None, None)

    assert summary["total_sessions"] == 5
    assert summary["total_duration_seconds"] == 1200
    assert summary["average_score"] == 82.4


@pytest.mark.asyncio
async def test_history_empty_result_pagination():
    repo = FakeDictationRepository()
    repo.attempt_count = 0
    repo.history_rows = []
    service = DictationService(repo)

    resp = await service.get_history(_user(), "completed", 1, None, None, None, None)

    assert resp.total == 0
    assert resp.items == []
    assert resp.total_pages == 1  # max(1, ceil(0/20))
