"""Unit tests for UserService — exercised against a fake repository (no DB)."""

from datetime import datetime

import pytest

from app.core.exceptions import BadRequestError
from app.core.security import hash_password
from app.models.user import User
from app.schemas.user import ChangePasswordRequest, UserPreferences, UserUpdateRequest
from app.services.user_service import UserService


class FakeUserRepository:
    """In-memory stand-in for UserRepository."""

    def __init__(self, *, attempts=0, avg=0.0, vocab=0, active_dates=None):
        self._attempts = attempts
        self._avg = avg
        self._vocab = vocab
        self._active_dates = active_dates or set()
        self.committed = 0
        self.refreshed = 0

    async def count_completed_attempts(self, user_id):
        return self._attempts

    async def average_sentence_score(self, user_id):
        return self._avg

    async def count_saved_words(self, user_id):
        return self._vocab

    async def active_attempt_dates(self, user_id):
        return self._active_dates

    async def commit(self):
        self.committed += 1

    async def refresh(self, instance):
        self.refreshed += 1


def _make_user(**overrides) -> User:
    defaults = dict(
        id="user-1",
        email="user@example.com",
        display_name="User",
        preferred_language="en",
        preferences={"audio_speed": 1.0, "theme": "dark"},
        password_hash=None,
        is_active=True,
        is_admin=False,
        created_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    defaults.update(overrides)
    return User(**defaults)


@pytest.mark.asyncio
async def test_get_profile_aggregates_and_scales_score():
    repo = FakeUserRepository(attempts=3, avg=0.825, vocab=12)
    service = UserService(repo)

    profile = await service.get_profile(_make_user())

    assert profile.id == "user-1"
    assert profile.stats.total_attempts == 3
    # average_score is the mean sentence score scaled to 0–100, one decimal.
    assert profile.stats.average_score == 82.5
    assert profile.stats.total_vocabulary == 12
    assert profile.preferences.theme == "dark"


@pytest.mark.asyncio
async def test_get_profile_coerces_missing_preferences_to_defaults():
    repo = FakeUserRepository()
    service = UserService(repo)

    profile = await service.get_profile(_make_user(preferences=None))

    assert profile.preferences == UserPreferences()  # defaults


@pytest.mark.asyncio
async def test_update_profile_merges_preferences_shallowly():
    repo = FakeUserRepository()
    service = UserService(repo)
    user = _make_user(preferences={"audio_speed": 1.5, "theme": "light"})

    body = UserUpdateRequest(display_name="New Name", preferences=UserPreferences(theme="dark"))
    profile = await service.update_profile(user, body)

    assert user.display_name == "New Name"
    # theme overwritten; audio_speed default from the supplied preferences object.
    assert user.preferences["theme"] == "dark"
    assert repo.committed == 1
    assert repo.refreshed == 1
    assert profile.display_name == "New Name"


@pytest.mark.asyncio
async def test_change_password_rejects_social_login_account():
    service = UserService(FakeUserRepository())
    body = ChangePasswordRequest(current_password="x", new_password="newpass")

    with pytest.raises(BadRequestError) as exc:
        await service.change_password(_make_user(password_hash=None), body)
    assert "social login" in exc.value.detail


@pytest.mark.asyncio
async def test_change_password_rejects_wrong_current_password():
    service = UserService(FakeUserRepository())
    user = _make_user(password_hash=hash_password("correct-pass"))
    body = ChangePasswordRequest(current_password="wrong-pass", new_password="newpass")

    with pytest.raises(BadRequestError) as exc:
        await service.change_password(user, body)
    assert exc.value.detail == "Current password is incorrect"


@pytest.mark.asyncio
async def test_change_password_rejects_too_short_new_password():
    service = UserService(FakeUserRepository())
    user = _make_user(password_hash=hash_password("correct-pass"))
    body = ChangePasswordRequest(current_password="correct-pass", new_password="123")

    with pytest.raises(BadRequestError) as exc:
        await service.change_password(user, body)
    assert "at least 6 characters" in exc.value.detail


@pytest.mark.asyncio
async def test_change_password_success_updates_hash_and_commits():
    repo = FakeUserRepository()
    service = UserService(repo)
    user = _make_user(password_hash=hash_password("correct-pass"))
    old_hash = user.password_hash

    body = ChangePasswordRequest(current_password="correct-pass", new_password="brand-new-pass")
    await service.change_password(user, body)

    assert user.password_hash != old_hash
    assert repo.committed == 1