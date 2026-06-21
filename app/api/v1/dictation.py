from enum import StrEnum

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.database import get_db
from app.models.user import User
from app.repositories.dictation_repository import DictationRepository
from app.schemas.cloze import (
    ClozeChunksResponse,
    ClozeFullResponse,
    ClozeResultResponse,
    ClozeSubmitAllRequest,
    ClozeSubmitAllResponse,
    ClozeSubmitRequest,
    PracticeMode,
)
from app.schemas.dictation import (
    HistoryPaginatedResponse,
    SentenceResultResponse,
    SubmitAnswerRequest,
)
from app.schemas.reorder import (
    ReorderChallengesResponse,
    ReorderSentenceResult,
    ReorderSubmitAllRequest,
    ReorderSubmitAllResponse,
    ReorderSubmitRequest,
)
from app.services.dictation_session_service import DictationService

router = APIRouter(prefix="/dictation", tags=["Dictation"])


class AttemptStatus(StrEnum):
    in_progress = "in-progress"
    completed = "completed"


def get_dictation_service(db: AsyncSession = Depends(get_db)) -> DictationService:
    return DictationService(DictationRepository(db))


@router.post("/sessions", status_code=201)
async def create_session(
    video_id: str,
    practice_mode: PracticeMode = "sentence",
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    return await service.create_session(current_user, video_id, practice_mode)


@router.post("/sessions/{session_id}/submit", response_model=SentenceResultResponse)
async def submit_answer(
    session_id: str,
    body: SubmitAnswerRequest,
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    return await service.submit_answer(current_user, session_id, body)


@router.post("/sessions/{session_id}/complete")
async def complete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    """Explicitly mark a session as completed. Idempotent safety net."""
    return await service.complete_session(current_user, session_id)


@router.get("/attempts/{status}", response_model=HistoryPaginatedResponse)
async def get_history(
    status: AttemptStatus,
    page: int = Query(1, ge=1),
    video_id: str | None = Query(None),
    practice_mode: str | None = Query(None),
    from_date: str | None = Query(None, description="ISO date, e.g. 2026-01-01"),
    to_date: str | None = Query(None, description="ISO date, e.g. 2026-12-31"),
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    return await service.get_history(
        current_user, status.value, page, video_id, practice_mode, from_date, to_date
    )


@router.get("/attempts-summary")
async def get_attempts_summary(
    video_id: str | None = Query(None),
    practice_mode: str | None = Query(None),
    from_date: str | None = Query(None),
    to_date: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    """Aggregate stats for the history page: total sessions, total time, avg score."""
    return await service.get_attempts_summary(
        current_user, video_id, practice_mode, from_date, to_date
    )


@router.get("/sessions/{session_id}/cloze", response_model=ClozeChunksResponse)
async def get_cloze_chunks(
    session_id: str,
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    """Build paragraph chunks with blanks for cloze-mode practice."""
    return await service.get_cloze_chunks(current_user, session_id)


@router.post("/sessions/{session_id}/cloze-submit", response_model=ClozeResultResponse)
async def submit_cloze(
    session_id: str,
    body: ClozeSubmitRequest,
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    """Score a chunk's blanks (case-insensitive, trimmed) and persist progress."""
    return await service.submit_cloze(current_user, session_id, body)


@router.get("/sessions/{session_id}/cloze-full", response_model=ClozeFullResponse)
async def get_cloze_full(
    session_id: str,
    difficulty: str = Query("medium"),
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    """Build the full-transcript cloze view with difficulty-based blank selection."""
    return await service.get_cloze_full(current_user, session_id, difficulty)


@router.post("/sessions/{session_id}/cloze-submit-all", response_model=ClozeSubmitAllResponse)
async def submit_cloze_all(
    session_id: str,
    body: ClozeSubmitAllRequest,
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    """Score ALL blanks at once and mark the session completed."""
    return await service.submit_cloze_all(current_user, session_id, body)


@router.get(
    "/sessions/{session_id}/reorder-challenges",
    response_model=ReorderChallengesResponse,
)
async def get_reorder_challenges(
    session_id: str,
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    """Return shuffled tokens for each transcript sentence in the session."""
    return await service.get_reorder_challenges(current_user, session_id)


@router.post(
    "/sessions/{session_id}/reorder-submit",
    response_model=ReorderSentenceResult,
)
async def submit_reorder(
    session_id: str,
    body: ReorderSubmitRequest,
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    """Score a single reordered sentence and persist progress."""
    return await service.submit_reorder(current_user, session_id, body)


@router.post(
    "/sessions/{session_id}/reorder-submit-all",
    response_model=ReorderSubmitAllResponse,
)
async def submit_reorder_all(
    session_id: str,
    body: ReorderSubmitAllRequest,
    current_user: User = Depends(get_current_user),
    service: DictationService = Depends(get_dictation_service),
):
    """Score ALL reordered sentences at once and mark session completed."""
    return await service.submit_reorder_all(current_user, session_id, body)