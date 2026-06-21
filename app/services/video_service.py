"""Business logic for video import, transcripts, difficulty, and lifecycle."""

import asyncio
import logging
import math
import re
from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from youtube_transcript_api._errors import TranscriptsDisabled, VideoUnavailable

from app.core.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.events import publish_user_recommendation_event, publish_video_event
from app.models.user import User
from app.models.video import TopicTag, Transcript, Video
from app.repositories.video_repository import (
    PUBLISH_STATUS_PENDING,
    PUBLISH_STATUS_PRIVATE,
    PUBLISH_STATUS_PUBLISHED,
    PUBLISH_STATUS_REJECTED,
    REQUEST_STATUS_APPROVED,
    REQUEST_STATUS_REJECTED,
    VideoRepository,
)
from app.schemas.video import (
    AdminFeedbackPatch,
    AdminPublishRequestAction,
    AdminTopicTagCreate,
    AdminTopicTagPatch,
    AdminTranscriptFeedbackItemResponse,
    AdminTranscriptFeedbackListResponse,
    ImporterSummaryResponse,
    ImportPartialResponse,
    ImportVideoRequest,
    ImportVideoResponse,
    PublishRequestCreate,
    PublishRequestResponse,
    TopicTagResponse,
    TranscriptBulkUpdateRequest,
    TranscriptBulkUpdateResponse,
    TranscriptFeedbackCreate,
    TranscriptFeedbackResponse,
    VideoEditStatusResponse,
    VideoRecommendationItemResponse,
    VideoRecommendationsResponse,
    VideoResponse,
    VideoTopicTagsUpdate,
)
from app.services import youtube_service
from app.services.level_service import calculate_audio_difficulty, difficulty_update_values
from app.services.llm_service import punctuate_transcript
from app.services.stt_audio_service import STT_MAX_DURATION_SECONDS
from app.tasks.transcription import run_stt_pipeline

logger = logging.getLogger(__name__)

STT_PROMPT_CONTEXT_MAX_CHARS = 4000
TRANSCRIPT_OVERLAP_TOLERANCE_SECONDS = 0.05
VALID_PUBLISH_STATUSES = {
    PUBLISH_STATUS_PRIVATE,
    PUBLISH_STATUS_PENDING,
    PUBLISH_STATUS_PUBLISHED,
    PUBLISH_STATUS_REJECTED,
}
VALID_FEEDBACK_STATUSES = {"pending", "reviewed", "resolved", "rejected"}
CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
CEFR_INDEX = {level: index for index, level in enumerate(CEFR_LEVELS)}


def _recency_weight(rank: int) -> float:
    return 1 / (1 + rank / 5)


def _normalize_channel(channel: str | None) -> str:
    return (channel or "").strip().casefold()


def _round_level(value: float) -> int:
    return max(0, min(len(CEFR_LEVELS) - 1, math.floor(value + 0.5)))


def _level_fit(candidate_level: str | None, target_index: int | None) -> float:
    if target_index is None:
        return 0.0
    candidate_index = CEFR_INDEX.get((candidate_level or "").upper())
    if candidate_index is None:
        return 0.1
    distance = abs(candidate_index - target_index)
    return {0: 1.0, 1: 0.6, 2: 0.2}.get(distance, 0.0)


def _freshness_score(video: Video, now: datetime) -> float:
    timestamp = video.published_at or video.created_at
    if not timestamp:
        return 0.0
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    age_days = max(0, (now - timestamp).days)
    if age_days <= 30:
        return 1.0
    if age_days <= 90:
        return 0.67
    if age_days <= 180:
        return 0.33
    return 0.0


def _published_timestamp(video: Video) -> float:
    timestamp = video.published_at or video.created_at
    if not timestamp:
        return 0.0
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.timestamp()


def _caption_prompt_text(
    segments: list[Transcript] | list[youtube_service.TranscriptSegment],
) -> str | None:
    text = youtube_service.get_full_text(segments).strip()
    if not text:
        return None
    return text[:STT_PROMPT_CONTEXT_MAX_CHARS]


def _stt_too_long_response(
    *,
    video_id: str,
    title: str,
    channel: str,
    thumbnail_url: str,
    duration: int,
) -> ImportPartialResponse:
    return ImportPartialResponse(
        video_id=video_id,
        title=title,
        channel=channel,
        thumbnail_url=thumbnail_url,
        duration=duration,
        message=(
            "This video has no manual subtitles and must be transcribed with AI, "
            f"but AI transcription is limited to {STT_MAX_DURATION_SECONDS // 60} minutes. "
            f"This video is {duration // 60}m {duration % 60}s."
        ),
    )


def _validate_transcript_timeline(entries: list[tuple[str, int, float, float]]) -> None:
    for transcript_id, _index, start_time, end_time in entries:
        if start_time < 0:
            raise BadRequestError(f"Transcript {transcript_id} start_time must be >= 0")
        if end_time <= start_time:
            raise BadRequestError(
                f"Transcript {transcript_id} end_time must be greater than start_time"
            )

    ordered = sorted(entries, key=lambda item: (item[1], item[2], item[3]))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        prev_id, _prev_index, _prev_start, prev_end = previous
        cur_id, _cur_index, cur_start, _cur_end = current
        if prev_end > cur_start + TRANSCRIPT_OVERLAP_TOLERANCE_SECONDS:
            raise BadRequestError(
                "Transcript timestamps overlap: "
                f"{prev_id} ends at {prev_end:.2f}s, "
                f"{cur_id} starts at {cur_start:.2f}s"
            )


def _effective_publish_status(video: Video) -> str:
    return getattr(video, "publish_status", None) or PUBLISH_STATUS_PUBLISHED


def _is_public(video: Video) -> bool:
    return _effective_publish_status(video) == PUBLISH_STATUS_PUBLISHED


def _normalize_slug(slug: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", slug.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized:
        raise BadRequestError("Tag slug is required")
    return normalized


def _now() -> datetime:
    return datetime.now(UTC)


class VideoService:
    def __init__(self, repo: VideoRepository):
        self.repo = repo

    async def _validate_active_tag_ids(self, tag_ids: list[str] | None) -> list[str]:
        unique_ids = list(dict.fromkeys(tag_ids or []))
        if not unique_ids:
            return []
        tags = await self.repo.get_active_topic_tags_by_ids(unique_ids)
        found = {tag.id for tag in tags}
        missing = [tag_id for tag_id in unique_ids if tag_id not in found]
        if missing:
            raise BadRequestError("One or more topic tags are inactive or do not exist")
        return unique_ids

    async def _can_access_video(self, user: User | None, video: Video) -> bool:
        if _is_public(video):
            return True
        if not user:
            return False
        if user.is_admin:
            return True
        return await self.repo.get_user_practice(user.id, video.id) is not None

    async def _require_video_access(self, user: User | None, video_id: str) -> Video:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")
        if not await self._can_access_video(user, video):
            raise NotFoundError("Video not found")
        return video

    async def _serialize_video(
        self,
        video: Video,
        *,
        user: User | None = None,
        play_count: int = 0,
        best_score: float | None = None,
        my_practice_created_at: datetime | None = None,
        public_tags: list[TopicTag] | None = None,
        my_tags: list[TopicTag] | None = None,
    ) -> VideoResponse:
        if getattr(video, "publish_status", None) is None:
            video.publish_status = PUBLISH_STATUS_PUBLISHED
        resp = VideoResponse.model_validate(video)
        resp.publish_status = _effective_publish_status(video)
        resp.play_count = play_count
        resp.best_score = round(best_score * 100, 1) if best_score is not None else None
        resp.my_practice_created_at = my_practice_created_at
        public_tags = (
            public_tags
            if public_tags is not None
            else await self.repo.get_video_public_tags(video.id)
        )
        resp.topic_tags = [TopicTagResponse.model_validate(tag) for tag in public_tags]
        if user:
            my_tags = (
                my_tags
                if my_tags is not None
                else await self.repo.get_user_practice_tags(user.id, video.id)
            )
            resp.my_topic_tags = [TopicTagResponse.model_validate(tag) for tag in my_tags]
        return resp

    async def _publish_video_as_admin(
        self,
        video: Video,
        admin: User,
        *,
        topic_tag_ids: list[str] | None = None,
        review_note: str | None = None,
        resolve_requests: bool = True,
    ) -> None:
        now = _now()
        video.publish_status = PUBLISH_STATUS_PUBLISHED
        video.published_at = video.published_at or now
        video.reviewed_by = admin.id
        video.reviewed_at = now
        video.review_note = review_note
        if topic_tag_ids is not None:
            await self.repo.set_video_public_tags(video.id, topic_tag_ids)
        if resolve_requests:
            await self.repo.resolve_pending_publish_requests(video.id, admin.id, review_note)

    # ── Listing ─────────────────────────────────────────────────────────────

    async def list_videos(
        self,
        language: str | None,
        level: str | None,
        curated: bool | None,
        topic_tag: str | None,
        page: int,
        page_size: int,
    ) -> dict:
        rows, total = await self.repo.list_with_stats(
            language, level, curated, topic_tag, page, page_size
        )
        total_pages = max(1, math.ceil(total / page_size))

        videos = []
        for row in rows:
            video = row[0]
            resp = await self._serialize_video(
                video,
                play_count=row[1] or 0,
                best_score=row[2],
            )
            videos.append(resp)
        return {"items": videos, "total": total, "page": page, "total_pages": total_pages}

    async def list_my_practice(
        self,
        user: User,
        publish_status: str | None,
        language: str | None,
        level: str | None,
        transcription_status: str | None,
        topic_tag: str | None,
        page: int,
        page_size: int,
    ) -> dict:
        if publish_status and publish_status not in VALID_PUBLISH_STATUSES:
            raise BadRequestError("Invalid publish_status")
        rows, total = await self.repo.list_my_practice_with_stats(
            user.id,
            publish_status,
            language,
            level,
            transcription_status,
            topic_tag,
            page,
            page_size,
        )
        total_pages = max(1, math.ceil(total / page_size))
        videos = []
        for video, created_at, play_count, best_score in rows:
            videos.append(
                await self._serialize_video(
                    video,
                    user=user,
                    play_count=play_count or 0,
                    best_score=best_score,
                    my_practice_created_at=created_at,
                )
            )
        return {"items": videos, "total": total, "page": page, "total_pages": total_pages}

    async def get_recommendations(
        self, user: User, limit: int = 6
    ) -> VideoRecommendationsResponse:
        candidate_rows = await self.repo.list_recommendation_candidates(user.id, limit=500)
        if not candidate_rows:
            return VideoRecommendationsResponse(strategy="cold_start", items=[])

        recent_rows = await self.repo.list_recent_completed_attempts(user.id, limit=20)
        practice_videos = await self.repo.list_unattempted_practice_videos(user.id, limit=500)
        practice_tag_rows = await self.repo.list_unattempted_practice_tags(user.id)

        candidate_ids = [row[0].id for row in candidate_rows]
        attempted_video_ids = [video.id for _, video in recent_rows]
        public_tag_rows = await self.repo.list_public_tags_for_videos(
            list(dict.fromkeys(candidate_ids + attempted_video_ids))
        )
        public_tags_by_video: dict[str, list[TopicTag]] = defaultdict(list)
        for video_id, tag in public_tag_rows:
            public_tags_by_video[video_id].append(tag)

        channel_affinity: dict[str, float] = defaultdict(float)
        topic_affinity: dict[str, float] = defaultdict(float)
        topic_names: dict[str, str] = {}
        adjusted_levels: list[tuple[float, float]] = []
        current_levels: list[tuple[float, float]] = []

        for rank, (attempt, video) in enumerate(recent_rows):
            weight = _recency_weight(rank)
            channel_key = _normalize_channel(video.channel)
            if channel_key:
                channel_affinity[channel_key] += weight
            for tag in public_tags_by_video.get(video.id, []):
                topic_affinity[tag.id] += weight
                topic_names[tag.id] = tag.name

            level_index = CEFR_INDEX.get((video.level or "").upper())
            if level_index is not None:
                score = attempt.score
                adjustment = -1 if score is not None and score < 0.60 else 0
                if score is not None and score >= 0.85:
                    adjustment = 1
                adjusted_levels.append(
                    (
                        max(0, min(len(CEFR_LEVELS) - 1, level_index + adjustment)),
                        weight,
                    )
                )
                current_levels.append((level_index, weight))

        for video in practice_videos:
            channel_key = _normalize_channel(video.channel)
            if channel_key:
                channel_affinity[channel_key] += 0.5
        for _video_id, tag in practice_tag_rows:
            topic_affinity[tag.id] += 0.5
            topic_names[tag.id] = tag.name

        max_channel_affinity = max(channel_affinity.values(), default=0.0)
        max_topic_affinity = max(topic_affinity.values(), default=0.0)
        target_level_index = None
        current_level_index = None
        if adjusted_levels:
            total_weight = sum(weight for _, weight in adjusted_levels)
            target_level_index = _round_level(
                sum(level * weight for level, weight in adjusted_levels) / total_weight
            )
        if current_levels:
            total_weight = sum(weight for _, weight in current_levels)
            current_level_index = _round_level(
                sum(level * weight for level, weight in current_levels) / total_weight
            )

        personalized = bool(recent_rows or channel_affinity or topic_affinity)
        strategy = "personalized" if personalized else "cold_start"
        max_popularity = max((int(row[1] or 0) for row in candidate_rows), default=0)
        now = _now()
        ranked: list[dict] = []

        for video, play_count, best_score in candidate_rows:
            tags = public_tags_by_video.get(video.id, [])
            popularity = (
                math.log1p(play_count or 0) / math.log1p(max_popularity)
                if max_popularity > 0
                else 0.0
            )
            channel_key = _normalize_channel(video.channel)
            channel_score = (
                channel_affinity.get(channel_key, 0.0) / max_channel_affinity
                if channel_key and max_channel_affinity
                else 0.0
            )
            matching_tag = max(
                tags,
                key=lambda tag: topic_affinity.get(tag.id, 0.0),
                default=None,
            )
            topic_score = (
                topic_affinity.get(matching_tag.id, 0.0) / max_topic_affinity
                if matching_tag and max_topic_affinity
                else 0.0
            )
            language_score = 1.0 if video.language == user.preferred_language else 0.0
            level_score = _level_fit(video.level, target_level_index)
            curated_score = 1.0 if video.is_curated else 0.0
            freshness = _freshness_score(video, now)

            if personalized:
                contributions = {
                    "topic_match": 0.30 * topic_score,
                    "level": 0.25 * level_score,
                    "same_channel": 0.15 * channel_score,
                    "preferred_language": 0.15 * language_score,
                    "popular": 0.10 * popularity,
                    "curated": 0.05 * curated_score,
                }
            else:
                contributions = {
                    "preferred_language": 0.45 * language_score,
                    "curated": 0.20 * curated_score,
                    "popular": 0.20 * popularity,
                    "new_content": 0.15 * freshness,
                }
            total_score = sum(contributions.values())

            level_reason = "level_match"
            if (
                target_level_index is not None
                and current_level_index is not None
                and target_level_index > current_level_index
                and CEFR_INDEX.get((video.level or "").upper()) == target_level_index
            ):
                level_reason = "level_progression"

            reason_candidates = [
                ("topic_match", contributions.get("topic_match", 0.0)),
                (level_reason, contributions.get("level", 0.0)),
                ("same_channel", contributions.get("same_channel", 0.0)),
                ("preferred_language", contributions.get("preferred_language", 0.0)),
                ("curated", contributions.get("curated", 0.0)),
                ("popular", contributions.get("popular", 0.0)),
                ("new_content", contributions.get("new_content", 0.0)),
            ]
            reason_code, reason_value = max(
                enumerate(reason_candidates),
                key=lambda item: (item[1][1], -item[0]),
            )[1]
            if reason_value <= 0:
                reason_code = "new_content"

            if reason_code == "topic_match" and matching_tag:
                topic_name = topic_names.get(matching_tag.id, matching_tag.name)
                reason_text = f"Matches your {topic_name} practice"
            elif reason_code == "level_progression" and current_level_index is not None:
                reason_text = f"A good next step from {CEFR_LEVELS[current_level_index]}"
            elif reason_code == "level_match" and target_level_index is not None:
                reason_text = f"Matches your current {CEFR_LEVELS[target_level_index]} level"
            elif reason_code == "same_channel":
                reason_text = f"More from {video.channel.strip()}"
            elif reason_code == "preferred_language":
                reason_text = "Matches your preferred language"
            elif reason_code == "curated":
                reason_text = "Selected by the DictaLearn team"
            elif reason_code == "popular":
                reason_text = "Popular with other learners"
            else:
                reason_text = "Recently added"

            ranked.append(
                {
                    "video": video,
                    "play_count": int(play_count or 0),
                    "best_score": best_score,
                    "tags": tags,
                    "score": total_score,
                    "reason_code": reason_code,
                    "reason_text": reason_text,
                    "popularity": int(play_count or 0),
                    "channel_key": channel_key,
                }
            )

        ranked.sort(
            key=lambda item: (
                -item["score"],
                -int(item["video"].is_curated),
                -item["popularity"],
                -_published_timestamp(item["video"]),
                item["video"].id,
            )
        )

        selected = []
        channel_counts: dict[str, int] = defaultdict(int)
        for item in ranked:
            channel_key = item["channel_key"]
            if channel_key and channel_counts[channel_key] >= 2:
                continue
            selected.append(item)
            if channel_key:
                channel_counts[channel_key] += 1
            if len(selected) >= limit:
                break

        response_items = []
        for item in selected:
            response_items.append(
                VideoRecommendationItemResponse(
                    video=await self._serialize_video(
                        item["video"],
                        user=user,
                        play_count=item["play_count"],
                        best_score=item["best_score"],
                        public_tags=item["tags"],
                        my_tags=[],
                    ),
                    reason_code=item["reason_code"],
                    reason_text=item["reason_text"],
                )
            )
        return VideoRecommendationsResponse(strategy=strategy, items=response_items)

    # ── Import ──────────────────────────────────────────────────────────────

    async def import_video(
        self, user: User, body: ImportVideoRequest
    ) -> ImportVideoResponse | ImportPartialResponse:
        topic_tag_ids = await self._validate_active_tag_ids(body.topic_tag_ids)
        video_id = youtube_service.extract_video_id(body.youtube_url)

        existing_video = await self.repo.get_by_youtube_id(video_id)
        if existing_video:
            importers, importers_count = await self.repo.get_similar_importers(
                existing_video.id, exclude_user_id=user.id
            )
            practice = await self.repo.get_user_practice(user.id, existing_video.id)
            already_in_my_practice = practice is not None
            if not practice:
                practice = await self.repo.create_user_practice(user.id, existing_video.id)

            if not already_in_my_practice or "topic_tag_ids" in body.model_fields_set:
                await self.repo.set_user_practice_tags(practice.id, topic_tag_ids)

            published_by_admin = user.is_admin and not _is_public(existing_video)
            if published_by_admin:
                await self._publish_video_as_admin(
                    existing_video,
                    user,
                    topic_tag_ids=topic_tag_ids if topic_tag_ids else None,
                    review_note="Published by admin import",
                )

            await self.repo.commit()
            await self.repo.refresh(existing_video)
            await publish_user_recommendation_event(
                user.id,
                "my_practice_changed",
                existing_video.id,
            )
            if published_by_admin:
                await publish_video_event("video.published", existing_video.id)
            message = (
                "Video is already in your My Practice."
                if already_in_my_practice
                else "Video already exists. Added to your My Practice."
            )
            return ImportVideoResponse(
                video=await self._serialize_video(existing_video, user=user),
                already_exists=True,
                already_in_my_practice=already_in_my_practice,
                message=message,
                similar_importers_count=importers_count,
                similar_importers=[
                    ImporterSummaryResponse(id=importer.id, display_name=importer.display_name)
                    for importer in importers
                ],
            )

        # ── Phase 1: External API calls — run sync I/O in thread pool ─────────
        metadata = {"title": "", "channel": "", "duration": 0, "thumbnail_url": ""}
        if not body.title or not body.channel:
            try:
                metadata = await asyncio.to_thread(youtube_service.get_video_metadata, video_id)
            except Exception as e:
                logger.error("Metadata fetch failed for %s: %s", video_id, e, exc_info=True)

        resolved_title = body.title or metadata.get("title") or f"YouTube Video {video_id}"
        resolved_channel = body.channel or metadata.get("channel", "")
        resolved_thumbnail = (
            metadata.get("thumbnail_url") or f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
        )

        transcript_result = None
        segments = None
        transcript_error = None

        try:
            transcript_result = await asyncio.to_thread(
                youtube_service.get_transcript, video_id, body.languages
            )
            segments = transcript_result.segments
            logger.info(
                "Transcript for %s fetched via youtube-transcript-api (is_generated=%s)",
                video_id,
                transcript_result.is_generated,
            )
        except TranscriptsDisabled as e:
            logger.error("Layer 1 failed — TranscriptsDisabled for %s: %s", video_id, e)
            transcript_error = e
        except VideoUnavailable as e:
            logger.error("Layer 1 failed — VideoUnavailable for %s: %s", video_id, e)
            raise NotFoundError(f"Video '{video_id}' is unavailable or has been removed.")
        except Exception as e:
            logger.error(
                "Layer 1 failed — %s for %s: %s", type(e).__name__, video_id, e, exc_info=True
            )
            transcript_error = e

        if segments is None:
            logger.info("Attempting yt-dlp subtitle fallback for %s", video_id)
            try:
                transcript_result = await asyncio.to_thread(
                    youtube_service.get_transcript_ytdlp, video_id, body.languages
                )
                segments = transcript_result.segments
                transcript_error = None
            except Exception as e:
                logger.error(
                    "Layer 2 failed — yt-dlp subtitle fallback for %s: %s",
                    video_id,
                    e,
                    exc_info=True,
                )
                transcript_error = transcript_error or e

        # Layer 3: If no manual subs are available, use STT as the final transcript.
        dispatch_stt = False
        if segments is None:
            video_duration_for_stt = metadata.get("duration", 0)
            if video_duration_for_stt <= STT_MAX_DURATION_SECONDS:
                dispatch_stt = True
                logger.info(
                    "No subtitles found for %s - will dispatch AssemblyAI STT task (duration=%ds)",
                    video_id,
                    video_duration_for_stt,
                )
                segments = []
                transcript_error = None
            else:
                return _stt_too_long_response(
                    video_id=video_id,
                    title=resolved_title,
                    channel=resolved_channel,
                    thumbnail_url=resolved_thumbnail,
                    duration=video_duration_for_stt,
                )

        # All transcript layers failed → partial response (no DB rollback).
        if segments is None:
            return ImportPartialResponse(
                video_id=video_id,
                title=resolved_title,
                channel=resolved_channel,
                thumbnail_url=resolved_thumbnail,
                duration=metadata.get("duration", 0),
                message=(
                    "Metadata fetched successfully, but transcript extraction "
                    "is currently blocked. "
                    f"Reason: {type(transcript_error).__name__}: {transcript_error}. "
                    "Please try again later."
                ),
            )

        is_auto_generated = transcript_result.is_generated if transcript_result else False
        video_duration = metadata.get("duration") or (int(segments[-1].end) if segments else 0)

        if is_auto_generated:
            if video_duration > STT_MAX_DURATION_SECONDS:
                return _stt_too_long_response(
                    video_id=video_id,
                    title=resolved_title,
                    channel=resolved_channel,
                    thumbnail_url=resolved_thumbnail,
                    duration=video_duration,
                )
            dispatch_stt = True
            logger.info(
                "Auto-generated captions detected for %s - dispatching AssemblyAI STT "
                "for final transcript (duration=%ds)",
                video_id,
                video_duration,
            )

        # Punctuate only transcripts that will be used as final content.
        if segments and not dispatch_stt:
            ends_with_punct = sum(
                1 for seg in segments if seg.text.rstrip().endswith((".", "?", "!"))
            )
            ratio = ends_with_punct / len(segments) if segments else 1.0
            if ratio < 0.30:
                logger.info(
                    "Low punctuation ratio for %s (%.0f%%) — calling Gemini to restore punctuation",
                    video_id,
                    ratio * 100,
                )
                full_text = youtube_service.get_full_text(segments)
                punctuated = await punctuate_transcript(full_text, language=body.language)
                if punctuated:
                    segments = youtube_service.apply_punctuation_to_segments(segments, punctuated)

        merged_segments = (
            youtube_service.merge_segments_smart(segments, max_duration=body.max_segment_duration)
            if segments
            else []
        )

        difficulty = None
        difficulty_values = {}
        if merged_segments and not dispatch_stt:
            difficulty = calculate_audio_difficulty(
                None,
                merged_segments,
                options={"duration_seconds": video_duration, "language": body.language},
            )
            difficulty_values = difficulty_update_values(difficulty)
            difficulty_values["difficulty_updated_at"] = datetime.now(UTC)

        if body.level:
            detected_level = body.level
        elif difficulty:
            detected_level = difficulty["level"]
            logger.info("Auto-detected level for video %s: %s", video_id, detected_level)
        else:
            detected_level = None

        # ── Phase 2: Database writes (all external data ready) ────────────────
        logger.info(
            "Import auto-gen detection for %s: is_auto=%s (from YouTube metadata)",
            video_id,
            is_auto_generated,
        )

        now = _now()
        publish_status = PUBLISH_STATUS_PUBLISHED if user.is_admin else PUBLISH_STATUS_PRIVATE
        video = Video(
            youtube_id=video_id,
            title=resolved_title,
            channel=resolved_channel,
            duration=video_duration,
            language=body.language,
            level=detected_level,
            is_curated=False,
            is_active=True,
            publish_status=publish_status,
            published_at=now if user.is_admin else None,
            reviewed_by=user.id if user.is_admin else None,
            reviewed_at=now if user.is_admin else None,
            review_note="Published by admin import" if user.is_admin else None,
            is_auto_generated=is_auto_generated,
            transcription_status="pending" if dispatch_stt else "ready",
            thumbnail_url=resolved_thumbnail,
            created_by=user.id,
            **difficulty_values,
        )
        self.repo.add(video)
        try:
            await self.repo.flush()
        except IntegrityError:
            await self.repo.rollback()
            raise ConflictError(f"Video {video_id} already imported")

        practice = await self.repo.create_user_practice(user.id, video.id)
        await self.repo.set_user_practice_tags(practice.id, topic_tag_ids)
        if user.is_admin:
            await self.repo.set_video_public_tags(video.id, topic_tag_ids)

        for idx, segment in enumerate(merged_segments):
            self.repo.add(
                Transcript(
                    video_id=video.id,
                    language=body.language,
                    index=idx,
                    text=segment.text,
                    start_time=segment.start,
                    end_time=segment.end,
                )
            )

        await self.repo.commit()
        await self.repo.refresh(video)
        await publish_user_recommendation_event(
            user.id,
            "my_practice_changed",
            video.id,
        )
        if user.is_admin:
            await publish_video_event("video.published", video.id)

        if dispatch_stt:
            task = run_stt_pipeline.delay(
                video_db_id=video.id,
                youtube_id=video_id,
                language=body.language,
                video_duration=video_duration,
                max_segment_duration=body.max_segment_duration,
                title=resolved_title,
                channel=resolved_channel,
                prompt_context=_caption_prompt_text(merged_segments) if merged_segments else None,
            )
            logger.info(
                "Dispatched STT Celery task %s for video %s (%s)", task.id, video.id, video_id
            )

        return ImportVideoResponse(
            video=await self._serialize_video(video, user=user),
            already_exists=False,
            already_in_my_practice=False,
            message=(
                "Video imported and published."
                if user.is_admin
                else "Video imported successfully. Added to your My Practice."
            ),
            similar_importers_count=0,
            similar_importers=[],
        )

    # ── Reads ───────────────────────────────────────────────────────────────

    def list_transcript_languages(self, video_id: str):
        return youtube_service.list_available_transcripts(video_id)

    async def get_transcription_status(self, user: User | None, video_id: str) -> dict:
        await self._require_video_access(user, video_id)
        row = await self.repo.get_transcription_status(video_id)
        if not row:
            raise NotFoundError("Video not found")
        return {"status": row[0], "error": row[1]}

    async def get_video(self, user: User | None, video_id: str) -> VideoResponse:
        video = await self._require_video_access(user, video_id)
        return await self._serialize_video(video, user=user)

    async def get_transcripts(self, user: User | None, video_id: str) -> list[Transcript]:
        await self._require_video_access(user, video_id)
        return await self.repo.get_transcripts_ordered(video_id)

    async def get_video_edit_status(self, user: User, video_id: str) -> VideoEditStatusResponse:
        await self._require_video_access(user, video_id)
        in_progress = await self.repo.has_in_progress_attempt(user.id, video_id)
        return VideoEditStatusResponse(has_in_progress_attempt=in_progress)

    # ── Mutations ───────────────────────────────────────────────────────────

    async def bulk_update_transcripts(
        self, user: User, video_id: str, body: TranscriptBulkUpdateRequest
    ) -> TranscriptBulkUpdateResponse:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")
        if not user.is_admin:
            raise ForbiddenError("Only admins can edit shared transcripts")

        ids = [item.transcript_id for item in body.items]
        if len(ids) != len(set(ids)):
            raise BadRequestError("Duplicate transcript_id values are not allowed")

        rows = await self.repo.get_transcripts_by_ids(ids)
        rows_by_id = {r.id: r for r in rows}
        all_transcripts = await self.repo.get_transcripts_ordered(video_id)

        deleted_ids: set[str] = set()
        changed_ids: set[str] = set()
        pending_updates: dict[str, tuple[str, float, float]] = {}
        deleted = 0
        for item in body.items:
            row = rows_by_id.get(item.transcript_id)
            if not row or row.video_id != video_id:
                raise NotFoundError(f"Transcript {item.transcript_id} not found for this video")
            if item.is_deleted:
                deleted_ids.add(row.id)
                deleted += 1
            else:
                new_text = item.text if item.text else row.text
                new_start = item.start_time if item.start_time is not None else row.start_time
                new_end = item.end_time if item.end_time is not None else row.end_time

                if new_text != row.text or new_start != row.start_time or new_end != row.end_time:
                    changed_ids.add(row.id)

                pending_updates[row.id] = (new_text, new_start, new_end)

        remaining_after_update = []
        for transcript in all_transcripts:
            if transcript.id in deleted_ids:
                continue
            _text, start_time, end_time = pending_updates.get(
                transcript.id,
                (transcript.text, transcript.start_time, transcript.end_time),
            )
            remaining_after_update.append((transcript.id, transcript.index, start_time, end_time))
        _validate_transcript_timeline(remaining_after_update)

        for transcript_id, (text, start_time, end_time) in pending_updates.items():
            row = rows_by_id[transcript_id]
            row.text = text
            row.start_time = start_time
            row.end_time = end_time

        for transcript_id in deleted_ids:
            await self.repo.delete(rows_by_id[transcript_id])

        if deleted > 0:
            remaining = await self.repo.get_transcripts_ordered(video_id)
            for idx, t in enumerate(remaining):
                t.index = idx

        await self.repo.commit()
        return TranscriptBulkUpdateResponse(updated=len(changed_ids) + deleted)

    async def analyze_video_level(self, user: User, video_id: str) -> dict:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")
        if not user.is_admin:
            raise ForbiddenError("Only admins can update shared video metadata")

        transcripts = await self.repo.get_transcripts_ordered(video_id)
        if not transcripts:
            raise NotFoundError("No transcript found for this video")

        analysis = calculate_audio_difficulty(
            video,
            transcripts,
            options={"duration_seconds": video.duration, "language": video.language},
        )

        video.level = analysis["level"]
        for field, value in difficulty_update_values(analysis).items():
            setattr(video, field, value)
        video.difficulty_updated_at = datetime.now(UTC)
        await self.repo.commit()
        await publish_video_event(
            "video.difficulty_updated",
            video.id,
            {"level": video.level},
        )

        return {
            "level": analysis["level"],
            "score": analysis["score"],
            "features": analysis["factors"],
            "label": analysis["label"],
            "factors": analysis["factors"],
            "explanation": analysis["explanation"],
            "recommendedModes": analysis["recommendedModes"],
        }

    async def delete_video(self, user: User, video_id: str) -> None:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")
        if not user.is_admin:
            raise ForbiddenError("Only admins can delete shared videos")

        await self.repo.delete_video_and_related(video)
        await self.repo.commit()
        await publish_video_event("video.deleted", video_id)

    async def list_topic_tags(self) -> list[TopicTag]:
        return await self.repo.list_active_topic_tags()

    async def list_admin_topic_tags(self, include_inactive: bool = True) -> list[TopicTag]:
        return await self.repo.list_topic_tags(include_inactive=include_inactive)

    async def create_topic_tag(self, body: AdminTopicTagCreate) -> TopicTag:
        slug = _normalize_slug(body.slug)
        if await self.repo.get_topic_tag_by_slug(slug):
            raise ConflictError("Topic tag slug already exists")
        tag = TopicTag(
            slug=slug,
            name=body.name.strip(),
            description=body.description,
            is_active=body.is_active,
            sort_order=body.sort_order,
        )
        self.repo.add(tag)
        await self.repo.commit()
        await self.repo.refresh(tag)
        await publish_video_event(
            "topic_tag.created",
            None,
            {"tag_id": tag.id},
        )
        return tag

    async def patch_topic_tag(self, tag_id: str, body: AdminTopicTagPatch) -> TopicTag:
        tag = await self.repo.get_topic_tag(tag_id)
        if not tag:
            raise NotFoundError("Topic tag not found")
        if body.slug is not None:
            slug = _normalize_slug(body.slug)
            existing = await self.repo.get_topic_tag_by_slug(slug)
            if existing and existing.id != tag.id:
                raise ConflictError("Topic tag slug already exists")
            tag.slug = slug
        if body.name is not None:
            tag.name = body.name.strip()
        if "description" in body.model_fields_set:
            tag.description = body.description
        if body.is_active is not None:
            tag.is_active = body.is_active
        if body.sort_order is not None:
            tag.sort_order = body.sort_order
        await self.repo.commit()
        await self.repo.refresh(tag)
        await publish_video_event(
            "topic_tag.updated",
            None,
            {"tag_id": tag.id},
        )
        return tag

    async def deactivate_topic_tag(self, tag_id: str) -> None:
        tag = await self.repo.get_topic_tag(tag_id)
        if not tag:
            raise NotFoundError("Topic tag not found")
        tag.is_active = False
        await self.repo.commit()
        await publish_video_event(
            "topic_tag.deactivated",
            None,
            {"tag_id": tag.id},
        )

    async def request_publish(self, user: User, video_id: str, body: PublishRequestCreate) -> dict:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")
        practice = await self.repo.get_user_practice(user.id, video_id)
        if not practice and not user.is_admin:
            raise ForbiddenError("You can only request publish for videos in My Practice")
        if _is_public(video):
            return {"status": PUBLISH_STATUS_PUBLISHED, "message": "Video is already public"}
        if user.is_admin:
            await self._publish_video_as_admin(
                video,
                user,
                review_note="Published by admin request",
            )
            await self.repo.commit()
            await publish_video_event("video.published", video.id)
            return {"status": PUBLISH_STATUS_PUBLISHED, "message": "Video published"}

        request = await self.repo.get_pending_publish_request(user.id, video_id)
        if not request:
            request = await self.repo.create_publish_request(user.id, video_id, body.message)
        video.publish_status = PUBLISH_STATUS_PENDING
        await self.repo.commit()
        await self.repo.refresh(request)
        return {
            "status": PUBLISH_STATUS_PENDING,
            "request": PublishRequestResponse.model_validate(request),
        }

    async def remove_from_my_practice(self, user: User, video_id: str) -> None:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")
        practice = await self.repo.get_user_practice(user.id, video_id)
        if not practice:
            raise NotFoundError("Video not found in My Practice")

        await self.repo.resolve_user_pending_publish_requests(
            user.id,
            video_id,
            "Requester removed the video from My Practice",
        )
        if _effective_publish_status(video) == PUBLISH_STATUS_PENDING:
            pending_count = await self.repo.count_pending_publish_requests(video_id)
            if pending_count == 0:
                video.publish_status = PUBLISH_STATUS_PRIVATE
                video.review_note = None
                video.reviewed_by = None
                video.reviewed_at = None

        await self.repo.delete_user_practice(practice)
        await self.repo.commit()
        await publish_user_recommendation_event(
            user.id,
            "my_practice_changed",
            video_id,
        )

    async def create_transcript_feedback(
        self, user: User, video_id: str, body: TranscriptFeedbackCreate
    ) -> TranscriptFeedbackResponse:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")
        practice = await self.repo.get_user_practice(user.id, video_id)
        if not user.is_admin and not _is_public(video) and not practice:
            raise ForbiddenError(
                "You can only send feedback for public videos or private videos in My Practice"
            )
        if body.transcript_id:
            transcript = await self.repo.get_transcript_by_id(body.transcript_id)
            if not transcript or transcript.video_id != video_id:
                raise NotFoundError("Transcript segment not found")
        feedback = await self.repo.create_transcript_feedback(
            user.id,
            video_id,
            body.transcript_id,
            body.message,
            body.suggested_text,
        )
        await self.repo.commit()
        await self.repo.refresh(feedback)
        return TranscriptFeedbackResponse.model_validate(feedback)

    async def list_publish_requests(self, status: str | None, page: int, page_size: int) -> dict:
        rows, total = await self.repo.list_publish_requests(status, page, page_size)
        total_pages = max(1, math.ceil(total / page_size))
        items = []
        for request, video, requester in rows:
            items.append(
                {
                    "id": request.id,
                    "status": request.status,
                    "message": request.message,
                    "admin_note": request.admin_note,
                    "created_at": request.created_at,
                    "updated_at": request.updated_at,
                    "reviewed_by": request.reviewed_by,
                    "reviewed_at": request.reviewed_at,
                    "video": await self._serialize_video(video),
                    "requester": ImporterSummaryResponse(
                        id=requester.id, display_name=requester.display_name
                    ),
                }
            )
        return {"items": items, "total": total, "page": page, "total_pages": total_pages}

    async def approve_publish_request(
        self, admin: User, request_id: str, body: AdminPublishRequestAction
    ) -> dict:
        request = await self.repo.get_publish_request(request_id)
        if not request:
            raise NotFoundError("Publish request not found")
        video = await self.repo.get_by_id(request.video_id)
        if not video:
            raise NotFoundError("Video not found")

        if body.topic_tag_ids is not None:
            topic_tag_ids = await self._validate_active_tag_ids(body.topic_tag_ids)
        else:
            requester_tags = await self.repo.get_user_practice_tags(
                request.user_id, request.video_id
            )
            topic_tag_ids = [tag.id for tag in requester_tags]

        request.status = REQUEST_STATUS_APPROVED
        request.admin_note = body.admin_note
        request.reviewed_by = admin.id
        request.reviewed_at = _now()
        await self._publish_video_as_admin(
            video,
            admin,
            topic_tag_ids=topic_tag_ids,
            review_note=body.admin_note,
        )
        await self.repo.commit()
        await self.repo.refresh(request)
        await self.repo.refresh(video)
        await publish_video_event("video.published", video.id)
        return {
            "request": PublishRequestResponse.model_validate(request),
            "video": await self._serialize_video(video),
        }

    async def reject_publish_request(
        self, admin: User, request_id: str, body: AdminPublishRequestAction
    ) -> dict:
        request = await self.repo.get_publish_request(request_id)
        if not request:
            raise NotFoundError("Publish request not found")
        video = await self.repo.get_by_id(request.video_id)
        if not video:
            raise NotFoundError("Video not found")

        request.status = REQUEST_STATUS_REJECTED
        request.admin_note = body.admin_note
        request.reviewed_by = admin.id
        request.reviewed_at = _now()
        pending_count = await self.repo.count_pending_publish_requests(video.id)
        if not _is_public(video) and pending_count == 0:
            video.publish_status = PUBLISH_STATUS_REJECTED
            video.review_note = body.admin_note
            video.reviewed_by = admin.id
            video.reviewed_at = request.reviewed_at
        await self.repo.commit()
        await self.repo.refresh(request)
        return {"request": PublishRequestResponse.model_validate(request)}

    async def update_video_public_tags(
        self, admin: User, video_id: str, body: VideoTopicTagsUpdate
    ) -> VideoResponse:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")
        topic_tag_ids = await self._validate_active_tag_ids(body.topic_tag_ids)
        await self.repo.set_video_public_tags(video_id, topic_tag_ids)
        video.reviewed_by = admin.id
        video.reviewed_at = _now()
        await self.repo.commit()
        await self.repo.refresh(video)
        await publish_video_event(
            "video.topic_tags_updated",
            video.id,
            {"topic_tag_ids": topic_tag_ids},
        )
        return await self._serialize_video(video)

    async def list_transcript_feedback(
        self, status: str | None, page: int, page_size: int
    ) -> AdminTranscriptFeedbackListResponse:
        if status and status not in VALID_FEEDBACK_STATUSES:
            raise BadRequestError("Invalid feedback status")
        rows, total = await self.repo.list_transcript_feedback(status, page, page_size)
        total_pages = max(1, math.ceil(total / page_size))
        items = []
        for feedback, video, requester, transcript in rows:
            items.append(
                AdminTranscriptFeedbackItemResponse(
                    id=feedback.id,
                    video_id=feedback.video_id,
                    transcript_id=feedback.transcript_id,
                    message=feedback.message,
                    suggested_text=feedback.suggested_text,
                    status=feedback.status,
                    admin_note=feedback.admin_note,
                    created_at=feedback.created_at,
                    updated_at=feedback.updated_at,
                    user_id=requester.id,
                    user_name=requester.display_name,
                    user_email=requester.email,
                    video_title=video.title,
                    transcript_text=transcript.text if transcript else None,
                )
            )
        return AdminTranscriptFeedbackListResponse(
            items=items,
            total=total,
            page=page,
            total_pages=total_pages,
        )

    async def patch_transcript_feedback(
        self, admin: User, feedback_id: str, body: AdminFeedbackPatch
    ) -> TranscriptFeedbackResponse:
        feedback = await self.repo.get_transcript_feedback(feedback_id)
        if not feedback:
            raise NotFoundError("Transcript feedback not found")
        if body.status is not None:
            if body.status not in VALID_FEEDBACK_STATUSES:
                raise BadRequestError("Invalid feedback status")
            feedback.status = body.status
        if "admin_note" in body.model_fields_set:
            feedback.admin_note = body.admin_note
        feedback.reviewed_by = admin.id
        feedback.reviewed_at = _now()
        await self.repo.commit()
        await self.repo.refresh(feedback)
        return TranscriptFeedbackResponse.model_validate(feedback)

    async def refresh_transcript(
        self, user: User, video_id: str, max_segment_duration: float
    ) -> Video:
        video = await self.repo.get_by_id(video_id)
        if not video:
            raise NotFoundError("Video not found")
        if not user.is_admin:
            raise ForbiddenError("Only admins can refresh shared transcripts")

        metadata = await asyncio.to_thread(youtube_service.get_video_metadata, video.youtube_id)
        if metadata.get("title"):
            video.title = metadata["title"]
        if metadata.get("channel"):
            video.channel = metadata["channel"]
        if metadata.get("thumbnail_url"):
            video.thumbnail_url = metadata["thumbnail_url"]
        if metadata.get("duration"):
            video.duration = metadata["duration"]

        transcript_result = None
        segments = None
        transcript_error = None
        try:
            transcript_result = await asyncio.to_thread(
                youtube_service.get_transcript, video.youtube_id
            )
            segments = transcript_result.segments
        except TranscriptsDisabled as e:
            transcript_error = e
        except VideoUnavailable:
            raise NotFoundError(f"Video '{video.youtube_id}' is unavailable or has been removed.")
        except Exception as e:
            transcript_error = e

        if segments is None:
            try:
                transcript_result = await asyncio.to_thread(
                    youtube_service.get_transcript_ytdlp,
                    video.youtube_id,
                    [video.language],
                )
                segments = transcript_result.segments
                transcript_error = None
            except Exception as e:
                transcript_error = transcript_error or e

        dispatch_stt_refresh = False
        if segments is None:
            if video.duration <= STT_MAX_DURATION_SECONDS:
                dispatch_stt_refresh = True
                segments = []
                logger.info(
                    "No subtitles found on refresh for %s - dispatching AssemblyAI STT",
                    video_id,
                )
            else:
                raise BadRequestError(
                    "No manual subtitles are available and AI transcription is limited to "
                    f"{STT_MAX_DURATION_SECONDS // 60} minutes. "
                    f"This video is {video.duration // 60}m {video.duration % 60}s. "
                    f"Last transcript error: {type(transcript_error).__name__}: {transcript_error}"
                )

        refresh_is_auto = transcript_result.is_generated if transcript_result else False
        logger.info(
            "Refresh auto-gen detection for %s: is_auto=%s (from YouTube metadata)",
            video_id,
            refresh_is_auto,
        )

        if refresh_is_auto:
            if video.duration and video.duration > STT_MAX_DURATION_SECONDS:
                raise BadRequestError(
                    "Videos without manual subtitles are limited to "
                    f"{STT_MAX_DURATION_SECONDS // 60} minutes for AI transcription. "
                    f"This video is {video.duration // 60}m {video.duration % 60}s. "
                    f"Please choose a shorter video or one with manually uploaded subtitles."
                )
            dispatch_stt_refresh = True
            logger.info(
                "Auto-generated captions detected on refresh for %s - dispatching AssemblyAI STT",
                video_id,
            )

        if segments and not dispatch_stt_refresh:
            ends_with_punct = sum(
                1 for seg in segments if seg.text.rstrip().endswith((".", "?", "!"))
            )
            ratio = ends_with_punct / len(segments) if segments else 1.0
            if ratio < 0.30:
                logger.info(
                    "Low punctuation ratio on refresh for %s (%.0f%%) — "
                    "calling Gemini to restore punctuation",
                    video_id,
                    ratio * 100,
                )
                full_text = youtube_service.get_full_text(segments)
                punctuated = await punctuate_transcript(full_text, language=video.language)
                if punctuated:
                    segments = youtube_service.apply_punctuation_to_segments(segments, punctuated)

        merged_segments = (
            youtube_service.merge_segments_smart(segments, max_duration=max_segment_duration)
            if segments
            else []
        )

        video.is_auto_generated = refresh_is_auto
        video.transcription_status = "pending" if dispatch_stt_refresh else "ready"

        await self.repo.delete_transcripts_for_video(video_id)
        for idx, segment in enumerate(merged_segments):
            self.repo.add(
                Transcript(
                    video_id=video.id,
                    language=video.language,
                    index=idx,
                    text=segment.text,
                    start_time=segment.start,
                    end_time=segment.end,
                )
            )

        if merged_segments and not video.duration:
            video.duration = int(merged_segments[-1].end)

        if merged_segments and not dispatch_stt_refresh:
            difficulty = calculate_audio_difficulty(
                video,
                merged_segments,
                options={"duration_seconds": video.duration, "language": video.language},
            )
            for field, value in difficulty_update_values(difficulty).items():
                setattr(video, field, value)
            video.difficulty_updated_at = datetime.now(UTC)
            video.level = difficulty["level"]
            logger.info("Re-analyzed level for video %s: %s", video_id, video.level)

        await self.repo.commit()
        await self.repo.refresh(video)
        await publish_video_event(
            "video.transcript_refreshed",
            video.id,
            {
                "transcription_status": video.transcription_status,
                "level": video.level,
            },
        )

        if dispatch_stt_refresh:
            task = run_stt_pipeline.delay(
                video_db_id=video.id,
                youtube_id=video.youtube_id,
                language=video.language,
                video_duration=video.duration,
                max_segment_duration=max_segment_duration,
                title=video.title,
                channel=video.channel,
                prompt_context=_caption_prompt_text(merged_segments) if merged_segments else None,
            )
            logger.info("Dispatched STT Celery task %s on refresh for %s", task.id, video_id)

        return video
