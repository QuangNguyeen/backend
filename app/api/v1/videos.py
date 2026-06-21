import logging

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_optional_current_user
from app.database import get_db
from app.models.user import User
from app.repositories.video_repository import VideoRepository
from app.schemas.video import (
    ImportPartialResponse,
    ImportVideoRequest,
    ImportVideoResponse,
    LevelAnalysisResponse,
    PublishRequestCreate,
    TranscriptBulkUpdateRequest,
    TranscriptBulkUpdateResponse,
    TranscriptFeedbackCreate,
    TranscriptFeedbackResponse,
    TranscriptLanguageResponse,
    TranscriptResponse,
    VideoEditStatusResponse,
    VideoRecommendationsResponse,
    VideoResponse,
)
from app.services.video_service import VideoService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/videos", tags=["Videos"])


def get_video_service(db: AsyncSession = Depends(get_db)) -> VideoService:
    return VideoService(VideoRepository(db))


@router.get("")
async def list_videos(
    language: str | None = None,
    level: str | None = None,
    curated: bool | None = None,
    topic_tag: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    service: VideoService = Depends(get_video_service),
):
    return await service.list_videos(language, level, curated, topic_tag, page, page_size)


@router.post(
    "/import",
    response_model=ImportVideoResponse,
    status_code=201,
    responses={206: {"model": ImportPartialResponse}},
)
async def import_video(
    body: ImportVideoRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    service: VideoService = Depends(get_video_service),
):
    """Import a YouTube video and extract its transcript for dictation practice."""
    result = await service.import_video(current_user, body)
    if isinstance(result, ImportPartialResponse):
        return JSONResponse(status_code=206, content=result.model_dump())
    if result.already_exists or result.already_in_my_practice:
        response.status_code = 200
    return result


# Static path routes MUST come before dynamic {video_id} routes
@router.get("/my-practice")
async def list_my_practice_videos(
    publish_status: str | None = None,
    language: str | None = None,
    level: str | None = None,
    transcription_status: str | None = None,
    topic_tag: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.list_my_practice(
        current_user,
        publish_status,
        language,
        level,
        transcription_status,
        topic_tag,
        page,
        page_size,
    )


@router.get("/recommendations", response_model=VideoRecommendationsResponse)
async def get_video_recommendations(
    limit: int = Query(6, ge=1, le=12),
    current_user: User = Depends(get_current_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.get_recommendations(current_user, limit)


@router.delete("/my-practice/{video_id}", status_code=204)
async def remove_from_my_practice(
    video_id: str,
    current_user: User = Depends(get_current_user),
    service: VideoService = Depends(get_video_service),
):
    await service.remove_from_my_practice(current_user, video_id)
    return Response(status_code=204)


@router.get("/transcript-languages/{video_id}", response_model=list[TranscriptLanguageResponse])
async def get_transcript_languages(
    video_id: str,
    service: VideoService = Depends(get_video_service),
):
    """List available transcript languages for a YouTube video."""
    return service.list_transcript_languages(video_id)


@router.get("/{video_id}/transcription-status")
async def get_transcription_status(
    video_id: str,
    current_user: User | None = Depends(get_optional_current_user),
    service: VideoService = Depends(get_video_service),
):
    """Poll transcription status for a video being processed by Celery."""
    return await service.get_transcription_status(current_user, video_id)


@router.get("/{video_id}", response_model=VideoResponse)
async def get_video(
    video_id: str,
    current_user: User | None = Depends(get_optional_current_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.get_video(current_user, video_id)


@router.get("/{video_id}/transcripts", response_model=list[TranscriptResponse])
async def get_transcripts(
    video_id: str,
    current_user: User | None = Depends(get_optional_current_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.get_transcripts(current_user, video_id)


@router.post("/{video_id}/publish-request")
async def request_publish(
    video_id: str,
    body: PublishRequestCreate,
    current_user: User = Depends(get_current_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.request_publish(current_user, video_id, body)


@router.post("/{video_id}/transcript-feedback", response_model=TranscriptFeedbackResponse)
async def create_transcript_feedback(
    video_id: str,
    body: TranscriptFeedbackCreate,
    current_user: User = Depends(get_current_user),
    service: VideoService = Depends(get_video_service),
):
    return await service.create_transcript_feedback(current_user, video_id, body)


@router.put("/{video_id}/transcripts", response_model=TranscriptBulkUpdateResponse)
async def bulk_update_transcripts(
    video_id: str,
    body: TranscriptBulkUpdateRequest,
    current_user: User = Depends(get_current_user),
    service: VideoService = Depends(get_video_service),
):
    """Bulk-update transcript text for a video (owner only)."""
    return await service.bulk_update_transcripts(current_user, video_id, body)


@router.get("/{video_id}/edit-status", response_model=VideoEditStatusResponse)
async def get_video_edit_status(
    video_id: str,
    current_user: User = Depends(get_current_user),
    service: VideoService = Depends(get_video_service),
):
    """Flags whether the current user has an in-progress DictationAttempt on this video."""
    return await service.get_video_edit_status(current_user, video_id)


@router.post("/{video_id}/analyze-level", response_model=LevelAnalysisResponse)
async def analyze_video_level(
    video_id: str,
    current_user: User = Depends(get_current_user),
    service: VideoService = Depends(get_video_service),
):
    """Analyze and persist the CEFR difficulty level of an existing video."""
    return await service.analyze_video_level(current_user, video_id)


@router.delete("/{video_id}", status_code=204)
async def delete_video(
    video_id: str,
    current_user: User = Depends(get_current_user),
    service: VideoService = Depends(get_video_service),
):
    """Delete a video and all its related data (transcripts, dictation attempts)."""
    await service.delete_video(current_user, video_id)
    return Response(status_code=204)


@router.put("/{video_id}/refresh", response_model=VideoResponse)
async def refresh_transcript(
    video_id: str,
    max_segment_duration: float = 10.0,
    current_user: User = Depends(get_current_user),
    service: VideoService = Depends(get_video_service),
):
    """Re-fetch metadata and transcript from YouTube."""
    return await service.refresh_transcript(current_user, video_id, max_segment_duration)
