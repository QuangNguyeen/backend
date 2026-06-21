"""Business logic for user profile, stats aggregation, and credentials."""

from datetime import date

from app.core.exceptions import BadRequestError
from app.core.security import hash_password, verify_password
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import (
    ChangePasswordRequest,
    UserPreferences,
    UserProfileResponse,
    UserStatsBlock,
    UserUpdateRequest,
)
from app.services.stats_service import compute_streaks


class UserService:
    def __init__(self, repo: UserRepository):
        self.repo = repo

    async def get_profile(self, user: User) -> UserProfileResponse:
        """Return the user merged with aggregated learning stats."""
        stats = await self._build_stats(user.id)
        return self._serialize_profile(user, stats)

    async def update_profile(self, user: User, body: UserUpdateRequest) -> UserProfileResponse:
        """Partial update — only supplied fields are written.

        Preferences are merged shallowly so an older client PUTting a partial
        preferences object does not clobber fields it doesn't know about.
        """
        if body.display_name is not None:
            user.display_name = body.display_name

        if body.preferences is not None:
            user.preferences = {**(user.preferences or {}), **body.preferences.model_dump()}

        await self.repo.commit()
        await self.repo.refresh(user)

        stats = await self._build_stats(user.id)
        return self._serialize_profile(user, stats)

    async def change_password(self, user: User, body: ChangePasswordRequest) -> None:
        if not user.password_hash:
            raise BadRequestError(
                "Account uses social login — set a password via your profile settings"
            )
        if not verify_password(body.current_password, user.password_hash):
            raise BadRequestError("Current password is incorrect")
        if len(body.new_password) < 6:
            raise BadRequestError("New password must be at least 6 characters")

        user.password_hash = hash_password(body.new_password)
        await self.repo.commit()

    async def _build_stats(self, user_id: str) -> UserStatsBlock:
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

    @staticmethod
    def _coerce_preferences(raw: dict | None) -> UserPreferences:
        """Tolerate legacy/empty rows by falling back to defaults."""
        return UserPreferences.model_validate(raw or {})

    @classmethod
    def _serialize_profile(cls, user: User, stats: UserStatsBlock) -> UserProfileResponse:
        return UserProfileResponse(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            is_admin=user.is_admin,
            preferred_language=user.preferred_language,
            preferences=cls._coerce_preferences(user.preferences),
            created_at=user.created_at,
            stats=stats,
        )