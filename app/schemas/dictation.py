from pydantic import BaseModel


class SubmitAnswerRequest(BaseModel):
    sentence_index: int
    user_input: str
    hints_used: int = 0
    replay_count: int = 0


class WordDiffItem(BaseModel):
    word: str
    status: str  # "correct" | "wrong" | "missing"
    expected: str | None = None


class SentenceResultResponse(BaseModel):
    sentence_index: int
    score: float
    word_diffs: list[WordDiffItem]
    correct_count: int
    wrong_count: int
    missing_count: int


class SessionResultResponse(BaseModel):
    session_id: str
    video_id: str
    total_score: float
    total_sentences: int
    completed_sentences: int
    results: list[SentenceResultResponse]


class DashboardStatsResponse(BaseModel):
    total_sessions: int
    total_time_minutes: float
    average_accuracy: float
    total_videos: int
    streak: int


class HistoryEntryResponse(BaseModel):
    id: str
    video_title: str
    type: str
    score: float
    duration_minutes: float
    completed_at: str
