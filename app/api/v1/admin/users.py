import math
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user
from app.core.exceptions import BadRequestError, NotFoundError
from app.core.security import hash_password
from app.database import get_db
from app.models.dictation import DictationAttempt, DictationSentence
from app.models.user import User
from app.models.vocabulary import SavedWord
from app.schemas.admin import (
    AdminPatchUserRequest,
    AdminUserDetailResponse,
    AdminUserListResponse,
    AdminUserResponse,
)
from app.schemas.user import UserStatsBlock
from app.services.stats_service import compute_streaks
from app.utils.email import normalize_email

router = APIRouter(prefix="/users")


def _apply_user_filters(
    query,
    *,
    is_admin: bool | None,
    is_active: bool | None,
    search: str | None,
):
    if is_admin is not None:
        query = query.where(User.is_admin == is_admin)
    if is_active is not None:
        query = query.where(User.is_active == is_active)
    if search:
        pattern = f"%{search.strip()}%"
        query = query.where(or_(User.email.ilike(pattern), User.display_name.ilike(pattern)))
    return query


async def _build_user_stats(db: AsyncSession, user_id: str) -> UserStatsBlock:
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
            .join(DictationAttempt, DictationSentence.attempt_id == DictationAttempt.id)
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


def _serialize_user(
    user: User,
    total_sessions: int = 0,
    total_vocabulary: int = 0,
) -> AdminUserResponse:
    return AdminUserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        preferred_language=user.preferred_language,
        streak_days=user.streak_days,
        is_admin=user.is_admin,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        total_sessions=total_sessions,
        total_vocabulary=total_vocabulary,
    )


@router.get("", response_model=AdminUserListResponse)
async def list_admin_users(
    is_admin: bool | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    sessions_sub = (
        select(
            DictationAttempt.user_id,
            func.count().filter(DictationAttempt.status == "completed").label("total_sessions"),
        )
        .group_by(DictationAttempt.user_id)
        .subquery()
    )
    vocabulary_sub = (
        select(SavedWord.user_id, func.count().label("total_vocabulary"))
        .where(SavedWord.deleted_at.is_(None))
        .group_by(SavedWord.user_id)
        .subquery()
    )

    count_q = _apply_user_filters(
        select(func.count()).select_from(User),
        is_admin=is_admin,
        is_active=is_active,
        search=search,
    )
    total = (await db.execute(count_q)).scalar() or 0
    total_pages = max(1, math.ceil(total / page_size))

    query = (
        select(
            User,
            func.coalesce(sessions_sub.c.total_sessions, 0).label("total_sessions"),
            func.coalesce(vocabulary_sub.c.total_vocabulary, 0).label("total_vocabulary"),
        )
        .outerjoin(sessions_sub, User.id == sessions_sub.c.user_id)
        .outerjoin(vocabulary_sub, User.id == vocabulary_sub.c.user_id)
    )
    query = _apply_user_filters(query, is_admin=is_admin, is_active=is_active, search=search)
    query = query.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)

    rows = (await db.execute(query)).all()
    return AdminUserListResponse(
        items=[_serialize_user(row[0], row[1] or 0, row[2] or 0) for row in rows],
        total=total,
        page=page,
        total_pages=total_pages,
    )


@router.get("/{user_id}", response_model=AdminUserDetailResponse)
async def get_admin_user_detail(
    user_id: str,
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise NotFoundError("User not found")

    stats = await _build_user_stats(db, user.id)
    return AdminUserDetailResponse(
        **_serialize_user(
            user,
            total_sessions=stats.total_attempts,
            total_vocabulary=stats.total_vocabulary,
        ).model_dump(),
        stats=stats,
    )


@router.patch("/{user_id}", response_model=AdminUserDetailResponse)
async def patch_admin_user(
    user_id: str,
    body: AdminPatchUserRequest,
    current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    target_user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not target_user:
        raise NotFoundError("User not found")

    if body.is_admin is False and target_user.id == current_admin.id:
        raise BadRequestError("Cannot revoke your own admin rights")
    if body.is_active is False and target_user.id == current_admin.id:
        raise BadRequestError("Cannot deactivate your own account")

    if body.is_admin is not None:
        target_user.is_admin = body.is_admin
    if body.is_active is not None:
        target_user.is_active = body.is_active
    if body.email is not None:
        normalized_email = normalize_email(str(body.email))
        if normalized_email != normalize_email(target_user.email):
            existing_user = (
                await db.execute(
                    select(User).where(
                        func.lower(User.email) == normalized_email,
                        User.id != target_user.id,
                    )
                )
            ).scalar_one_or_none()
            if existing_user:
                raise BadRequestError("Email already belongs to another user")
            target_user.email = normalized_email
    if body.password:
        target_user.password_hash = hash_password(body.password)

    await db.commit()
    await db.refresh(target_user)

    stats = await _build_user_stats(db, target_user.id)
    return AdminUserDetailResponse(
        **_serialize_user(
            target_user,
            total_sessions=stats.total_attempts,
            total_vocabulary=stats.total_vocabulary,
        ).model_dump(),
        stats=stats,
    )
