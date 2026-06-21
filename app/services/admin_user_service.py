"""Business logic for admin user management."""

from datetime import date

from app.core.exceptions import BadRequestError, NotFoundError
from app.core.security import hash_password
from app.models.user import User
from app.repositories.admin_user_repository import AdminUserRepository
from app.schemas.admin import (
    AdminPatchUserRequest,
    AdminUserDetailResponse,
    AdminUserListResponse,
    AdminUserResponse,
)
from app.schemas.user import UserStatsBlock
from app.services.stats_service import compute_streaks
from app.utils.email import normalize_email


def _serialize_user(
    user: User, total_sessions: int = 0, total_vocabulary: int = 0
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


class AdminUserService:
    def __init__(self, repo: AdminUserRepository):
        self.repo = repo

    async def _build_user_stats(self, user_id: str) -> UserStatsBlock:
        total_attempts = await self.repo.count_completed_attempts(user_id)
        avg_raw = await self.repo.average_sentence_score(user_id)
        total_vocabulary = await self.repo.count_saved_words(user_id)
        active_dates = await self.repo.active_attempt_dates(user_id)
        current_streak, longest_streak = compute_streaks(active_dates, date.today())

        return UserStatsBlock(
            total_attempts=total_attempts,
            average_score=round(float(avg_raw) * 100, 1),
            total_vocabulary=total_vocabulary,
            current_streak=current_streak,
            longest_streak=longest_streak,
        )

    def _detail(self, user: User, stats: UserStatsBlock) -> AdminUserDetailResponse:
        return AdminUserDetailResponse(
            **_serialize_user(
                user,
                total_sessions=stats.total_attempts,
                total_vocabulary=stats.total_vocabulary,
            ).model_dump(),
            stats=stats,
        )

    async def list_users(
        self, *, is_admin, is_active, search, page, page_size
    ) -> AdminUserListResponse:
        rows, total = await self.repo.list_with_counts(
            is_admin=is_admin,
            is_active=is_active,
            search=search,
            page=page,
            page_size=page_size,
        )
        total_pages = max(1, -(-total // page_size))  # ceil
        return AdminUserListResponse(
            items=[_serialize_user(row[0], row[1] or 0, row[2] or 0) for row in rows],
            total=total,
            page=page,
            total_pages=total_pages,
        )

    async def get_user_detail(self, user_id: str) -> AdminUserDetailResponse:
        user = await self.repo.get_by_id(user_id)
        if not user:
            raise NotFoundError("User not found")
        stats = await self._build_user_stats(user.id)
        return self._detail(user, stats)

    async def patch_user(
        self, current_admin: User, user_id: str, body: AdminPatchUserRequest
    ) -> AdminUserDetailResponse:
        target_user = await self.repo.get_by_id(user_id)
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
                if await self.repo.email_belongs_to_other(normalized_email, target_user.id):
                    raise BadRequestError("Email already belongs to another user")
                target_user.email = normalized_email
        if body.password:
            target_user.password_hash = hash_password(body.password)

        await self.repo.commit()
        await self.repo.refresh(target_user)

        stats = await self._build_user_stats(target_user.id)
        return self._detail(target_user, stats)