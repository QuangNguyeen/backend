"""Tests for POST /api/v1/auth/google — Google OAuth2 ID-token login."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.database import get_db
from app.main import create_app
from app.models.user import User

FAKE_GOOGLE_ID = "google-uid-123456"
FAKE_EMAIL = "test@gmail.com"
FAKE_NAME = "Test User"
FAKE_ID_TOKEN = "fake.google.id_token"
FAKE_CLIENT_ID = "test-web-client-id.apps.googleusercontent.com"


def _make_user(*, google_id=None, email=FAKE_EMAIL, password_hash=None, user_id=None):
    return User(
        id=user_id or str(uuid.uuid4()),
        email=email,
        display_name=FAKE_NAME,
        google_id=google_id,
        password_hash=password_hash,
        preferred_language="en",
        streak_days=0,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        last_login_at=None,
    )


class FakeResult:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


def _mock_db_session(existing_user_by_google=None, existing_user_by_email=None):
    session = AsyncMock()
    call_count = 0

    async def fake_execute(stmt):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return FakeResult(existing_user_by_google)
        return FakeResult(existing_user_by_email)

    session.execute = fake_execute
    session.add = MagicMock()
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


@pytest.fixture
def mock_verify_token():
    with (
        patch("app.api.v1.auth.google_id_token.verify_oauth2_token") as mock,
        patch("app.api.v1.auth.get_settings") as mock_settings,
    ):
        mock.return_value = {
            "sub": FAKE_GOOGLE_ID,
            "email": FAKE_EMAIL,
            "name": FAKE_NAME,
            "aud": FAKE_CLIENT_ID,
        }
        mock_settings.return_value.google_client_ids = [FAKE_CLIENT_ID]
        yield mock


@pytest.mark.asyncio
async def test_google_login_new_user(override_db, mock_verify_token):
    session = _mock_db_session()
    override_db(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/auth/google", json={"id_token": FAKE_ID_TOKEN})

    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"
    session.add.assert_called_once()


@pytest.mark.asyncio
async def test_google_login_existing_email_links_google_id(override_db, mock_verify_token):
    existing = _make_user(google_id=None)
    session = _mock_db_session(existing_user_by_google=None, existing_user_by_email=existing)
    override_db(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/auth/google", json={"id_token": FAKE_ID_TOKEN})

    assert resp.status_code == 200
    assert existing.google_id == FAKE_GOOGLE_ID
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_google_login_existing_google_id(override_db, mock_verify_token):
    existing = _make_user(google_id=FAKE_GOOGLE_ID)
    session = _mock_db_session(existing_user_by_google=existing)
    override_db(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/v1/auth/google", json={"id_token": FAKE_ID_TOKEN})

    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_google_login_wrong_audience(override_db):
    """A validly-signed token whose audience is not one of our client IDs is rejected."""
    session = _mock_db_session()
    override_db(session)

    with (
        patch("app.api.v1.auth.google_id_token.verify_oauth2_token") as mock,
        patch("app.api.v1.auth.get_settings") as mock_settings,
    ):
        mock.return_value = {
            "sub": FAKE_GOOGLE_ID,
            "email": FAKE_EMAIL,
            "name": FAKE_NAME,
            "aud": "some-other-client-id.apps.googleusercontent.com",
        }
        mock_settings.return_value.google_client_ids = [FAKE_CLIENT_ID]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/auth/google", json={"id_token": FAKE_ID_TOKEN})

    assert resp.status_code == 401
    assert "Invalid Google token" in resp.json()["detail"]
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_google_login_invalid_token(override_db):
    session = _mock_db_session()
    override_db(session)

    with patch(
        "app.api.v1.auth.google_id_token.verify_oauth2_token",
        side_effect=ValueError("Token expired"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/auth/google", json={"id_token": "bad-token"})

    assert resp.status_code == 401
    assert "Invalid Google token" in resp.json()["detail"]
