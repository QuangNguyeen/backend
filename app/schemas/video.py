from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TopicTagResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: str | None = None
    is_active: bool = True
    sort_order: int = 0

    model_config = {"from_attributes": True}


class ImporterSummaryResponse(BaseModel):
    id: str
    display_name: str


class LevelAnalysisResponse(BaseModel):
    level: str
    score: float
    features: dict
    label: str | None = None
    factors: dict | None = None
    explanation: list[str] = []
    recommendedModes: dict | None = None
    error: str | None = None


class VideoResponse(BaseModel):
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
    is_curated: bool
    is_active: bool
    publish_status: str = "published"
    published_at: datetime | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None
    is_auto_generated: bool = False
    transcription_status: str = "ready"
    transcription_error: str | None = None
    thumbnail_url: str
    play_count: int = 0
    best_score: float | None = None
    topic_tags: list[TopicTagResponse] = Field(default_factory=list)
    my_topic_tags: list[TopicTagResponse] = Field(default_factory=list)
    my_practice_created_at: datetime | None = None

    model_config = {"from_attributes": True}


class ImportVideoRequest(BaseModel):
    youtube_url: str = Field(..., description="YouTube video URL or video ID")
    title: str | None = Field(None, description="Custom title (auto-fetched if omitted)")
    channel: str | None = Field(None, description="Channel name (auto-fetched if omitted)")
    language: str = Field("en", description="Language code (e.g., en, ja)")
    level: str | None = Field(
        None, description="CEFR level (A1–C2). Auto-detected from transcript if omitted."
    )
    languages: list[str] | None = Field(
        None,
        description=(
            "Preferred transcript languages (defaults to ['en', 'en-US', 'en-GB'] if omitted)"
        ),
    )
    max_segment_duration: float = Field(
        10.0, ge=3.0, le=30.0, description="Maximum duration in seconds for each sentence segment"
    )
    topic_tag_ids: list[str] = Field(
        default_factory=list,
        description="Fixed topic tag IDs selected by the importer.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                }
            ]
        }
    }


class ImportVideoResponse(BaseModel):
    video: VideoResponse
    already_exists: bool = False
    already_in_my_practice: bool = False
    message: str
    similar_importers_count: int = 0
    similar_importers: list[ImporterSummaryResponse] = Field(default_factory=list)


class VideoRecommendationItemResponse(BaseModel):
    video: VideoResponse
    reason_code: Literal[
        "same_channel",
        "topic_match",
        "level_match",
        "level_progression",
        "preferred_language",
        "curated",
        "popular",
        "new_content",
    ]
    reason_text: str


class VideoRecommendationsResponse(BaseModel):
    strategy: Literal["personalized", "cold_start"]
    items: list[VideoRecommendationItemResponse] = Field(default_factory=list)


class ImportPartialResponse(BaseModel):
    """Returned when metadata was fetched but transcript extraction failed."""

    status: str = "partial"
    video_id: str
    title: str
    channel: str
    thumbnail_url: str
    duration: int
    message: str


class TranscriptResponse(BaseModel):
    id: str
    index: int
    text: str
    start_time: float
    end_time: float
    language: str

    model_config = {"from_attributes": True}


class TranscriptLanguageResponse(BaseModel):
    language: str
    language_code: str
    is_generated: bool
    is_translatable: bool


class TranscriptUpdateItem(BaseModel):
    transcript_id: str = Field(..., description="ID of the transcript row to update")
    text: str = Field("", description="New subtitle text (ignored when is_deleted=True)")
    start_time: float | None = Field(None, ge=0, description="New segment start time in seconds")
    end_time: float | None = Field(None, ge=0, description="New segment end time in seconds")
    is_deleted: bool = Field(False, description="When True, delete this transcript row")


class TranscriptBulkUpdateRequest(BaseModel):
    items: list[TranscriptUpdateItem] = Field(..., min_length=1)


class TranscriptBulkUpdateResponse(BaseModel):
    updated: int


class VideoEditStatusResponse(BaseModel):
    has_in_progress_attempt: bool


class PublishRequestCreate(BaseModel):
    message: str | None = Field(None, max_length=2000)


class PublishRequestResponse(BaseModel):
    id: str
    user_id: str
    video_id: str
    status: str
    message: str | None = None
    admin_note: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TranscriptFeedbackCreate(BaseModel):
    transcript_id: str | None = None
    message: str = Field(..., min_length=1, max_length=5000)
    suggested_text: str | None = Field(None, max_length=5000)


class TranscriptFeedbackResponse(BaseModel):
    id: str
    user_id: str
    video_id: str
    transcript_id: str | None = None
    message: str
    suggested_text: str | None = None
    status: str
    admin_note: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AdminTranscriptFeedbackItemResponse(BaseModel):
    id: str
    video_id: str
    transcript_id: str | None = None
    message: str
    suggested_text: str | None = None
    status: str
    admin_note: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    user_id: str
    user_name: str | None = None
    user_email: str | None = None
    video_title: str
    transcript_text: str | None = None


class AdminTranscriptFeedbackListResponse(BaseModel):
    items: list[AdminTranscriptFeedbackItemResponse]
    total: int
    page: int
    total_pages: int


class AdminTopicTagCreate(BaseModel):
    slug: str = Field(..., min_length=1, max_length=80)
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = None
    is_active: bool = True
    sort_order: int = 0


class AdminTopicTagPatch(BaseModel):
    slug: str | None = Field(None, min_length=1, max_length=80)
    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class AdminPublishRequestAction(BaseModel):
    admin_note: str | None = Field(None, max_length=2000)
    topic_tag_ids: list[str] | None = None


class VideoTopicTagsUpdate(BaseModel):
    topic_tag_ids: list[str] = Field(default_factory=list)


class AdminFeedbackPatch(BaseModel):
    status: str | None = Field(None, pattern="^(pending|reviewed|resolved|rejected)$")
    admin_note: str | None = Field(None, max_length=2000)
