import math
from enum import Enum

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func as sa_func

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.dictation import DictationAttempt, DictationSentence
from app.models.video import Video, Transcript
from app.schemas.dictation import (
    SubmitAnswerRequest,
    SentenceResultResponse,
    WordDiffItem,
    HistoryAttemptResponse,
    HistoryPaginatedResponse,
)
from app.services.dictation_service import compute_word_diff
from app.services.text_analysis_service import get_word_difficulty_map
from app.core.exceptions import NotFoundError


class AttemptStatus(str, Enum):
    in_progress = "in-progress"
    completed = "completed"

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

    total_sentences = len(transcripts)

    # Check for existing attempt for this user + video
    result = await db.execute(
        select(DictationAttempt).where(
            DictationAttempt.user_id == current_user.id,
            DictationAttempt.video_id == video_id,
        )
    )
    attempt = result.scalar_one_or_none()

    if attempt and attempt.status == "in_progress":
        # Case A: Resume — return existing attempt with saved sentence results
        sentence_results = await db.execute(
            select(DictationSentence)
            .where(DictationSentence.attempt_id == attempt.id)
            .order_by(DictationSentence.sentence_index)
        )
        sentences = sentence_results.scalars().all()
        return {
            "session_id": attempt.id,
            "total_sentences": attempt.total_sentences,
            "current_sentence_index": attempt.current_sentence_index,
            "resumed": True,
            "sentence_results": [
                {
                    "sentence_index": s.sentence_index,
                    "score": s.score,
                    "word_diff": s.word_diff,
                }
                for s in sentences
            ],
        }

    if attempt and attempt.status == "completed":
        # Case B: Re-practice — reset the existing row
        attempt.status = "in_progress"
        attempt.score = None
        attempt.current_sentence_index = 0
        attempt.total_sentences = total_sentences
        attempt.error_summary = None
        attempt.completed_at = None
        attempt.correct_words = None
        attempt.total_words = None
        attempt.duration_seconds = None
        # Delete old sentence results
        old_sentences = await db.execute(
            select(DictationSentence).where(DictationSentence.attempt_id == attempt.id)
        )
        for s in old_sentences.scalars().all():
            await db.delete(s)
        await db.commit()
        await db.refresh(attempt)
        return {
            "session_id": attempt.id,
            "total_sentences": attempt.total_sentences,
            "current_sentence_index": 0,
            "resumed": False,
            "sentence_results": [],
        }

    # Case C: First time — create new row
    attempt = DictationAttempt(
        user_id=current_user.id,
        video_id=video_id,
        status="in_progress",
        total_sentences=total_sentences,
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return {
        "session_id": attempt.id,
        "total_sentences": attempt.total_sentences,
        "current_sentence_index": 0,
        "resumed": False,
        "sentence_results": [],
    }


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

    # Upsert sentence result (update if same sentence submitted again)
    existing_sentence = (await db.execute(
        select(DictationSentence).where(
            DictationSentence.attempt_id == session_id,
            DictationSentence.sentence_index == body.sentence_index,
        )
    )).scalar_one_or_none()

    if existing_sentence:
        existing_sentence.user_input = body.user_input
        existing_sentence.original_text = transcript.text
        existing_sentence.score = final_score
        existing_sentence.word_diff = [d.model_dump() for d in diffs]
        existing_sentence.hints_used = body.hints_used
        existing_sentence.replay_count = body.replay_count
    else:
        db.add(DictationSentence(
            attempt_id=session_id,
            sentence_index=body.sentence_index,
            user_input=body.user_input,
            original_text=transcript.text,
            score=final_score,
            word_diff=[d.model_dump() for d in diffs],
            hints_used=body.hints_used,
            replay_count=body.replay_count,
        ))

    # Advance progress — only move forward, never backwards
    new_index = body.sentence_index + 1
    if new_index > attempt.current_sentence_index:
        attempt.current_sentence_index = new_index

    # Auto-complete when all sentences are done
    if attempt.total_sentences and attempt.current_sentence_index >= attempt.total_sentences:
        from datetime import datetime, timezone

        # Compute overall score from all sentence results
        all_sentences = (await db.execute(
            select(DictationSentence).where(DictationSentence.attempt_id == session_id)
        )).scalars().all()
        scores = [s.score for s in all_sentences if s.score is not None]
        attempt.score = sum(scores) / len(scores) if scores else 0.0
        attempt.status = "completed"
        attempt.completed_at = datetime.now(timezone.utc)

    await db.commit()

    correct = sum(1 for d in diffs if d.status == "correct")
    wrong = sum(1 for d in diffs if d.status == "wrong")
    missing = sum(1 for d in diffs if d.status == "missing")
    difficulty_map = get_word_difficulty_map(transcript.text, language=transcript.language)

    return SentenceResultResponse(
        sentence_index=body.sentence_index,
        score=final_score,
        word_diffs=diffs,
        correct_count=correct,
        wrong_count=wrong,
        missing_count=missing,
        original_text=transcript.text,
        video_id=attempt.video_id,
        audio_start_time=transcript.start_time,
        word_difficulty=difficulty_map,
    )


@router.post("/sessions/{session_id}/complete")
async def complete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Explicitly mark a session as completed. Idempotent safety net."""
    from datetime import datetime, timezone

    result = await db.execute(
        select(DictationAttempt).where(
            DictationAttempt.id == session_id,
            DictationAttempt.user_id == current_user.id,
        )
    )
    attempt = result.scalar_one_or_none()
    if not attempt:
        raise NotFoundError("Session not found")

    if attempt.status == "completed":
        return {"status": "completed", "score": round((attempt.score or 0) * 100, 1)}

    # Compute overall score from all sentence results
    all_sentences = (await db.execute(
        select(DictationSentence).where(DictationSentence.attempt_id == session_id)
    )).scalars().all()
    scores = [s.score for s in all_sentences if s.score is not None]
    attempt.score = sum(scores) / len(scores) if scores else 0.0
    attempt.status = "completed"
    attempt.completed_at = datetime.now(timezone.utc)
    attempt.current_sentence_index = attempt.total_sentences or len(all_sentences)
    await db.commit()

    return {"status": "completed", "score": round(attempt.score * 100, 1)}


PAGE_SIZE = 20


@router.get("/attempts/{status}", response_model=HistoryPaginatedResponse)
async def get_history(
    status: AttemptStatus,
    page: int = Query(1, ge=1),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Map URL slug to DB value (in-progress → in_progress)
    db_status = status.value.replace("-", "_")

    # Base filter
    base_filter = [
        DictationAttempt.user_id == current_user.id,
        DictationAttempt.status == db_status,
    ]

    # Count total
    count_q = select(sa_func.count()).select_from(DictationAttempt).where(*base_filter)
    total = (await db.execute(count_q)).scalar() or 0
    total_pages = max(1, math.ceil(total / PAGE_SIZE))

    # Order depends on status
    if status == AttemptStatus.completed:
        order_col = DictationAttempt.completed_at.desc()
    else:
        order_col = DictationAttempt.updated_at.desc()

    # Query with join to Video
    query = (
        select(DictationAttempt, Video.title, Video.thumbnail_url)
        .join(Video, DictationAttempt.video_id == Video.id)
        .where(*base_filter)
        .order_by(order_col)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    rows = (await db.execute(query)).all()

    items = []
    for attempt, video_title, video_thumbnail in rows:
        progress_str = f"{attempt.current_sentence_index}/{attempt.total_sentences or 0}"
        items.append(
            HistoryAttemptResponse(
                attempt_id=attempt.id,
                video_id=attempt.video_id,
                status=attempt.status,
                score=attempt.score,
                progress_str=progress_str,
                video_title=video_title,
                video_thumbnail=video_thumbnail or "",
                error_summary=attempt.error_summary,
                updated_at=attempt.updated_at.isoformat(),
                completed_at=attempt.completed_at.isoformat() if attempt.completed_at else None,
            )
        )

    return HistoryPaginatedResponse(
        items=items,
        total=total,
        page=page,
        total_pages=total_pages,
    )
