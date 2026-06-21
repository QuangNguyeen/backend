"""Unit tests for admin analytics helpers and provider-free admin service paths."""

from datetime import date, datetime, timedelta

import pytest

from app.core.exceptions import BadRequestError, NotFoundError
from app.models.user import User
from app.schemas.admin import AdminPatchUserRequest
from app.services.admin_analytics_service import (
    _add_active_user_bucket,
    _day_series,
    _hour_point_date,
    _range_start,
)
from app.services.admin_user_service import AdminUserService

# ── Analytics pure helpers ──────────────────────────────────────────────────


def test_range_start_known_ranges():
    today = date.today()
    assert _range_start("1d") == today
    assert _range_start("7d") == today - timedelta(days=6)
    assert _range_start("30d") == today - timedelta(days=29)
    # Unknown range falls back to 7d.
    assert _range_start("bogus") == today - timedelta(days=6)


def test_day_series_is_inclusive_contiguous():
    start = date(2026, 1, 1)
    end = date(2026, 1, 4)
    assert _day_series(start, end) == [
        date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 4),
    ]


def test_hour_point_date_format():
    assert _hour_point_date(date(2026, 1, 5), 9) == "2026-01-05T09:00:00"


def test_add_active_user_bucket_skips_empty():
    buckets: dict = {}
    _add_active_user_bucket(buckets, None, "u1")  # no bucket
    _add_active_user_bucket(buckets, 3, None)  # no user
    _add_active_user_bucket(buckets, 3, "u1")
    _add_active_user_bucket(buckets, 3, "u1")  # dedup
    _add_active_user_bucket(buckets, 3, "u2")
    assert buckets == {3: {"u1", "u2"}}


# ── Admin user service (fake repo) ──────────────────────────────────────────


class FakeAdminUserRepository:
    def __init__(self):
        self.users: dict[str, User] = {}
        self.email_taken = False
        self.committed = 0

    async def get_by_id(self, user_id):
        return self.users.get(user_id)

    async def email_belongs_to_other(self, email, user_id):
        return self.email_taken

    async def count_completed_attempts(self, user_id):
        return 0

    async def average_sentence_score(self, user_id):
        return 0.0

    async def count_saved_words(self, user_id):
        return 0

    async def active_attempt_dates(self, user_id):
        return set()

    async def commit(self):
        self.committed += 1

    async def refresh(self, instance):
        pass


def _admin():
    return User(
        id="admin1", email="a@e.com", display_name="A", preferred_language="en",
        is_admin=True, is_active=True, streak_days=0,
        created_at=datetime(2026, 1, 1), last_login_at=None,
    )


def _target(**kw):
    defaults = dict(
        id="u2", email="user@example.com", display_name="U", preferred_language="en",
        is_admin=False, is_active=True, streak_days=0,
        created_at=datetime(2026, 1, 1), last_login_at=None,
    )
    defaults.update(kw)
    return User(**defaults)


@pytest.mark.asyncio
async def test_get_user_detail_not_found():
    service = AdminUserService(FakeAdminUserRepository())
    with pytest.raises(NotFoundError):
        await service.get_user_detail("ghost")


@pytest.mark.asyncio
async def test_patch_user_cannot_revoke_own_admin():
    repo = FakeAdminUserRepository()
    admin = _admin()
    repo.users["admin1"] = admin
    service = AdminUserService(repo)
    with pytest.raises(BadRequestError):
        await service.patch_user(admin, "admin1", AdminPatchUserRequest(is_admin=False))


@pytest.mark.asyncio
async def test_patch_user_cannot_deactivate_self():
    repo = FakeAdminUserRepository()
    admin = _admin()
    repo.users["admin1"] = admin
    service = AdminUserService(repo)
    with pytest.raises(BadRequestError):
        await service.patch_user(admin, "admin1", AdminPatchUserRequest(is_active=False))


@pytest.mark.asyncio
async def test_patch_user_rejects_taken_email():
    repo = FakeAdminUserRepository()
    repo.users["u2"] = _target(email="old@example.com")
    repo.email_taken = True
    service = AdminUserService(repo)
    with pytest.raises(BadRequestError):
        await service.patch_user(_admin(), "u2", AdminPatchUserRequest(email="new@example.com"))


@pytest.mark.asyncio
async def test_patch_user_applies_role_change():
    repo = FakeAdminUserRepository()
    target = _target(is_admin=False)
    repo.users["u2"] = target
    service = AdminUserService(repo)

    await service.patch_user(_admin(), "u2", AdminPatchUserRequest(is_admin=True))

    assert target.is_admin is True
    assert repo.committed == 1