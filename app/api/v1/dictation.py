from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.session import DictationSession, SentenceResult
from app.models.video import Sentence
from app.schemas.dictation import SubmitAnswerRequest, SentenceResultResponse, WordDiffItem
from app.services.dictation_service import compute_word_diff
from app.core.exceptions import NotFoundError

router = APIRouter(prefix="/dictation", tags=["Dictation"])


@router.post("/sessions", status_code=201)
async def create_session(
    video_id: str,
    mode: str = "intermediate",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Count sentences
    result = await db.execute(
        select(Sentence).where(Sentence.video_id == video_id)
    )
    sentences = result.scalars().all()
    if not sentences:
        raise NotFoundError("No sentences found for this video")

    session = DictationSession(
        user_id=current_user.id,
        video_id=video_id,
        mode=mode,
        total_sentences=len(sentences),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return {"session_id": session.id, "total_sentences": session.total_sentences}


@router.post("/sessions/{session_id}/submit", response_model=SentenceResultResponse)
async def submit_answer(
    session_id: str,
    body: SubmitAnswerRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Get session
    result = await db.execute(
        select(DictationSession).where(
            DictationSession.id == session_id,
            DictationSession.user_id == current_user.id,
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise NotFoundError("Session not found")

    # Get correct sentence
    result = await db.execute(
        select(Sentence).where(
            Sentence.video_id == session.video_id,
            Sentence.index == body.sentence_index,
        )
    )
    sentence = result.scalar_one_or_none()
    if not sentence:
        raise NotFoundError("Sentence not found")

    # Compute word diff
    diffs, score = compute_word_diff(body.user_input, sentence.text)

    # Apply hint penalty
    hint_penalty = body.hints_used * 0.05
    final_score = max(0, score - hint_penalty)

    # Save result
    sentence_result = SentenceResult(
        session_id=session_id,
        sentence_index=body.sentence_index,
        user_input=body.user_input,
        correct_text=sentence.text,
        score=final_score,
        word_diffs=[d.model_dump() for d in diffs],
        hints_used=body.hints_used,
        replay_count=body.replay_count,
    )
    db.add(sentence_result)

    # Update session progress
    session.current_index = body.sentence_index + 1
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
