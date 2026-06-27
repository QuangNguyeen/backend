from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.config import get_settings
from app.core.exceptions import UnauthorizedError
from app.database import get_db
from app.models.user import User
from app.repositories.auth_repository import AuthRepository
from app.schemas.auth import (
    GoogleLoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Auth"])


def get_auth_service(db: AsyncSession = Depends(get_db)) -> AuthService:
    return AuthService(AuthRepository(db))


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    service: AuthService = Depends(get_auth_service),
):
    return await service.register(body)


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    service: AuthService = Depends(get_auth_service),
):
    return await service.login(form_data.username, form_data.password)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest,
    service: AuthService = Depends(get_auth_service),
):
    return service.refresh(body.refresh_token)


@router.post("/google", response_model=TokenResponse)
async def google_login(
    body: GoogleLoginRequest,
    service: AuthService = Depends(get_auth_service),
):
    settings = get_settings()

    # Token verification stays at the transport/auth boundary.
    try:
        idinfo = google_id_token.verify_oauth2_token(
            body.id_token,
            google_requests.Request(),
            # Audience is verified manually below so that tokens from any of our
            # client IDs (web + native iOS/Android) are accepted.
            audience=None,
            # Tolerate small clock drift between Google, the device, and the
            # server — otherwise mobile sign-ins intermittently fail with
            # "Token used too early/late".
            clock_skew_in_seconds=10,
        )
    except ValueError as e:
        raise UnauthorizedError(f"Invalid Google token: {e}")

    # Accept the token only if its audience is one of our configured client IDs.
    if idinfo.get("aud") not in settings.google_client_ids:
        raise UnauthorizedError("Invalid Google token: unrecognized client ID")

    return await service.google_login(idinfo)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user