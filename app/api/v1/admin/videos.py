from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user
from app.database import get_db
from app.models.user import User
from app.repositories.admin_video_repository import AdminVideoRepository
from app.schemas.admin import (
    AdminDifficultyRecalculationResponse,
    AdminPatchVideoRequest,
    AdminRetryTranscriptionResponse,
    AdminVideoListResponse,
    AdminVideoResponse,
)
from app.services.admin_video_service import AdminVideoService

router = APIRouter(prefix="/videos")


def get_admin_video_service(db: AsyncSession = Depends(get_db)) -> AdminVideoService:
    return AdminVideoService(AdminVideoRepository(db))


@router.get("", response_model=AdminVideoListResponse)
async def list_admin_videos(
    status: str | None = None,
    active: bool | None = None,
    level: str | None = None,
    language: str | None = None,
    curated: bool | None = None,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _current_admin: User = Depends(get_admin_user),
    service: AdminVideoService = Depends(get_admin_video_service),
):
    return await service.list_videos(
        status=status,
        active=active,
        level=level,
        language=language,
        curated=curated,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.patch("/{video_id}", response_model=AdminVideoResponse)
async def patch_admin_video(
    video_id: str,
    body: AdminPatchVideoRequest,
    _current_admin: User = Depends(get_admin_user),
    service: AdminVideoService = Depends(get_admin_video_service),
):
    return await service.patch_video(video_id, body)


@router.delete("/{video_id}", status_code=204)
async def delete_admin_video(
    video_id: str,
    _current_admin: User = Depends(get_admin_user),
    service: AdminVideoService = Depends(get_admin_video_service),
):
    await service.delete_video(video_id)
    return Response(status_code=204)


@router.post("/{video_id}/retry-transcription", response_model=AdminRetryTranscriptionResponse)
async def retry_admin_transcription(
    video_id: str,
    max_segment_duration: float = Query(10.0, ge=3.0, le=30.0),
    _current_admin: User = Depends(get_admin_user),
    service: AdminVideoService = Depends(get_admin_video_service),
):
    return await service.retry_transcription(video_id, max_segment_duration)


@router.post(
    "/{video_id}/recalculate-difficulty", response_model=AdminDifficultyRecalculationResponse
)
async def recalculate_admin_video_difficulty(
    video_id: str,
    _current_admin: User = Depends(get_admin_user),
    service: AdminVideoService = Depends(get_admin_video_service),
):
    return await service.recalculate_difficulty(video_id)