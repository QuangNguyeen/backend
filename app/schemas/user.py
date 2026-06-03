from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class UserPreferences(BaseModel):
    """User-tunable app settings persisted as JSON on the User row."""

    audio_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    theme: Literal["light", "dark", "system"] = "system"


class UserStatsBlock(BaseModel):
    """Aggregated learning stats — computed on read, not persisted."""

    total_attempts: int
    average_score: float  # 0–100, one decimal place
    total_vocabulary: int
    current_streak: int
    longest_streak: int


class UserProfileResponse(BaseModel):
    id: str
    email: str
    display_name: str
    is_admin: bool = False
    preferred_language: str
    preferences: UserPreferences
    created_at: datetime
    stats: UserStatsBlock

    model_config = {"from_attributes": True}


class UserUpdateRequest(BaseModel):
    """Partial update — only fields supplied are touched."""

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    preferences: UserPreferences | None = None

    @field_validator("display_name")
    @classmethod
    def strip_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("display_name cannot be blank")
        return stripped
