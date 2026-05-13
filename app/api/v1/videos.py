import logging

from fastapi import APIRouter, Depends, Response
from sqlalchemy import delete, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.database import get_db
from app.models.dictation import DictationAttempt, DictationSentence
from app.models.user import User
from app.models.video import Transcript, Video
from app.schemas.video import (
    ImportPartialResponse,
    ImportVideoRequest,
    LevelAnalysisResponse,
    TranscriptBulkUpdateRequest,
    TranscriptBulkUpdateResponse,
    TranscriptResponse,
    TranscriptLanguageResponse,
    VideoEditStatusResponse,
    VideoResponse,
)
from youtube_transcript_api._errors import TranscriptsDisabled, VideoUnavailable

from app.services import youtube_service
from app.services.level_service import analyze_level, estimate_cefr_with_speech_rate
from app.tasks.transcription import run_stt_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/videos", tags=["Videos"])


def _redistribute_punctuated_text(
    punctuated_text: str,
    original_segments: list[youtube_service.TranscriptSegment],
) -> list[youtube_service.TranscriptSegment]:
    """Map Gemini-punctuated text back onto original segment timestamps.

    Strategy: Instead of rigid per-segment word mapping, we use a
    proportional approach. We calculate the overall word-position ratio
    and map each punctuated word back to the closest original segment's
    time range. This tolerates word-count drift from Gemini without
    losing segments or creating time gaps.
    """
    if not original_segments or not punctuated_text.strip():
        return original_segments

    punct_words = punctuated_text.split()
    total_punct_words = len(punct_words)
    if total_punct_words == 0:
        return original_segments

    word_timestamps: list[tuple[float, float]] = []
    for seg in original_segments:
        seg_wc = len(seg.text.split())
        if seg_wc == 0:
            continue
        word_duration = seg.duration / seg_wc
        for w_idx in range(seg_wc):
            w_start = seg.start + w_idx * word_duration
            w_end = w_start + word_duration
            word_timestamps.append((w_start, w_end))

    total_orig_words = len(word_timestamps)
    if total_orig_words == 0:
        return original_segments

    result_segments: list[youtube_service.TranscriptSegment] = []
    current_words: list[str] = []
    current_start: float | None = None
    current_end: float = 0.0

    for p_idx, word in enumerate(punct_words):
        orig_idx = int(p_idx * total_orig_words / total_punct_words)
        orig_idx = min(orig_idx, total_orig_words - 1)

        w_start, w_end = word_timestamps[orig_idx]

        if current_start is None:
            current_start = w_start

        current_words.append(word)
        current_end = w_end

        stripped = word.rstrip()
        if stripped and stripped[-1] in ".?!":
            result_segments.append(youtube_service.TranscriptSegment(
                text=" ".join(current_words),
                start=current_start,
                duration=current_end - current_start,
            ))
            current_words = []
            current_start = None

    if current_words and current_start is not None:
        result_segments.append(youtube_service.TranscriptSegment(
            text=" ".join(current_words),
            start=current_start,
            duration=current_end - current_start,
        ))

    if not result_segments:
        return original_segments

    logger.warning(
        "Redistributed punctuated text: %d orig segments → %d sentences",
        len(original_segments), len(result_segments),
    )
    return result_segments


@router.get("", response_model=list[VideoResponse])
async def list_videos(
    language: str | None = None,
    level: str | None = None,
    curated: bool | None = None,
    db: AsyncSession = Depends(get_db),
):
    # Subquery for play_count and best_score per video
    stats_sub = (
        select(
            DictationAttempt.video_id,
            func.count().filter(DictationAttempt.status == "completed").label("play_count"),
            func.max(DictationAttempt.score).label("best_score"),
        )
        .group_by(DictationAttempt.video_id)
        .subquery()
    )

    query = (
        select(
            Video,
            func.coalesce(stats_sub.c.play_count, 0).label("play_count"),
            stats_sub.c.best_score,
        )
        .outerjoin(stats_sub, Video.id == stats_sub.c.video_id)
        .where(Video.is_active == True)  # noqa: E712
    )
    if language:
        query = query.where(Video.language == language)
    if level:
        query = query.where(Video.level == level)
    if curated is not None:
        query = query.where(Video.is_curated == curated)

    result = await db.execute(query.order_by(Video.created_at.desc()))
    videos = []
    for row in result.all():
        video = row[0]
        resp = VideoResponse.model_validate(video)
        resp.play_count = row[1] or 0
        resp.best_score = round(row[2] * 100, 1) if row[2] is not None else None
        videos.append(resp)
    return videos


@router.post("/import", response_model=VideoResponse, status_code=201,
              responses={206: {"model": ImportPartialResponse}})
async def import_video(
    body: ImportVideoRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import a YouTube video and extract its transcript for dictation practice."""
    from fastapi.responses import JSONResponse

    # Extract video ID from URL
    video_id = youtube_service.extract_video_id(body.youtube_url)

    # Check if video already exists
    existing = await db.execute(
        select(Video).where(Video.youtube_id == video_id)
    )
    if existing.scalar_one_or_none():
        raise ConflictError(f"Video {video_id} already imported")

    # ── Phase 1: External API calls (no DB writes) ─────────────────────────

    # 1. Metadata (oEmbed → yt-dlp → defaults)
    metadata = {"title": "", "channel": "", "duration": 0, "thumbnail_url": ""}
    if not body.title or not body.channel:
        try:
            metadata = youtube_service.get_video_metadata(video_id)
        except Exception as e:
            logger.error("Metadata fetch failed for %s: %s", video_id, e, exc_info=True)

    resolved_title = body.title or metadata.get("title") or f"YouTube Video {video_id}"
    resolved_thumbnail = metadata.get("thumbnail_url") or f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"

    # 2. Transcript — Layer 1: youtube-transcript-api
    segments = None
    transcript_error = None

    try:
        segments = youtube_service.get_transcript(video_id, languages=body.languages)
        logger.info("Transcript for %s fetched via youtube-transcript-api", video_id)
    except TranscriptsDisabled as e:
        logger.error("Layer 1 failed — TranscriptsDisabled for %s: %s", video_id, e)
        transcript_error = e
    except VideoUnavailable as e:
        logger.error("Layer 1 failed — VideoUnavailable for %s: %s", video_id, e)
        raise NotFoundError(f"Video '{video_id}' is unavailable or has been removed.")
    except Exception as e:
        logger.error("Layer 1 failed — %s for %s: %s", type(e).__name__, video_id, e, exc_info=True)
        transcript_error = e

    # 2b. Transcript — Layer 2: yt-dlp --write-auto-subs
    if segments is None:
        logger.info("Attempting yt-dlp subtitle fallback for %s", video_id)
        try:
            segments = youtube_service.get_transcript_ytdlp(video_id, languages=body.languages)
            transcript_error = None
        except Exception as e:
            logger.error("Layer 2 failed — yt-dlp subtitle fallback for %s: %s", video_id, e, exc_info=True)
            transcript_error = transcript_error or e

    # 2c. Transcript — Layer 3: If no subs found at all, dispatch STT via Celery
    dispatch_stt_layer3 = False
    if segments is None:
        video_duration_for_stt = metadata.get("duration", 0)
        if video_duration_for_stt <= 300:
            dispatch_stt_layer3 = True
            logger.info(
                "No subtitles found for %s — will dispatch Celery STT task (duration=%ds)",
                video_id, video_duration_for_stt,
            )
            segments = []
            transcript_error = None
        else:
            transcript_error = transcript_error or Exception("No subtitles available and video too long for STT")

    # 2d. All transcript layers failed → return partial response (no DB rollback)
    if segments is None:
        return JSONResponse(
            status_code=206,
            content=ImportPartialResponse(
                video_id=video_id,
                title=resolved_title,
                channel=body.channel or metadata.get("channel", ""),
                thumbnail_url=resolved_thumbnail,
                duration=metadata.get("duration", 0),
                message=(
                    f"Metadata fetched successfully, but transcript extraction is currently blocked. "
                    f"Reason: {type(transcript_error).__name__}: {transcript_error}. "
                    f"Please try again later."
                ),
            ).model_dump(),
        )

    # 3. Merge & analyze (pure computation, no network)
    merged_segments = youtube_service.merge_segments_smart(
        segments, max_duration=body.max_segment_duration
    ) if segments else []

    video_duration = metadata.get("duration") or (int(merged_segments[-1].end) if merged_segments else 0)
    if body.level:
        detected_level = body.level
    elif merged_segments:
        full_text = youtube_service.get_full_text(segments)
        if body.language == "en" and video_duration > 0:
            detected_level = estimate_cefr_with_speech_rate(
                full_text, duration_seconds=video_duration, language=body.language,
            )
        else:
            detected_level = analyze_level(full_text, language=body.language)
        logger.info("Auto-detected level for video %s: %s", video_id, detected_level)
    else:
        detected_level = None

    # ── Phase 2: Database writes (all external data ready) ────────────────

    # Detect auto-generated subs on RAW segments (before merge).
    # Auto-gen subs chop mid-sentence — raw segments rarely end with .?!
    # Manually-uploaded subs have proper sentence boundaries.
    if segments:
        ends_with_punct = sum(
            1 for seg in segments
            if seg.text.rstrip().endswith((".", "?", "!"))
        )
        ratio = ends_with_punct / len(segments)
        is_auto_generated = ratio < 0.50
        logger.warning(
            "Import auto-gen detection for %s: %d/%d raw segments end with punct (%.0f%%) → is_auto=%s",
            video_id, ends_with_punct, len(segments), ratio * 100, is_auto_generated,
        )
    else:
        is_auto_generated = False

    MAX_AUTO_GEN_DURATION = 300
    if is_auto_generated and video_duration > MAX_AUTO_GEN_DURATION:
        raise BadRequestError(
            f"Videos with auto-generated subtitles are limited to "
            f"{MAX_AUTO_GEN_DURATION // 60} minutes. "
            f"This video is {video_duration // 60}m {video_duration % 60}s. "
            f"Please choose a shorter video or one with manually uploaded subtitles."
        )

    # Determine whether to dispatch async STT upgrade via Celery
    dispatch_stt = dispatch_stt_layer3 or (is_auto_generated and video_duration <= MAX_AUTO_GEN_DURATION)

    video = Video(
        youtube_id=video_id,
        title=resolved_title,
        channel=body.channel or metadata.get("channel", ""),
        duration=video_duration,
        language=body.language,
        level=detected_level,
        is_curated=False,
        is_active=True,
        is_auto_generated=is_auto_generated,
        transcription_status="pending" if dispatch_stt else "ready",
        thumbnail_url=resolved_thumbnail,
        created_by=current_user.id,
    )
    db.add(video)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise ConflictError(f"Video {video_id} already imported")

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

    if dispatch_stt:
        task = run_stt_pipeline.delay(
            video_db_id=video.id,
            youtube_id=video_id,
            language=body.language,
            video_duration=video_duration,
            max_segment_duration=body.max_segment_duration,
        )
        logger.info(
            "Dispatched STT Celery task %s for video %s (%s)",
            task.id, video.id, video_id,
        )

    return video


# Static path routes MUST come before dynamic {video_id} routes
@router.get("/transcript-languages/{video_id}", response_model=list[TranscriptLanguageResponse])
async def get_transcript_languages(video_id: str):
    """List available transcript languages for a YouTube video."""
    return youtube_service.list_available_transcripts(video_id)


@router.get("/{video_id}/transcription-status")
async def get_transcription_status(video_id: str, db: AsyncSession = Depends(get_db)):
    """Poll transcription status for a video being processed by Celery."""
    result = await db.execute(
        select(Video.transcription_status, Video.transcription_error)
        .where(Video.id == video_id)
    )
    row = result.one_or_none()
    if not row:
        raise NotFoundError("Video not found")
    return {"status": row[0], "error": row[1]}


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


@router.put("/{video_id}/transcripts", response_model=TranscriptBulkUpdateResponse)
async def bulk_update_transcripts(
    video_id: str,
    body: TranscriptBulkUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-update transcript text for a video (owner only).

    All updates commit as one transaction. Any unknown transcript_id or row
    belonging to a different video aborts the whole request with 404.
    """
    video = (await db.execute(select(Video).where(Video.id == video_id))).scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")
    if video.created_by != current_user.id and not current_user.is_admin:
        raise ForbiddenError("You can only edit subtitles of videos you created")

    ids = [item.transcript_id for item in body.items]
    rows = (await db.execute(
        select(Transcript).where(Transcript.id.in_(ids))
    )).scalars().all()
    rows_by_id = {r.id: r for r in rows}

    deleted = 0
    updated = 0
    for item in body.items:
        row = rows_by_id.get(item.transcript_id)
        if not row or row.video_id != video_id:
            raise NotFoundError(f"Transcript {item.transcript_id} not found for this video")
        if item.is_deleted:
            await db.delete(row)
            deleted += 1
        else:
            if item.text:
                row.text = item.text
                updated += 1

    if deleted > 0:
        remaining = (await db.execute(
            select(Transcript)
            .where(Transcript.video_id == video_id)
            .order_by(Transcript.index)
        )).scalars().all()
        for idx, t in enumerate(remaining):
            t.index = idx

    await db.commit()
    return TranscriptBulkUpdateResponse(updated=updated + deleted)


@router.get("/{video_id}/edit-status", response_model=VideoEditStatusResponse)
async def get_video_edit_status(
    video_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Flags whether the current user has an in-progress DictationAttempt on this
    video, so the UI can warn before editing subtitles may alter past scoring."""
    video = (await db.execute(select(Video).where(Video.id == video_id))).scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")

    exists_q = select(DictationAttempt.id).where(
        DictationAttempt.user_id == current_user.id,
        DictationAttempt.video_id == video_id,
        DictationAttempt.status == "in_progress",
    ).limit(1)
    in_progress = (await db.execute(exists_q)).scalar_one_or_none() is not None
    return VideoEditStatusResponse(has_in_progress_attempt=in_progress)


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

    if video.created_by != current_user.id and not current_user.is_admin:
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
    """Re-fetch metadata and transcript from YouTube.

    Updates title, channel, thumbnail, duration, transcripts, and CEFR level.
    """
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise NotFoundError("Video not found")

    if video.created_by != current_user.id and not current_user.is_admin:
        raise ForbiddenError("You can only refresh videos you created")

    # Re-fetch metadata (never raises)
    metadata = youtube_service.get_video_metadata(video.youtube_id)
    if metadata.get("title"):
        video.title = metadata["title"]
    if metadata.get("channel"):
        video.channel = metadata["channel"]
    if metadata.get("thumbnail_url"):
        video.thumbnail_url = metadata["thumbnail_url"]
    if metadata.get("duration"):
        video.duration = metadata["duration"]

    # Delete old transcripts
    await db.execute(
        delete(Transcript).where(Transcript.video_id == video_id)
    )

    # Re-fetch transcript from YouTube
    try:
        segments = youtube_service.get_transcript(video.youtube_id)
    except TranscriptsDisabled:
        raise BadRequestError(f"Transcripts are disabled for video: {video.youtube_id}")
    except VideoUnavailable:
        raise NotFoundError(f"Video '{video.youtube_id}' is unavailable or has been removed.")

    # Merge with smart algorithm
    merged_segments = youtube_service.merge_segments_smart(
        segments, max_duration=max_segment_duration
    )

    # Detect auto-generated subs on RAW segments (before merge).
    if segments:
        ends_with_punct = sum(
            1 for seg in segments
            if seg.text.rstrip().endswith((".", "?", "!"))
        )
        ratio = ends_with_punct / len(segments)
        refresh_is_auto = ratio < 0.50
        logger.warning(
            "Refresh auto-gen detection for %s: %d/%d raw segments end with punct (%.0f%%) → is_auto=%s",
            video_id, ends_with_punct, len(segments), ratio * 100, refresh_is_auto,
        )
    else:
        refresh_is_auto = False
    video.is_auto_generated = refresh_is_auto

    MAX_AUTO_GEN_DURATION = 300
    if refresh_is_auto and video.duration and video.duration > MAX_AUTO_GEN_DURATION:
        raise BadRequestError(
            f"Videos with auto-generated subtitles are limited to "
            f"{MAX_AUTO_GEN_DURATION // 60} minutes. "
            f"This video is {video.duration // 60}m {video.duration % 60}s. "
            f"Please choose a shorter video or one with manually uploaded subtitles."
        )

    dispatch_stt_refresh = refresh_is_auto and video.duration and video.duration <= MAX_AUTO_GEN_DURATION

    if dispatch_stt_refresh:
        video.transcription_status = "pending"
    else:
        video.transcription_status = "ready"

    # Create new transcript records (auto-gen subs as placeholder until STT completes)
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

    # Update duration from transcript if metadata didn't provide it
    if merged_segments and not video.duration:
        video.duration = int(merged_segments[-1].end)

    # Re-analyze CEFR level (only if not dispatching STT — it will recalculate)
    if merged_segments and not dispatch_stt_refresh:
        full_text = youtube_service.get_full_text(segments)
        if video.language == "en" and video.duration > 0:
            video.level = estimate_cefr_with_speech_rate(
                full_text, duration_seconds=video.duration, language=video.language,
            )
        else:
            video.level = analyze_level(full_text, language=video.language)
        logger.info("Re-analyzed level for video %s: %s", video_id, video.level)

    await db.commit()
    await db.refresh(video)

    if dispatch_stt_refresh:
        task = run_stt_pipeline.delay(
            video_db_id=video.id,
            youtube_id=video.youtube_id,
            language=video.language,
            video_duration=video.duration,
            max_segment_duration=max_segment_duration,
        )
        logger.info("Dispatched STT Celery task %s on refresh for %s", task.id, video_id)

    return video