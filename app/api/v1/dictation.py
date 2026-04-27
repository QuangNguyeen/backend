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
from app.schemas.cloze import (
    ClozeChunksResponse,
    ClozeBlankResult,
    ClozeResultResponse,
    ClozeSubmitRequest,
    ClozeFullResponse,
    ClozeSubmitAllRequest,
    ClozeSubmitAllResponse,
    SegmentScore,
    PracticeMode,
)
from app.schemas.dictation import (
    SubmitAnswerRequest,
    SentenceResultResponse,
    WordDiffItem,
    HistoryAttemptResponse,
    HistoryPaginatedResponse,
)
from app.services.cloze_service import (
    build_chunks,
    get_chunk_answers,
    build_full_cloze,
    get_full_cloze_answers,
)
from app.services.dictation_service import compute_word_diff
from app.services.text_analysis_service import get_word_difficulty_map
from app.core.exceptions import NotFoundError, BadRequestError


class AttemptStatus(str, Enum):
    in_progress = "in-progress"
    completed = "completed"

router = APIRouter(prefix="/dictation", tags=["Dictation"])


def _session_payload(attempt: DictationAttempt, *, resumed: bool, sentence_results: list[dict]) -> dict:
    return {
        "session_id": attempt.id,
        "total_sentences": attempt.total_sentences,
        "current_sentence_index": attempt.current_sentence_index if resumed else 0,
        "resumed": resumed,
        "sentence_results": sentence_results,
        "practice_mode": attempt.practice_mode,
    }


@router.post("/sessions", status_code=201)
async def create_session(
    video_id: str,
    practice_mode: PracticeMode = "sentence",
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
        # Mode-switch detection: if the user picked a different mode, reset
        # the in-progress session rather than resuming with stale data.
        if attempt.practice_mode != practice_mode:
            attempt.score = None
            attempt.current_sentence_index = 0
            attempt.total_sentences = total_sentences
            attempt.practice_mode = practice_mode
            attempt.error_summary = None
            attempt.completed_at = None
            attempt.correct_words = None
            attempt.total_words = None
            attempt.duration_seconds = None
            old_sentences = await db.execute(
                select(DictationSentence).where(DictationSentence.attempt_id == attempt.id)
            )
            for s in old_sentences.scalars().all():
                await db.delete(s)
            await db.commit()
            await db.refresh(attempt)
            return _session_payload(attempt, resumed=False, sentence_results=[])

        # Same mode — resume where the user left off
        sentence_results = await db.execute(
            select(DictationSentence)
            .where(DictationSentence.attempt_id == attempt.id)
            .order_by(DictationSentence.sentence_index)
        )
        sentences = sentence_results.scalars().all()
        return _session_payload(
            attempt,
            resumed=True,
            sentence_results=[
                {
                    "sentence_index": s.sentence_index,
                    "score": s.score,
                    "word_diff": s.word_diff,
                }
                for s in sentences
            ],
        )

    if attempt and attempt.status == "completed":
        # Re-practice — reset the existing row, including the mode
        attempt.status = "in_progress"
        attempt.score = None
        attempt.current_sentence_index = 0
        attempt.total_sentences = total_sentences
        attempt.practice_mode = practice_mode
        attempt.error_summary = None
        attempt.completed_at = None
        attempt.correct_words = None
        attempt.total_words = None
        attempt.duration_seconds = None
        old_sentences = await db.execute(
            select(DictationSentence).where(DictationSentence.attempt_id == attempt.id)
        )
        for s in old_sentences.scalars().all():
            await db.delete(s)
        await db.commit()
        await db.refresh(attempt)
        return _session_payload(attempt, resumed=False, sentence_results=[])

    # First time — create new row
    attempt = DictationAttempt(
        user_id=current_user.id,
        video_id=video_id,
        status="in_progress",
        total_sentences=total_sentences,
        practice_mode=practice_mode,
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return _session_payload(attempt, resumed=False, sentence_results=[])


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


# ─── Cloze (paragraph fill-in-the-blanks) ─────────────────────────────────────


async def _load_attempt(
    session_id: str, user_id: str, db: AsyncSession,
) -> DictationAttempt:
    attempt = (await db.execute(
        select(DictationAttempt).where(
            DictationAttempt.id == session_id,
            DictationAttempt.user_id == user_id,
        )
    )).scalar_one_or_none()
    if not attempt:
        raise NotFoundError("Session not found")
    return attempt


async def _load_transcripts(video_id: str, db: AsyncSession) -> list[Transcript]:
    rows = (await db.execute(
        select(Transcript)
        .where(Transcript.video_id == video_id)
        .order_by(Transcript.index)
    )).scalars().all()
    if not rows:
        raise NotFoundError("No transcripts found for this video")
    return list(rows)


@router.get("/sessions/{session_id}/cloze", response_model=ClozeChunksResponse)
async def get_cloze_chunks(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Build paragraph chunks with blanks for cloze-mode practice."""
    attempt = await _load_attempt(session_id, current_user.id, db)
    transcripts = await _load_transcripts(attempt.video_id, db)
    chunks = build_chunks(transcripts)
    return ClozeChunksResponse(practice_mode="cloze", chunks=chunks)


@router.post("/sessions/{session_id}/cloze-submit", response_model=ClozeResultResponse)
async def submit_cloze(
    session_id: str,
    body: ClozeSubmitRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Score a chunk's blanks (case-insensitive, trimmed) and persist progress."""
    attempt = await _load_attempt(session_id, current_user.id, db)
    transcripts = await _load_transcripts(attempt.video_id, db)
    chunks = build_chunks(transcripts)

    if body.chunk_index < 0 or body.chunk_index >= len(chunks):
        raise BadRequestError("chunk_index out of range")

    chunk = chunks[body.chunk_index]
    expected = get_chunk_answers(chunk)

    # Pad/truncate user's answers to the expected length so we always score
    # against the same number of blanks the server believes exist.
    given = list(body.answers) + [""] * max(0, len(expected) - len(body.answers))
    given = given[: len(expected)]

    results: list[ClozeBlankResult] = []
    correct_count = 0
    for i, (exp, got) in enumerate(zip(expected, given)):
        is_correct = exp.strip().lower() == got.strip().lower() and got.strip() != ""
        if is_correct:
            correct_count += 1
        results.append(ClozeBlankResult(
            blank_index=i,
            given=got,
            expected=exp,
            status="correct" if is_correct else "wrong",
        ))

    score = correct_count / len(expected) if expected else 1.0

    # Persist as a DictationSentence row keyed by chunk_index. Reuses the existing
    # aggregation pipeline (avg score, streak counting) without further changes.
    existing = (await db.execute(
        select(DictationSentence).where(
            DictationSentence.attempt_id == session_id,
            DictationSentence.sentence_index == body.chunk_index,
        )
    )).scalar_one_or_none()

    word_diff_payload = [r.model_dump() for r in results]
    user_input_joined = " ".join(given).strip()

    if existing:
        existing.user_input = user_input_joined
        existing.original_text = " ".join(expected)
        existing.score = score
        existing.word_diff = word_diff_payload
    else:
        db.add(DictationSentence(
            attempt_id=session_id,
            sentence_index=body.chunk_index,
            user_input=user_input_joined,
            original_text=" ".join(expected),
            score=score,
            word_diff=word_diff_payload,
        ))

    new_index = body.chunk_index + 1
    if new_index > attempt.current_sentence_index:
        attempt.current_sentence_index = new_index

    # Cloze treats total_sentences as total chunks for completion tracking
    if attempt.total_sentences != len(chunks):
        attempt.total_sentences = len(chunks)

    if attempt.current_sentence_index >= len(chunks):
        from datetime import datetime, timezone

        all_sentences = (await db.execute(
            select(DictationSentence).where(DictationSentence.attempt_id == session_id)
        )).scalars().all()
        scores = [s.score for s in all_sentences if s.score is not None]
        attempt.score = sum(scores) / len(scores) if scores else 0.0
        attempt.status = "completed"
        attempt.completed_at = datetime.now(timezone.utc)

    await db.commit()

    return ClozeResultResponse(
        chunk_index=body.chunk_index,
        score=score,
        correct_count=correct_count,
        total_count=len(expected),
        results=results,
        audio_start_time=chunk.start_time,
        audio_end_time=chunk.end_time,
    )


# ─── Full-transcript cloze (new) ─────────────────────────────────────────────


@router.get("/sessions/{session_id}/cloze-full", response_model=ClozeFullResponse)
async def get_cloze_full(
    session_id: str,
    difficulty: str = Query("medium"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Build the full-transcript cloze view with difficulty-based blank selection."""
    attempt = await _load_attempt(session_id, current_user.id, db)
    transcripts = await _load_transcripts(attempt.video_id, db)
    segments, total_blanks = build_full_cloze(transcripts, difficulty)
    return ClozeFullResponse(
        practice_mode="cloze",
        difficulty=difficulty,
        total_blanks=total_blanks,
        segments=segments,
    )


@router.post("/sessions/{session_id}/cloze-submit-all", response_model=ClozeSubmitAllResponse)
async def submit_cloze_all(
    session_id: str,
    body: ClozeSubmitAllRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Score ALL blanks at once and mark the session completed."""
    from datetime import datetime, timezone

    attempt = await _load_attempt(session_id, current_user.id, db)
    transcripts = await _load_transcripts(attempt.video_id, db)
    segments, total_blanks = build_full_cloze(transcripts, body.difficulty)
    expected = get_full_cloze_answers(segments)

    given = list(body.answers) + [""] * max(0, len(expected) - len(body.answers))
    given = given[: len(expected)]

    results: list[ClozeBlankResult] = []
    correct_count = 0
    for i, (exp, got) in enumerate(zip(expected, given)):
        is_correct = exp.strip().lower() == got.strip().lower() and got.strip() != ""
        if is_correct:
            correct_count += 1
        results.append(ClozeBlankResult(
            blank_index=i,
            given=got,
            expected=exp,
            status="correct" if is_correct else "wrong",
        ))

    score = correct_count / len(expected) if expected else 1.0

    blank_to_result: dict[int, ClozeBlankResult] = {r.blank_index: r for r in results}
    segment_scores: list[SegmentScore] = []
    for seg in segments:
        seg_results = [
            blank_to_result[tok.blank_index]
            for tok in seg.tokens
            if tok.is_blank and tok.blank_index is not None and tok.blank_index in blank_to_result
        ]
        seg_correct = sum(1 for r in seg_results if r.status == "correct")
        seg_score = seg_correct / len(seg_results) if seg_results else 1.0
        segment_scores.append(SegmentScore(
            segment_index=seg.segment_index,
            start_time=seg.start_time,
            end_time=seg.end_time,
            score=seg_score,
            blank_results=seg_results,
        ))

    attempt.score = score
    attempt.status = "completed"
    attempt.completed_at = datetime.now(timezone.utc)
    attempt.current_sentence_index = attempt.total_sentences or 0
    await db.commit()

    return ClozeSubmitAllResponse(
        score=score,
        correct_count=correct_count,
        total_count=len(expected),
        results=results,
        segment_scores=segment_scores,
    )
