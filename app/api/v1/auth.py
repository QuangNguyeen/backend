from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.auth import (
    LoginRequest, RegisterRequest, TokenResponse, UserResponse, RefreshRequest,
    GoogleLoginRequest,
)
from app.core.security import (
    hash_password, verify_password, create_access_token, create_refresh_token, decode_token,
)
from app.core.exceptions import ConflictError, UnauthorizedError
from app.config import get_settings
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    # Check existing
    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none():
        raise ConflictError("Email already registered")

    user = User(
        email=body.email,
        display_name=body.display_name,
        password_hash=hash_password(body.password),
        preferred_language=body.preferred_language,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == form_data.username))
    user = result.scalar_one_or_none()

    if not user or not user.password_hash or not verify_password(form_data.password, user.password_hash):
        raise UnauthorizedError("Invalid email or password")

    access_token = create_access_token({"sub": user.id})
    refresh_token = create_refresh_token({"sub": user.id})
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest):
    payload = decode_token(body.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise UnauthorizedError("Invalid refresh token")

    access_token = create_access_token({"sub": payload["sub"]})
    refresh_token = create_refresh_token({"sub": payload["sub"]})
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/google", response_model=TokenResponse)
async def google_login(body: GoogleLoginRequest, db: AsyncSession = Depends(get_db)):
    settings = get_settings()

    try:
        idinfo = google_id_token.verify_oauth2_token(
            body.id_token,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
    except ValueError as e:
        raise UnauthorizedError(f"Invalid Google token: {e}")

    google_id = idinfo["sub"]
    email = idinfo.get("email", "")
    display_name = idinfo.get("name", email.split("@")[0])

    result = await db.execute(select(User).where(User.google_id == google_id))
    user = result.scalar_one_or_none()

    if not user:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

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
            db.add(user)

    from datetime import datetime, timezone
    user.last_login_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(user)

    access_token = create_access_token({"sub": user.id})
    refresh_token = create_refresh_token({"sub": user.id})
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return current_user
