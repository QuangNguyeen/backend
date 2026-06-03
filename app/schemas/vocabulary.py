from datetime import datetime

from pydantic import BaseModel


class SaveWordRequest(BaseModel):
    word: str
    video_id: str | None = None
    context_sentence: str | None = None
    audio_start_time: float | None = None
    meaning: str | None = None
    note: str | None = None
    source: str = "dictation"
    audio_url: str | None = None
    phonetic: str | None = None
    context_translation: str | None = None
    part_of_speech: str | None = None


class UpdateWordRequest(BaseModel):
    meaning: str | None = None
    note: str | None = None


class ReviewRequest(BaseModel):
    quality: int  # 1-5: Again(1), Hard(2), Good(3), Easy(5)


class SavedWordResponse(BaseModel):
    id: str
    word: str
    context_sentence: str | None
    audio_start_time: float | None
    meaning: str | None
    note: str | None
    source: str
    video_id: str | None
    ease_factor: float
    interval_days: int
    repetitions: int
    next_review_at: datetime | None
    last_reviewed_at: datetime | None
    created_at: datetime
    phonetic: str | None = None
    audio_url: str | None = None
    context_translation: str | None = None
    part_of_speech: str | None = None

    model_config = {"from_attributes": True}


class ReviewResponse(BaseModel):
    word_id: str
    next_review_at: datetime
    interval_days: int
    ease_factor: float
    repetitions: int


class FlashCardResponse(BaseModel):
    id: str
    word: str
    context_sentence: str | None
    audio_start_time: float | None
    video_id: str | None
    meaning: str | None
    phonetic: str | None = None
    audio_url: str | None = None
    context_translation: str | None = None
    part_of_speech: str | None = None

    model_config = {"from_attributes": True}


class DueCardsResponse(BaseModel):
    cards: list[FlashCardResponse]
    total_due: int


class WordPreviewResponse(BaseModel):
    word: str
    phonetic: str | None = None
    meaning: str | None = None
    audio_url: str | None = None
    context_translation: str | None = None
    is_saved: bool = False
    part_of_speech: str | None = None


class BatchPreviewRequest(BaseModel):
    words: list[str]
    context: str | None = None


class ImportResultResponse(BaseModel):
    job_id: str | None = None  # None if enrich=False
    imported: int
    updated: int
    errors: list[dict]
    total_words: int
    enrich_queued: bool


class ImportJobStatus(BaseModel):
    job_id: str
    status: str  # "pending" | "processing" | "completed" | "failed"
    total: int
    enriched: int
    phase: str  # "meanings" | "audio" | "translations" | "done"
    progress_pct: int  # 0-100
    error: str | None = None
