from pydantic import BaseModel, Field


class LevelAnalysisResponse(BaseModel):
    level: str
    score: float
    features: dict
    error: str | None = None


class VideoResponse(BaseModel):
    id: str
    youtube_id: str
    title: str
    channel: str
    duration: int
    language: str
    level: str | None
    is_curated: bool
    is_active: bool
    thumbnail_url: str
    play_count: int = 0
    best_score: float | None = None

    model_config = {"from_attributes": True}


class ImportVideoRequest(BaseModel):
    youtube_url: str = Field(..., description="YouTube video URL or video ID")
    title: str | None = Field(None, description="Custom title (auto-fetched if omitted)")
    channel: str | None = Field(None, description="Channel name (auto-fetched if omitted)")
    language: str = Field("en", description="Language code (e.g., en, ja)")
    level: str | None = Field(None, description="CEFR level (A1–C2). Auto-detected from transcript if omitted.")
    languages: list[str] | None = Field(
        None,
        description="Preferred transcript languages (defaults to ['en', 'en-US', 'en-GB'] if omitted)"
    )
    max_segment_duration: float = Field(
        10.0,
        ge=3.0,
        le=30.0,
        description="Maximum duration in seconds for each sentence segment"
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
