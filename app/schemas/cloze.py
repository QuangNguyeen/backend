from typing import Literal

from pydantic import BaseModel

PracticeMode = Literal["sentence", "cloze", "reorder"]
ClozeDifficulty = Literal["easy", "medium", "hard"]


class ClozeToken(BaseModel):
    text: str
    is_blank: bool
    blank_index: int | None = None


# ─── Legacy chunk-based types (kept for backward compat) ────────────────────


class ClozeChunk(BaseModel):
    chunk_index: int
    start_time: float
    end_time: float
    tokens: list[ClozeToken]
    blank_count: int


class ClozeChunksResponse(BaseModel):
    practice_mode: PracticeMode = "cloze"
    chunks: list[ClozeChunk]


class ClozeSubmitRequest(BaseModel):
    chunk_index: int
    answers: list[str]


class ClozeBlankResult(BaseModel):
    blank_index: int
    given: str
    expected: str
    status: Literal["correct", "wrong"]


class ClozeResultResponse(BaseModel):
    chunk_index: int
    score: float
    correct_count: int
    total_count: int
    results: list[ClozeBlankResult]
    audio_start_time: float
    audio_end_time: float


# ─── New full-transcript cloze types ────────────────────────────────────────


class ClozeSegment(BaseModel):
    segment_index: int
    start_time: float
    end_time: float
    tokens: list[ClozeToken]
    blank_count: int


class ClozeFullResponse(BaseModel):
    practice_mode: PracticeMode = "cloze"
    difficulty: str
    total_blanks: int
    segments: list[ClozeSegment]


class ClozeSubmitAllRequest(BaseModel):
    difficulty: str
    answers: list[str]


class SegmentScore(BaseModel):
    segment_index: int
    start_time: float
    end_time: float
    score: float
    blank_results: list[ClozeBlankResult]


class ClozeSubmitAllResponse(BaseModel):
    score: float
    correct_count: int
    total_count: int
    results: list[ClozeBlankResult]
    segment_scores: list[SegmentScore]
