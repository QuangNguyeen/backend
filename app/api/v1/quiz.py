import random
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.exceptions import BadRequestError, NotFoundError
from app.database import get_db
from app.models.quiz import QuizSession
from app.models.user import User
from app.models.video import Transcript

router = APIRouter(prefix="/quiz", tags=["Quiz"])

NUM_QUESTIONS = 5
NUM_CHOICES = 4


class CreateQuizRequest(BaseModel):
    video_id: str
    num_questions: int = NUM_QUESTIONS


class AnswerRequest(BaseModel):
    question_index: int
    selected_choice: int


def _generate_questions(transcripts: list[Transcript], num_questions: int) -> list[dict]:
    if len(transcripts) < NUM_CHOICES:
        raise BadRequestError(f"Video needs at least {NUM_CHOICES} transcript segments for quiz")

    candidates = [t for t in transcripts if len(t.text.strip().split()) >= 3]
    if len(candidates) < num_questions:
        candidates = transcripts

    selected = random.sample(candidates, min(num_questions, len(candidates)))
    all_texts = [t.text.strip() for t in transcripts]
    questions = []

    for t in selected:
        correct_text = t.text.strip()
        distractors_pool = [txt for txt in all_texts if txt != correct_text]
        distractors = random.sample(distractors_pool, min(NUM_CHOICES - 1, len(distractors_pool)))

        choices = distractors + [correct_text]
        random.shuffle(choices)
        correct_index = choices.index(correct_text)

        questions.append(
            {
                "question": "What did you hear?",
                "choices": choices,
                "correct_index": correct_index,
                "start_time": t.start_time,
                "end_time": t.end_time,
            }
        )

    return questions


@router.post("/sessions", status_code=201)
async def create_quiz_session(
    body: CreateQuizRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Transcript).where(Transcript.video_id == body.video_id).order_by(Transcript.index)
    )
    transcripts = list(result.scalars().all())
    if not transcripts:
        raise NotFoundError("No transcripts found for this video")

    questions = _generate_questions(transcripts, body.num_questions)

    session = QuizSession(
        user_id=current_user.id,
        video_id=body.video_id,
        total_questions=len(questions),
        questions=questions,
        answers=[None] * len(questions),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    safe_questions = [
        {
            "question": q["question"],
            "choices": q["choices"],
            "start_time": q["start_time"],
            "end_time": q["end_time"],
        }
        for q in questions
    ]

    return {
        "session_id": session.id,
        "video_id": body.video_id,
        "total_questions": len(questions),
        "questions": safe_questions,
    }


@router.post("/sessions/{session_id}/answer")
async def answer_question(
    session_id: str,
    body: AnswerRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = (
        await db.execute(
            select(QuizSession).where(
                QuizSession.id == session_id,
                QuizSession.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if not session:
        raise NotFoundError("Quiz session not found")
    if session.status == "completed":
        raise BadRequestError("Quiz already completed")
    if body.question_index < 0 or body.question_index >= session.total_questions:
        raise BadRequestError("Invalid question index")

    question = session.questions[body.question_index]
    is_correct = body.selected_choice == question["correct_index"]

    answers = list(session.answers)
    answers[body.question_index] = {
        "selected": body.selected_choice,
        "correct_index": question["correct_index"],
        "is_correct": is_correct,
    }
    session.answers = answers
    await db.commit()

    return {
        "question_index": body.question_index,
        "is_correct": is_correct,
        "correct_index": question["correct_index"],
        "correct_text": question["choices"][question["correct_index"]],
    }


@router.post("/sessions/{session_id}/complete")
async def complete_quiz(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = (
        await db.execute(
            select(QuizSession).where(
                QuizSession.id == session_id,
                QuizSession.user_id == current_user.id,
            )
        )
    ).scalar_one_or_none()
    if not session:
        raise NotFoundError("Quiz session not found")

    if session.status == "completed":
        return {
            "status": "completed",
            "score": session.score,
            "correct_count": session.correct_count,
            "total_questions": session.total_questions,
        }

    answers = session.answers or []
    correct_count = sum(1 for a in answers if a and a.get("is_correct"))
    score = correct_count / session.total_questions if session.total_questions else 0.0

    session.status = "completed"
    session.correct_count = correct_count
    session.score = score
    session.completed_at = datetime.now(UTC)
    if session.created_at:
        session.duration_seconds = int((session.completed_at - session.created_at).total_seconds())

    await db.commit()

    return {
        "status": "completed",
        "score": round(score * 100, 1),
        "correct_count": correct_count,
        "total_questions": session.total_questions,
        "answers": answers,
    }


@router.get("/sessions")
async def list_quiz_sessions(
    video_id: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    filters = [QuizSession.user_id == current_user.id]
    if video_id:
        filters.append(QuizSession.video_id == video_id)
    if status:
        filters.append(QuizSession.status == status)

    from sqlalchemy import func as sa_func

    total = (
        await db.execute(select(sa_func.count()).select_from(QuizSession).where(*filters))
    ).scalar() or 0

    rows = (
        (
            await db.execute(
                select(QuizSession)
                .where(*filters)
                .order_by(QuizSession.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    return {
        "items": [
            {
                "session_id": s.id,
                "video_id": s.video_id,
                "status": s.status,
                "score": round(s.score * 100, 1) if s.score is not None else None,
                "correct_count": s.correct_count,
                "total_questions": s.total_questions,
                "duration_seconds": s.duration_seconds,
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
                "created_at": s.created_at.isoformat(),
            }
            for s in rows
        ],
        "total": total,
        "page": page,
    }
