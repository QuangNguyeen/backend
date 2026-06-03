from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import Date, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user
from app.database import get_db
from app.models.dictation import DictationAttempt
from app.models.user import User
from app.models.video import Video
from app.models.vocabulary import SavedWord
from app.schemas.admin import AdminStatsResponse

router = APIRouter(prefix="/stats")


async def _count(db: AsyncSession, query) -> int:
    return (await db.execute(query)).scalar() or 0


@router.get("", response_model=AdminStatsResponse)
async def get_admin_stats(
    _current_admin: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    today = date.today()

    return AdminStatsResponse(
        total_users=await _count(db, select(func.count()).select_from(User)),
        total_videos=await _count(db, select(func.count()).select_from(Video)),
        total_sessions=await _count(db, select(func.count()).select_from(DictationAttempt)),
        total_vocabulary_words=await _count(
            db,
            select(func.count()).where(SavedWord.deleted_at.is_(None)),
        ),
        pending_transcriptions=await _count(
            db,
            select(func.count()).where(Video.transcription_status == "pending"),
        ),
        failed_transcriptions=await _count(
            db,
            select(func.count()).where(Video.transcription_status == "failed"),
        ),
        new_users_today=await _count(
            db,
            select(func.count()).where(cast(User.created_at, Date) == today),
        ),
        sessions_today=await _count(
            db,
            select(func.count(func.distinct(User.id)))
            .select_from(User)
            .outerjoin(DictationAttempt, DictationAttempt.user_id == User.id)
            .where(
                or_(
                    cast(User.last_login_at, Date) == today,
                    cast(DictationAttempt.updated_at, Date) == today,
                )
            ),
        ),
    )
