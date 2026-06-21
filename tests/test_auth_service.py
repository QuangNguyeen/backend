"""Unit tests for AuthService (register, login, refresh) via a fake repo."""

import pytest

from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import create_refresh_token, hash_password
from app.models.user import User
from app.schemas.auth import RegisterRequest, TokenResponse
from app.services.auth_service import AuthService


class FakeAuthRepository:
    def __init__(self):
        self.by_email = None
        self.by_google = None
        self.added = []
        self.committed = 0

    async def get_by_email_ci(self, email):
        return self.by_email

    async def get_by_google_id(self, google_id):
        return self.by_google

    def add(self, instance):
        self.added.append(instance)

    async def commit(self):
        self.committed += 1

    async def refresh(self, instance):
        pass


def _user(**kw):
    defaults = dict(
        id="u1", email="user@example.com", display_name="U",
        password_hash=hash_password("correct-pass"), preferred_language="en",
        is_active=True,
    )
    defaults.update(kw)
    return User(**defaults)


# ── register ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_conflict_when_email_exists():
    repo = FakeAuthRepository()
    repo.by_email = _user()
    service = AuthService(repo)
    body = RegisterRequest(
        email="user@example.com", password="secret123", display_name="U",
        preferred_language="en",
    )
    with pytest.raises(ConflictError):
        await service.register(body)


@pytest.mark.asyncio
async def test_register_creates_user():
    repo = FakeAuthRepository()
    service = AuthService(repo)
    body = RegisterRequest(
        email="New@Example.com", password="secret123", display_name="New",
        preferred_language="en",
    )
    user = await service.register(body)
    assert user.email == "new@example.com"  # normalized
    assert repo.added and repo.committed == 1


# ── login ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_login_unknown_user():
    service = AuthService(FakeAuthRepository())
    with pytest.raises(UnauthorizedError):
        await service.login("nobody@example.com", "x")


@pytest.mark.asyncio
async def test_login_inactive_account():
    repo = FakeAuthRepository()
    repo.by_email = _user(is_active=False)
    with pytest.raises(UnauthorizedError):
        await AuthService(repo).login("user@example.com", "correct-pass")


@pytest.mark.asyncio
async def test_login_google_only_account_has_no_password():
    repo = FakeAuthRepository()
    repo.by_email = _user(password_hash=None)
    with pytest.raises(UnauthorizedError):
        await AuthService(repo).login("user@example.com", "anything")


@pytest.mark.asyncio
async def test_login_wrong_password():
    repo = FakeAuthRepository()
    repo.by_email = _user()
    with pytest.raises(UnauthorizedError):
        await AuthService(repo).login("user@example.com", "wrong-pass")


@pytest.mark.asyncio
async def test_login_success_issues_tokens_and_sets_last_login():
    repo = FakeAuthRepository()
    user = _user()
    repo.by_email = user
    tokens = await AuthService(repo).login("User@Example.com", "correct-pass")
    assert isinstance(tokens, TokenResponse)
    assert tokens.access_token and tokens.refresh_token
    assert user.last_login_at is not None
    assert repo.committed == 1


# ── refresh ───────────────────────────────────────────────────────────────────


def test_refresh_rejects_non_refresh_token():
    service = AuthService(FakeAuthRepository())
    # An access token (or garbage) must not be accepted as a refresh token.
    with pytest.raises(UnauthorizedError):
        service.refresh("not-a-real-token")


def test_refresh_accepts_valid_refresh_token():
    service = AuthService(FakeAuthRepository())
    token = create_refresh_token({"sub": "u1"})
    tokens = service.refresh(token)
    assert isinstance(tokens, TokenResponse)
    assert tokens.access_token and tokens.refresh_token
