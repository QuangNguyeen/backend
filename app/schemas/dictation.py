from pydantic import BaseModel


class SubmitAnswerRequest(BaseModel):
    sentence_index: int
    user_input: str
    hints_used: int = 0
    replay_count: int = 0
    skipped: bool = False


class WordDiffItem(BaseModel):
    word: str
    status: str  # "correct" | "wrong" | "missing" | "hinted" | "corrected" | "extra"
    expected: str | None = None


class SentenceResultResponse(BaseModel):
    sentence_index: int
    score: float
    word_diffs: list[WordDiffItem]
    correct_count: int
    wrong_count: int
    missing_count: int
    is_skipped: bool = False
    # Vocabulary saving context
    original_text: str
    video_id: str
    audio_start_time: float
    word_difficulty: dict[str, float]  # word → 0.0 (easy) to 1.0 (hard)


class AttemptResultResponse(BaseModel):
    attempt_id: str
    video_id: str
    total_score: float
    total_sentences: int
    completed_sentences: int
    results: list[SentenceResultResponse]


class DashboardStatsResponse(BaseModel):
    total_sessions: int
    total_sentences: int
    total_time_minutes: float
    average_accuracy: float
    total_videos: int
    current_streak: int
    longest_streak: int


class HeatmapDay(BaseModel):
    date: str  # "2026-04-01"
    count: int
    level: int  # 0-4 intensity


class AccuracyPoint(BaseModel):
    date: str
    score: float
    accuracy: float


class DashboardFullResponse(BaseModel):
    stats: DashboardStatsResponse
    heatmap: list[HeatmapDay]
    accuracy_trend: list[AccuracyPoint]


class HistoryEntryResponse(BaseModel):
    id: str
    video_title: str
    video_thumbnail: str
    type: str
    status: str
    score: float | None
    progress_str: str
    completed_at: str | None
    updated_at: str


class HistoryAttemptResponse(BaseModel):
    attempt_id: str
    video_id: str
    status: str
    score: float | None = None
    progress_str: str  # e.g. "5/10"
    video_title: str
    video_thumbnail: str
    practice_mode: str = "sentence"
    duration_seconds: int | None = None
    error_summary: dict | None = None
    updated_at: str
    completed_at: str | None = None


class HistoryPaginatedResponse(BaseModel):
    items: list[HistoryAttemptResponse]
    total: int
    page: int
    total_pages: int
