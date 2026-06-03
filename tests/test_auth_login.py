"""Tests for password login email normalization."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.security import hash_password
from app.database import get_db
from app.main import create_app
from app.models.user import User

PASSWORD = "testpass123"


def _make_user(*, email: str, password_hash: str | None = None, is_active: bool = True):
    return User(
        id=str(uuid.uuid4()),
        email=email,
        display_name="Test User",
        password_hash=password_hash,
        preferred_language="en",
        streak_days=0,
        is_active=is_active,
        is_admin=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        last_login_at=None,
    )


class FakeResult:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


def _mock_db_session(user):
    session = AsyncMock()

    async def fake_execute(stmt):
        return FakeResult(user)

    session.execute = fake_execute
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


app = create_app()


@pytest.fixture
def override_db():
    def _override(session):
        async def _get_db():
            yield session

        app.dependency_overrides[get_db] = _get_db
        return session

    yield _override
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_login_matches_email_case_insensitively(override_db):
    user = _make_user(email="user@example.com", password_hash=hash_password(PASSWORD))
    override_db(_mock_db_session(user))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/auth/login",
            data={"username": "User@Example.com", "password": PASSWORD},
        )

    assert resp.status_code == 200
    assert "access_token" in resp.json()
    assert user.last_login_at is not None


@pytest.mark.asyncio
async def test_login_rejects_google_only_account(override_db):
    user = _make_user(email="google@example.com", password_hash=None)
    override_db(_mock_db_session(user))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/auth/login",
            data={"username": "google@example.com", "password": PASSWORD},
        )

    assert resp.status_code == 401
    assert "no password set" in resp.json()["detail"].lower()
