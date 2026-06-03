from pydantic import BaseModel


class ReorderToken(BaseModel):
    index: int
    text: str


class ReorderChallenge(BaseModel):
    sentence_index: int
    start_time: float
    end_time: float
    shuffled_tokens: list[ReorderToken]
    token_count: int


class ReorderChallengesResponse(BaseModel):
    practice_mode: str = "reorder"
    total_sentences: int
    challenges: list[ReorderChallenge]


class ReorderSubmitRequest(BaseModel):
    sentence_index: int
    ordered_tokens: list[str]


class ReorderTokenResult(BaseModel):
    index: int
    token: str
    expected: str
    is_correct: bool


class ReorderSentenceResult(BaseModel):
    sentence_index: int
    score: float
    token_results: list[ReorderTokenResult]
    correct_order: list[str]
    start_time: float
    end_time: float


class ReorderSubmitAllRequest(BaseModel):
    answers: list[list[str]]


class ReorderSubmitAllResponse(BaseModel):
    score: float
    correct_count: int
    total_count: int
    sentence_results: list[ReorderSentenceResult]
