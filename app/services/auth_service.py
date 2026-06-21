"""Business logic for authentication: registration, login, token refresh, OAuth."""

from datetime import UTC, datetime

from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.repositories.auth_repository import AuthRepository
from app.schemas.auth import RegisterRequest, TokenResponse
from app.utils.email import normalize_email


def _issue_tokens(user_id: str) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token({"sub": user_id}),
        refresh_token=create_refresh_token({"sub": user_id}),
    )


class AuthService:
    def __init__(self, repo: AuthRepository):
        self.repo = repo

    async def register(self, body: RegisterRequest) -> User:
        email = normalize_email(str(body.email))
        if await self.repo.get_by_email_ci(email):
            raise ConflictError("Email already registered")

        user = User(
            email=email,
            display_name=body.display_name,
            password_hash=hash_password(body.password),
            preferred_language=body.preferred_language,
        )
        self.repo.add(user)
        await self.repo.commit()
        await self.repo.refresh(user)
        return user

    async def login(self, username: str, password: str) -> TokenResponse:
        email = normalize_email(username)
        user = await self.repo.get_by_email_ci(email)

        if not user:
            raise UnauthorizedError("Invalid email or password")
        if not user.is_active:
            raise UnauthorizedError("Account is deactivated")
        if not user.password_hash:
            raise UnauthorizedError(
                "This account has no password set. "
                "Use Google sign-in or ask an admin to set one."
            )
        if not verify_password(password, user.password_hash):
            raise UnauthorizedError("Invalid email or password")

        user.last_login_at = datetime.now(UTC)
        await self.repo.commit()

        return _issue_tokens(user.id)

    def refresh(self, refresh_token: str) -> TokenResponse:
        payload = decode_token(refresh_token)
        if not payload or payload.get("type") != "refresh":
            raise UnauthorizedError("Invalid refresh token")
        return _issue_tokens(payload["sub"])

    async def google_login(self, idinfo: dict) -> TokenResponse:
        google_id = idinfo["sub"]
        email = idinfo.get("email", "")
        display_name = idinfo.get("name", email.split("@")[0])

        user = await self.repo.get_by_google_id(google_id)

        if not user:
            user = await self.repo.get_by_email_ci(normalize_email(email))

            if user:
                user.google_id = google_id
            else:
                user = User(
                    email=email,
                    display_name=display_name,
                    google_id=google_id,
                    password_hash=None,
                    preferred_language="en",
                )
                self.repo.add(user)

        user.last_login_at = datetime.now(UTC)

        await self.repo.commit()
        await self.repo.refresh(user)

        return _issue_tokens(user.id)