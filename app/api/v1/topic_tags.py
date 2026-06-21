from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.repositories.video_repository import VideoRepository
from app.schemas.video import TopicTagResponse
from app.services.video_service import VideoService

router = APIRouter(prefix="/topic-tags", tags=["Topic Tags"])


def get_video_service(db: AsyncSession = Depends(get_db)) -> VideoService:
    return VideoService(VideoRepository(db))


@router.get("", response_model=list[TopicTagResponse])
async def list_topic_tags(service: VideoService = Depends(get_video_service)):
    return await service.list_topic_tags()
