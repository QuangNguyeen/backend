from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.exceptions import BadRequestError
from app.core.security import hash_password, verify_password
from app.database import get_db
from app.models.dictation import DictationAttempt, DictationSentence
from app.models.user import User
from app.models.vocabulary import SavedWord
from app.schemas.user import (
    UserPreferences,
    UserProfileResponse,
    UserStatsBlock,
    UserUpdateRequest,
)
from app.services.stats_service import compute_streaks

router = APIRouter(prefix="/users", tags=["Users"])


async def _build_stats(db: AsyncSession, user_id: str) -> UserStatsBlock:
    """Aggregate the four headline stats + streak in independent queries.

    Per-user volumes are small; readability beats a single mega-query.
    """
    total_attempts = (
        await db.execute(
            select(func.count()).where(
                DictationAttempt.user_id == user_id,
                DictationAttempt.status == "completed",
            )
        )
    ).scalar() or 0

    avg_raw = (
        await db.execute(
            select(func.avg(DictationSentence.score))
            .join(DictationAttempt)
            .where(DictationAttempt.user_id == user_id)
        )
    ).scalar() or 0.0

    total_vocabulary = (
        await db.execute(
            select(func.count()).where(
                SavedWord.user_id == user_id,
                SavedWord.deleted_at.is_(None),
            )
        )
    ).scalar() or 0

    active_rows = (
        await db.execute(
            select(cast(DictationAttempt.updated_at, Date))
            .where(DictationAttempt.user_id == user_id)
            .group_by(cast(DictationAttempt.updated_at, Date))
        )
    ).all()
    active_dates = {row[0] for row in active_rows if row[0] is not None}
    current_streak, longest_streak = compute_streaks(active_dates, date.today())

    return UserStatsBlock(
        total_attempts=total_attempts,
        average_score=round(float(avg_raw) * 100, 1),
        total_vocabulary=total_vocabulary,
        current_streak=current_streak,
        longest_streak=longest_streak,
    )


def _coerce_preferences(raw: dict | None) -> UserPreferences:
    """Tolerate legacy/empty rows by falling back to defaults."""
    return UserPreferences.model_validate(raw or {})


def _serialize_profile(user: User, stats: UserStatsBlock) -> UserProfileResponse:
    return UserProfileResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        preferred_language=user.preferred_language,
        preferences=_coerce_preferences(user.preferences),
        created_at=user.created_at,
        stats=stats,
    )


@router.get("/me", response_model=UserProfileResponse)
async def get_my_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user merged with aggregated learning stats."""
    stats = await _build_stats(db, current_user.id)
    return _serialize_profile(current_user, stats)


@router.put("/me", response_model=UserProfileResponse)
async def update_my_profile(
    body: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Partial update — only fields present in the body are written.

    Preferences are merged shallowly so future fields aren't clobbered when an
    older client PUTs a partial preferences object.
    """
    if body.display_name is not None:
        current_user.display_name = body.display_name

    if body.preferences is not None:
        merged = {**(current_user.preferences or {}), **body.preferences.model_dump()}
        current_user.preferences = merged

    await db.commit()
    await db.refresh(current_user)

    stats = await _build_stats(db, current_user.id)
    return _serialize_profile(current_user, stats)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/me/change-password")
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.password_hash:
        raise BadRequestError(
            "Account uses social login — set a password via your profile settings"
        )
    if not verify_password(body.current_password, current_user.password_hash):
        raise BadRequestError("Current password is incorrect")
    if len(body.new_password) < 6:
        raise BadRequestError("New password must be at least 6 characters")

    current_user.password_hash = hash_password(body.new_password)
    await db.commit()
    return {"message": "Password changed successfully"}
