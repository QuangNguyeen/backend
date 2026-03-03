from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.vocabulary import SavedWord
from app.schemas.vocabulary import (
    SaveWordRequest, UpdateWordRequest, SavedWordResponse,
    ReviewRequest, ReviewResponse,
    FlashCardResponse, DueCardsResponse,
)
from app.services.srs_service import calculate_next_review
from app.core.exceptions import NotFoundError

router = APIRouter(prefix="/vocabulary", tags=["Vocabulary"])


@router.post("/save", response_model=SavedWordResponse, status_code=status.HTTP_201_CREATED)
async def save_word(
    body: SaveWordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save a word from dictation/quiz for SRS review."""
    word = SavedWord(
        user_id=current_user.id,
        word=body.word,
        video_id=body.video_id,
        context_sentence=body.context_sentence,
        audio_start_time=body.audio_start_time,
        meaning=body.meaning,
        note=body.note,
        source=body.source,
        next_review_at=datetime.now(timezone.utc),  # Due immediately
    )
    db.add(word)
    await db.commit()
    await db.refresh(word)
    return word


@router.get("", response_model=list[SavedWordResponse])
async def list_words(
    video_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all saved words for the current user."""
    query = select(SavedWord).where(SavedWord.user_id == current_user.id)
    if video_id:
        query = query.where(SavedWord.video_id == video_id)
    query = query.order_by(SavedWord.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/due", response_model=DueCardsResponse)
async def get_due_cards(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get FlashCards due for review today."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(SavedWord)
        .where(
            SavedWord.user_id == current_user.id,
            SavedWord.next_review_at <= now,
        )
        .order_by(SavedWord.next_review_at)
        .limit(50)
    )
    cards = result.scalars().all()

    # Count total due
    count_result = await db.execute(
        select(func.count()).where(
            SavedWord.user_id == current_user.id,
            SavedWord.next_review_at <= now,
        )
    )
    total_due = count_result.scalar() or 0

    return DueCardsResponse(
        cards=[FlashCardResponse.model_validate(c) for c in cards],
        total_due=total_due,
    )


@router.post("/{word_id}/review", response_model=ReviewResponse)
async def review_word(
    word_id: str,
    body: ReviewRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Submit a FlashCard review with SM-2 quality rating (1-5)."""
    result = await db.execute(
        select(SavedWord).where(
            SavedWord.id == word_id,
            SavedWord.user_id == current_user.id,
        )
    )
    word = result.scalar_one_or_none()
    if not word:
        raise NotFoundError("Word not found")

    # Calculate next review using SM-2
    srs = calculate_next_review(
        quality=body.quality,
        repetitions=word.repetitions,
        ease_factor=word.ease_factor,
        interval_days=word.interval_days,
    )

    # Update word
    word.repetitions = srs.repetitions
    word.ease_factor = srs.ease_factor
    word.interval_days = srs.interval_days
    word.next_review_at = srs.next_review_at
    word.last_reviewed_at = datetime.now(timezone.utc)

    await db.commit()

    return ReviewResponse(
        word_id=word.id,
        next_review_at=srs.next_review_at,
        interval_days=srs.interval_days,
        ease_factor=srs.ease_factor,
        repetitions=srs.repetitions,
    )


@router.patch("/{word_id}", response_model=SavedWordResponse)
async def update_word(
    word_id: str,
    body: UpdateWordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update meaning or note for a saved word."""
    result = await db.execute(
        select(SavedWord).where(
            SavedWord.id == word_id,
            SavedWord.user_id == current_user.id,
        )
    )
    word = result.scalar_one_or_none()
    if not word:
        raise NotFoundError("Word not found")

    if body.meaning is not None:
        word.meaning = body.meaning
    if body.note is not None:
        word.note = body.note

    await db.commit()
    await db.refresh(word)
    return word


@router.delete("/{word_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_word(
    word_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a saved word."""
    result = await db.execute(
        select(SavedWord).where(
            SavedWord.id == word_id,
            SavedWord.user_id == current_user.id,
        )
    )
    word = result.scalar_one_or_none()
    if not word:
        raise NotFoundError("Word not found")

    await db.delete(word)
    await db.commit()
