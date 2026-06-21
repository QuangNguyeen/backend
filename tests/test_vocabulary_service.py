"""Unit tests for VocabularyService logic that needs no external providers."""

from datetime import UTC, datetime

import pytest

from app.core.exceptions import NotFoundError
from app.models.import_job import ImportJob
from app.models.user import User
from app.models.vocabulary import SavedWord
from app.schemas.vocabulary import ReviewRequest, UpdateWordRequest
from app.services.vocabulary_service import VocabularyService


class FakeVocabRepository:
    def __init__(self):
        self.saved_by_id: dict[str, SavedWord] = {}
        self.active_saved_by_id: dict[str, SavedWord] = {}
        self.import_jobs: dict[str, ImportJob] = {}
        self.committed = 0
        self.refreshed = 0

    async def get_saved_word_by_id(self, user_id, word_id):
        return self.saved_by_id.get(word_id)

    async def get_active_saved_word_by_id(self, user_id, word_id):
        return self.active_saved_by_id.get(word_id)

    async def get_import_job(self, user_id, job_id):
        return self.import_jobs.get(job_id)

    async def commit(self):
        self.committed += 1

    async def refresh(self, instance):
        self.refreshed += 1


def _user():
    return User(id="u1", email="u@e.com", display_name="U", preferred_language="en")


def _saved_word(**overrides):
    defaults = dict(
        id="w1",
        user_id="u1",
        word="run",
        repetitions=2,
        ease_factor=2.5,
        interval_days=6,
        next_review_at=datetime.now(UTC),
        meaning="chạy",
        note=None,
        deleted_at=None,
    )
    defaults.update(overrides)
    return SavedWord(**defaults)


@pytest.mark.asyncio
async def test_review_word_not_found_raises():
    service = VocabularyService(FakeVocabRepository())
    with pytest.raises(NotFoundError):
        await service.review_word(_user(), "missing", ReviewRequest(quality=3))


@pytest.mark.asyncio
async def test_review_word_advances_srs_and_commits():
    repo = FakeVocabRepository()
    word = _saved_word()
    repo.saved_by_id["w1"] = word
    service = VocabularyService(repo)

    resp = await service.review_word(_user(), "w1", ReviewRequest(quality=5))

    assert resp.word_id == "w1"
    # A correct review advances repetitions and pushes next_review into the future.
    assert resp.repetitions == 3
    assert word.repetitions == 3
    assert word.last_reviewed_at is not None
    assert resp.next_review_at > datetime.now(UTC)
    assert repo.committed == 1


@pytest.mark.asyncio
async def test_update_word_applies_partial_fields():
    repo = FakeVocabRepository()
    word = _saved_word(meaning="old", note=None)
    repo.saved_by_id["w1"] = word
    service = VocabularyService(repo)

    await service.update_word(_user(), "w1", UpdateWordRequest(note="my note"))

    assert word.note == "my note"
    assert word.meaning == "old"  # untouched (not supplied)
    assert repo.committed == 1


@pytest.mark.asyncio
async def test_delete_word_soft_deletes():
    repo = FakeVocabRepository()
    word = _saved_word()
    repo.active_saved_by_id["w1"] = word
    service = VocabularyService(repo)

    await service.delete_word(_user(), "w1")

    assert word.deleted_at is not None
    assert repo.committed == 1


@pytest.mark.asyncio
async def test_delete_word_not_found_raises():
    service = VocabularyService(FakeVocabRepository())
    with pytest.raises(NotFoundError):
        await service.delete_word(_user(), "missing")


@pytest.mark.asyncio
async def test_import_status_progress_math():
    repo = FakeVocabRepository()
    repo.import_jobs["j1"] = ImportJob(
        id="j1",
        user_id="u1",
        status="processing",
        total_words=4,
        enriched_count=1,
        phase="meanings",
        error=None,
    )
    service = VocabularyService(repo)

    status = await service.get_import_status(_user(), "j1")

    assert status.total == 4
    assert status.enriched == 1
    assert status.progress_pct == 25  # round(1/4 * 100)


@pytest.mark.asyncio
async def test_import_status_completed_is_100pct():
    repo = FakeVocabRepository()
    repo.import_jobs["j1"] = ImportJob(
        id="j1",
        user_id="u1",
        status="completed",
        total_words=4,
        enriched_count=4,
        phase="done",
        error=None,
    )
    service = VocabularyService(repo)

    status = await service.get_import_status(_user(), "j1")
    assert status.progress_pct == 100