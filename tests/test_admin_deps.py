"""Tests for admin-only dependency guards."""

import pytest
from fastapi import HTTPException

from app.api.deps import get_admin_user
from app.models.user import User


def _make_user(*, is_admin: bool) -> User:
    return User(
        id="user-1",
        email="user@example.com",
        display_name="User",
        preferred_language="en",
        streak_days=0,
        is_active=True,
        is_admin=is_admin,
    )


@pytest.mark.asyncio
async def test_get_admin_user_allows_admin():
    user = _make_user(is_admin=True)

    assert await get_admin_user(user) is user


@pytest.mark.asyncio
async def test_get_admin_user_rejects_regular_user():
    user = _make_user(is_admin=False)

    with pytest.raises(HTTPException) as exc:
        await get_admin_user(user)

    assert exc.value.status_code == 403
    assert exc.value.detail == "Admin access required"
