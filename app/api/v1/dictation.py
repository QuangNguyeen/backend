from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.dictation import DictationAttempt, DictationSentence
from app.models.video import Transcript
from app.schemas.dictation import SubmitAnswerRequest, SentenceResultResponse, WordDiffItem
from app.services.dictation_service import compute_word_diff
from app.core.exceptions import NotFoundError

router = APIRouter(prefix="/dictation", tags=["Dictation"])


@router.post("/sessions", status_code=201)
async def create_session(
    video_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Count transcripts (sentences)
    result = await db.execute(
        select(Transcript).where(Transcript.video_id == video_id)
    )
    transcripts = result.scalars().all()
    if not transcripts:
        raise NotFoundError("No transcripts found for this video")

    attempt = DictationAttempt(
        user_id=current_user.id,
        video_id=video_id,
        status="in_progress",
        total_sentences=len(transcripts),
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return {"session_id": attempt.id, "total_sentences": attempt.total_sentences}


@router.post("/sessions/{session_id}/submit", response_model=SentenceResultResponse)
async def submit_answer(
    session_id: str,
    body: SubmitAnswerRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Get attempt
    result = await db.execute(
        select(DictationAttempt).where(
            DictationAttempt.id == session_id,
            DictationAttempt.user_id == current_user.id,
        )
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise NotFoundError("Session not found")

    # Get correct transcript segment
    result = await db.execute(
        select(Transcript).where(
            Transcript.video_id == attempt.video_id,
            Transcript.index == body.sentence_index,
        )
    )
    transcript = result.scalar_one_or_none()
    if not transcript:
        raise NotFoundError("Transcript segment not found")

    # Compute word diff
    diffs, score = compute_word_diff(body.user_input, transcript.text)

    # Apply hint penalty
    hint_penalty = body.hints_used * 0.05
    final_score = max(0, score - hint_penalty)

    # Save result
    dictation_sentence = DictationSentence(
        attempt_id=session_id,
        sentence_index=body.sentence_index,
        user_input=body.user_input,
        original_text=transcript.text,
        score=final_score,
        word_diff=[d.model_dump() for d in diffs],
        hints_used=body.hints_used,
        replay_count=body.replay_count,
    )
    db.add(dictation_sentence)

    # Update attempt progress
    attempt.current_sentence_index = body.sentence_index + 1
    await db.commit()

    correct = sum(1 for d in diffs if d.status == "correct")
    wrong = sum(1 for d in diffs if d.status == "wrong")
    missing = sum(1 for d in diffs if d.status == "missing")

    return SentenceResultResponse(
        sentence_index=body.sentence_index,
        score=final_score,
        word_diffs=diffs,
        correct_count=correct,
        wrong_count=wrong,
        missing_count=missing,
    )
