from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.api.deps import get_current_user
from app.models.user import User
from app.models.video import Video, Sentence
from app.schemas.video import VideoResponse, ImportVideoRequest, SentenceResponse
from app.core.exceptions import NotFoundError

router = APIRouter(prefix="/videos", tags=["Videos"])


@router.get("", response_model=list[VideoResponse])
async def list_videos(
    language: str | None = None,
    level: str | None = None,
    curated: bool | None = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Video)
    if language:
        query = query.where(Video.language == language)
    if level:
        query = query.where(Video.level == level)
    if curated is not None:
        query = query.where(Video.is_curated == curated)

    result = await db.execute(query.order_by(Video.created_at.desc()))
    return result.scalars().all()


@router.get("/{video_id}", response_model=VideoResponse)
async def get_video(video_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")
    return video


@router.get("/{video_id}/sentences", response_model=list[SentenceResponse])
async def get_sentences(video_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Sentence).where(Sentence.video_id == video_id).order_by(Sentence.index)
    )
    return result.scalars().all()


@router.post("/import", response_model=VideoResponse, status_code=201)
async def import_video(
    body: ImportVideoRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # TODO: Implement YouTube import + transcript extraction
    # This will use youtube_service.py and youtube-transcript-api
    raise NotFoundError("Import not yet implemented — coming soon")
