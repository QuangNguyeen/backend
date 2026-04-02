import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.database import get_db
from app.models.dictation import DictationAttempt, DictationSentence
from app.models.user import User
from app.models.video import Transcript, Video
from app.schemas.video import (
    ImportVideoRequest,
    LevelAnalysisResponse,
    TranscriptResponse,
    TranscriptLanguageResponse,
    VideoResponse,
)
from app.services import youtube_service
from app.services.level_service import analyze_level

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/videos", tags=["Videos"])


@router.get("", response_model=list[VideoResponse])
async def list_videos(
    language: str | None = None,
    level: str | None = None,
    curated: bool | None = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Video).where(Video.is_active == True)  # noqa: E712
    if language:
        query = query.where(Video.language == language)
    if level:
        query = query.where(Video.level == level)
    if curated is not None:
        query = query.where(Video.is_curated == curated)

    result = await db.execute(query.order_by(Video.created_at.desc()))
    return result.scalars().all()


@router.post("/import", response_model=VideoResponse, status_code=201)
async def import_video(
    body: ImportVideoRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import a YouTube video and extract its transcript for dictation practice."""
    # Extract video ID from URL
    video_id = youtube_service.extract_video_id(body.youtube_url)

    # Check if video already exists
    existing = await db.execute(
        select(Video).where(Video.youtube_id == video_id)
    )
    if existing.scalar_one_or_none():
        raise ConflictError(f"Video {video_id} already imported")

    # Auto-fetch metadata from YouTube if not provided manually
    metadata = {"title": "", "channel": "", "duration": 0, "thumbnail_url": ""}
    if not body.title or not body.channel:
        try:
            metadata = youtube_service.get_video_metadata(video_id)
        except (NotFoundError, BadRequestError):
            logger.warning("Failed to fetch metadata for video %s, using defaults", video_id)

    # Get transcript
    segments = youtube_service.get_transcript(video_id, languages=body.languages)

    # Merge segments using smart sentence-boundary-aware algorithm
    merged_segments = youtube_service.merge_segments_smart(
        segments, max_duration=body.max_segment_duration
    )

    # Auto-detect CEFR level from transcript text when not explicitly provided
    if body.level:
        detected_level = body.level
    else:
        full_text = youtube_service.get_full_text(segments)
        detected_level = analyze_level(full_text, language=body.language)
        logger.info("Auto-detected level for video %s: %s", video_id, detected_level)

    # Create video record
    video = Video(
        youtube_id=video_id,
        title=body.title or metadata.get("title") or f"YouTube Video {video_id}",
        channel=body.channel or metadata.get("channel", ""),
        duration=metadata.get("duration") or (int(merged_segments[-1].end) if merged_segments else 0),
        language=body.language,
        level=detected_level,
        is_curated=False,
        is_active=True,
        thumbnail_url=metadata.get("thumbnail_url") or f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
        created_by=current_user.id,
    )
    db.add(video)
    await db.flush()  # Get video.id

    # Create transcript records
    for idx, segment in enumerate(merged_segments):
        transcript = Transcript(
            video_id=video.id,
            language=body.language,
            index=idx,
            text=segment.text,
            start_time=segment.start,
            end_time=segment.end,
        )
        db.add(transcript)

    await db.commit()
    await db.refresh(video)

    return video


# Static path routes MUST come before dynamic {video_id} routes
@router.get("/transcript-languages/{video_id}", response_model=list[TranscriptLanguageResponse])
async def get_transcript_languages(video_id: str):
    """List available transcript languages for a YouTube video."""
    return youtube_service.list_available_transcripts(video_id)


@router.get("/{video_id}", response_model=VideoResponse)
async def get_video(video_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")
    return video


@router.get("/{video_id}/transcripts", response_model=list[TranscriptResponse])
async def get_transcripts(video_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Transcript).where(Transcript.video_id == video_id).order_by(Transcript.index)
    )
    return result.scalars().all()


@router.post("/{video_id}/analyze-level", response_model=LevelAnalysisResponse)
async def analyze_video_level(
    video_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Analyze and persist the CEFR difficulty level of an existing video.

    Uses spaCy (syntactic analysis) and wordfreq (vocabulary frequency) on
    the stored transcript text, then saves the result to the video record.
    Returns the full feature breakdown for transparency.
    """
    from app.services.level_service import analyze_level_detailed

    # Fetch video
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")

    # Fetch transcript
    tr_result = await db.execute(
        select(Transcript).where(Transcript.video_id == video_id).order_by(Transcript.index)
    )
    transcripts = tr_result.scalars().all()
    if not transcripts:
        raise NotFoundError("No transcript found for this video")

    full_text = " ".join(t.text for t in transcripts)
    analysis = analyze_level_detailed(full_text, language=video.language)

    # Persist detected level
    video.level = analysis["level"]
    await db.commit()

    return analysis


@router.delete("/{video_id}", status_code=204)
async def delete_video(
    video_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a video and all its related data (transcripts, dictation attempts)."""
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")

    if video.created_by != current_user.id:
        raise ForbiddenError("You can only delete videos you created")

    # Delete related dictation attempts and their sentences
    attempts = await db.execute(
        select(DictationAttempt).where(DictationAttempt.video_id == video_id)
    )
    for attempt in attempts.scalars().all():
        await db.execute(
            delete(DictationSentence).where(DictationSentence.attempt_id == attempt.id)
        )
    await db.execute(
        delete(DictationAttempt).where(DictationAttempt.video_id == video_id)
    )

    # Delete video (transcripts cascade automatically via relationship)
    await db.delete(video)
    await db.commit()

    return Response(status_code=204)


@router.put("/{video_id}/refresh", response_model=VideoResponse)
async def refresh_transcript(
    video_id: str,
    max_segment_duration: float = 10.0,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-fetch transcript from YouTube and rebuild transcript segments.

    This keeps the video record but replaces all transcripts with
    freshly fetched and merged transcript data.
    """
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")

    if video.created_by != current_user.id:
        raise ForbiddenError("You can only refresh videos you created")

    # Delete old transcripts
    await db.execute(
        delete(Transcript).where(Transcript.video_id == video_id)
    )

    # Re-fetch transcript from YouTube
    segments = youtube_service.get_transcript(video.youtube_id)

    # Merge with smart algorithm
    merged_segments = youtube_service.merge_segments_smart(
        segments, max_duration=max_segment_duration
    )

    # Create new transcript records
    for idx, segment in enumerate(merged_segments):
        transcript = Transcript(
            video_id=video.id,
            language=video.language,
            index=idx,
            text=segment.text,
            start_time=segment.start,
            end_time=segment.end,
        )
        db.add(transcript)

    # Update video duration and re-analyze level from fresh transcript
    if merged_segments:
        video.duration = int(merged_segments[-1].end)
        full_text = youtube_service.get_full_text(segments)
        video.level = analyze_level(full_text, language=video.language)
        logger.info("Re-analyzed level for video %s: %s", video_id, video.level)

    await db.commit()
    await db.refresh(video)

    return video