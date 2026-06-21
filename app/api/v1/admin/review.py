from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user
from app.database import get_db
from app.models.user import User
from app.repositories.video_repository import VideoRepository
from app.schemas.video import (
    AdminFeedbackPatch,
    AdminPublishRequestAction,
    AdminTranscriptFeedbackListResponse,
    TranscriptFeedbackResponse,
    VideoResponse,
    VideoTopicTagsUpdate,
)
from app.services.video_service import VideoService

router = APIRouter()


def get_video_service(db: AsyncSession = Depends(get_db)) -> VideoService:
    return VideoService(VideoRepository(db))


@router.get("/videos/publish-requests")
async def list_publish_requests(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _admin: User = Depends(get_admin_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.list_publish_requests(status, page, page_size)


@router.post("/videos/publish-requests/{request_id}/approve")
async def approve_publish_request(
    request_id: str,
    body: AdminPublishRequestAction,
    admin: User = Depends(get_admin_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.approve_publish_request(admin, request_id, body)


@router.post("/videos/publish-requests/{request_id}/reject")
async def reject_publish_request(
    request_id: str,
    body: AdminPublishRequestAction,
    admin: User = Depends(get_admin_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.reject_publish_request(admin, request_id, body)


@router.put("/videos/{video_id}/topic-tags", response_model=VideoResponse)
async def update_video_public_tags(
    video_id: str,
    body: VideoTopicTagsUpdate,
    admin: User = Depends(get_admin_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.update_video_public_tags(admin, video_id, body)


@router.get("/transcript-feedback", response_model=AdminTranscriptFeedbackListResponse)
async def list_transcript_feedback(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _admin: User = Depends(get_admin_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.list_transcript_feedback(status, page, page_size)


@router.patch("/transcript-feedback/{feedback_id}", response_model=TranscriptFeedbackResponse)
async def patch_transcript_feedback(
    feedback_id: str,
    body: AdminFeedbackPatch,
    admin: User = Depends(get_admin_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.patch_transcript_feedback(admin, feedback_id, body)
