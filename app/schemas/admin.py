from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.schemas.user import UserStatsBlock


class AdminStatsResponse(BaseModel):
    total_users: int
    total_videos: int
    total_sessions: int
    total_vocabulary_words: int
    pending_transcriptions: int
    failed_transcriptions: int
    new_users_today: int
    sessions_today: int


class AdminVideoResponse(BaseModel):
    id: str
    youtube_id: str
    title: str
    channel: str
    duration: int
    language: str
    level: str | None
    difficulty_score: float | None = None
    difficulty_level: str | None = None
    difficulty_label: str | None = None
    difficulty_factors: dict | None = None
    is_curated: bool
    is_active: bool
    is_auto_generated: bool
    transcription_status: str
    transcription_error: str | None
    thumbnail_url: str
    created_by: str | None
    created_by_email: str | None = None
    created_by_name: str | None = None
    created_at: datetime
    play_count: int = 0
    best_score: float | None = None


class AdminVideoListResponse(BaseModel):
    items: list[AdminVideoResponse]
    total: int
    page: int
    total_pages: int


class AdminPatchVideoRequest(BaseModel):
    is_active: bool | None = None
    is_curated: bool | None = None
    level: str | None = Field(default=None, max_length=5)


class AdminRetryTranscriptionResponse(BaseModel):
    video_id: str
    status: str
    task_id: str | None = None


class AdminDifficultyRecalculationResponse(BaseModel):
    video_id: str
    difficulty_score: float
    difficulty_level: str
    difficulty_label: str
    factors: dict
    explanation: list[str]
    recommendedModes: dict


class AdminUserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    preferred_language: str
    streak_days: int
    is_admin: bool
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None
    total_sessions: int = 0
    total_vocabulary: int = 0


class AdminUserDetailResponse(AdminUserResponse):
    stats: UserStatsBlock


class AdminUserListResponse(BaseModel):
    items: list[AdminUserResponse]
    total: int
    page: int
    total_pages: int


class AdminPatchUserRequest(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


class AdminTrafficPoint(BaseModel):
    date: str
    active_users: int
    new_users: int


class AdminTrafficResponse(BaseModel):
    points: list[AdminTrafficPoint]
    time_range: str


class AdminStudyHoursPoint(BaseModel):
    date: str
    total_minutes: float
    avg_minutes_per_user: float


class AdminStudyHoursResponse(BaseModel):
    points: list[AdminStudyHoursPoint]
    time_range: str


class AdminTopLearner(BaseModel):
    user_id: str
    display_name: str
    email: str
    study_minutes: float
    sessions: int
    avg_accuracy: float
    streak: int


class AdminTopLearnersResponse(BaseModel):
    learners: list[AdminTopLearner]
    time_range: str


class AdminContentHealthResponse(BaseModel):
    total_videos: int
    ready: int
    pending: int
    processing: int
    failed: int
    curated: int
    levels: dict[str, int]


class AdminEngagementResponse(BaseModel):
    completion_rate: float
    avg_session_duration: float
    repeat_rate: float
    vocab_save_rate: float


class AdminRecentActivity(BaseModel):
    id: str
    type: str
    description: str
    user_name: str | None
    timestamp: datetime


class AdminRecentActivityResponse(BaseModel):
    activities: list[AdminRecentActivity]
