"""Shared fixtures for endpoint-level (API) tests.

These exercise the full router → service stack over ASGI without a real
database: feature service dependencies are overridden with services backed by
fake repositories, and `get_current_user` is overridden with a test user.
"""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_admin_user, get_current_user
from app.main import create_app
from app.models.user import User


def make_user(*, is_admin: bool = False, **overrides) -> User:
    defaults = dict(
        id="user-1",
        email="user@example.com",
        display_name="Test User",
        preferred_language="en",
        password_hash=None,
        streak_days=0,
        is_active=True,
        is_admin=is_admin,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_login_at=None,
    )
    defaults.update(overrides)
    return User(**defaults)


@pytest.fixture
def app():
    """A fresh app per test so dependency_overrides never leak."""
    application = create_app()
    yield application
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_as(app):
    """Override the current user (and admin) for protected endpoints."""

    def _set(user: User | None = None, *, is_admin: bool = False) -> User:
        u = user or make_user(is_admin=is_admin)
        app.dependency_overrides[get_current_user] = lambda: u
        if is_admin:
            app.dependency_overrides[get_admin_user] = lambda: u
        return u

    return _set