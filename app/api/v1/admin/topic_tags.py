from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user
from app.database import get_db
from app.models.user import User
from app.repositories.video_repository import VideoRepository
from app.schemas.video import AdminTopicTagCreate, AdminTopicTagPatch, TopicTagResponse
from app.services.video_service import VideoService

router = APIRouter(prefix="/topic-tags")


def get_video_service(db: AsyncSession = Depends(get_db)) -> VideoService:
    return VideoService(VideoRepository(db))


@router.get("", response_model=list[TopicTagResponse])
async def list_admin_topic_tags(
    include_inactive: bool = Query(True),
    _admin: User = Depends(get_admin_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.list_admin_topic_tags(include_inactive=include_inactive)


@router.post("", response_model=TopicTagResponse, status_code=201)
async def create_topic_tag(
    body: AdminTopicTagCreate,
    _admin: User = Depends(get_admin_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.create_topic_tag(body)


@router.patch("/{tag_id}", response_model=TopicTagResponse)
async def patch_topic_tag(
    tag_id: str,
    body: AdminTopicTagPatch,
    _admin: User = Depends(get_admin_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.patch_topic_tag(tag_id, body)


@router.delete("/{tag_id}", status_code=204)
async def deactivate_topic_tag(
    tag_id: str,
    _admin: User = Depends(get_admin_user),
    service: VideoService = Depends(get_video_service),
):
    await service.deactivate_topic_tag(tag_id)
    return Response(status_code=204)
