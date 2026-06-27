"""Database access for videos, transcripts, and related cleanup."""

from datetime import UTC, datetime

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dictation import DictationAttempt, DictationSentence
from app.models.room import RoomSession
from app.models.user import User
from app.models.video import (
    TopicTag,
    Transcript,
    TranscriptFeedback,
    UserPracticeVideo,
    UserPracticeVideoTag,
    Video,
    VideoPublishRequest,
    VideoTopicTag,
)

PUBLISH_STATUS_PUBLISHED = "published"
PUBLISH_STATUS_PRIVATE = "private"
PUBLISH_STATUS_PENDING = "pending_review"
PUBLISH_STATUS_REJECTED = "rejected"

REQUEST_STATUS_PENDING = "pending"
REQUEST_STATUS_APPROVED = "approved"
REQUEST_STATUS_REJECTED = "rejected"
REQUEST_STATUS_RESOLVED = "resolved"


class VideoRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Listing ─────────────────────────────────────────────────────────────

    async def list_with_stats(
        self,
        language: str | None,
        level: str | None,
        curated: bool | None,
        topic_tag: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list, int]:
        stats_sub = (
            select(
                DictationAttempt.video_id,
                func.count().filter(DictationAttempt.status == "completed").label("play_count"),
                func.max(DictationAttempt.score).label("best_score"),
            )
            .group_by(DictationAttempt.video_id)
            .subquery()
        )

        base = (
            select(
                Video,
                func.coalesce(stats_sub.c.play_count, 0).label("play_count"),
                stats_sub.c.best_score,
            )
            .outerjoin(stats_sub, Video.id == stats_sub.c.video_id)
            .where(
                Video.is_active == True,  # noqa: E712
                Video.publish_status == PUBLISH_STATUS_PUBLISHED,
            )
        )
        count_q = (
            select(func.count(func.distinct(Video.id)))
            .select_from(Video)
            .where(
                Video.is_active == True,  # noqa: E712
                Video.publish_status == PUBLISH_STATUS_PUBLISHED,
            )
        )
        if topic_tag:
            base = (
                base.join(VideoTopicTag, VideoTopicTag.video_id == Video.id)
                .join(TopicTag, TopicTag.id == VideoTopicTag.tag_id)
                .where(TopicTag.slug == topic_tag)
            )
            count_q = (
                count_q.join(VideoTopicTag, VideoTopicTag.video_id == Video.id)
                .join(TopicTag, TopicTag.id == VideoTopicTag.tag_id)
                .where(TopicTag.slug == topic_tag)
            )
        if language:
            base = base.where(Video.language == language)
            count_q = count_q.where(Video.language == language)
        if level:
            base = base.where(Video.level == level)
            count_q = count_q.where(Video.level == level)
        if curated is not None:
            base = base.where(Video.is_curated == curated)
            count_q = count_q.where(Video.is_curated == curated)
        total = (await self.db.execute(count_q)).scalar() or 0

        query = (
            base.order_by(Video.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        )
        rows = (await self.db.execute(query)).all()
        return rows, total

    async def list_my_practice_with_stats(
        self,
        user_id: str,
        publish_status: str | None,
        language: str | None,
        level: str | None,
        transcription_status: str | None,
        topic_tag: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list, int]:
        stats_sub = (
            select(
                DictationAttempt.video_id,
                func.count().filter(DictationAttempt.status == "completed").label("play_count"),
                func.max(DictationAttempt.score).label("best_score"),
            )
            .where(DictationAttempt.user_id == user_id)
            .group_by(DictationAttempt.video_id)
            .subquery()
        )

        base = (
            select(
                Video,
                UserPracticeVideo.created_at.label("my_practice_created_at"),
                func.coalesce(stats_sub.c.play_count, 0).label("play_count"),
                stats_sub.c.best_score,
            )
            .join(UserPracticeVideo, UserPracticeVideo.video_id == Video.id)
            .outerjoin(stats_sub, Video.id == stats_sub.c.video_id)
            .where(
                UserPracticeVideo.user_id == user_id,
                Video.is_active == True,  # noqa: E712
            )
        )
        count_q = (
            select(func.count(func.distinct(Video.id)))
            .select_from(Video)
            .join(UserPracticeVideo, UserPracticeVideo.video_id == Video.id)
            .where(
                UserPracticeVideo.user_id == user_id,
                Video.is_active == True,  # noqa: E712
            )
        )
        if topic_tag:
            base = (
                base.join(
                    UserPracticeVideoTag,
                    UserPracticeVideoTag.practice_video_id == UserPracticeVideo.id,
                )
                .join(TopicTag, TopicTag.id == UserPracticeVideoTag.tag_id)
                .where(TopicTag.slug == topic_tag)
            )
            count_q = (
                count_q.join(
                    UserPracticeVideoTag,
                    UserPracticeVideoTag.practice_video_id == UserPracticeVideo.id,
                )
                .join(TopicTag, TopicTag.id == UserPracticeVideoTag.tag_id)
                .where(TopicTag.slug == topic_tag)
            )
        if publish_status:
            base = base.where(Video.publish_status == publish_status)
            count_q = count_q.where(Video.publish_status == publish_status)
        if language:
            base = base.where(Video.language == language)
            count_q = count_q.where(Video.language == language)
        if level:
            base = base.where(Video.level == level)
            count_q = count_q.where(Video.level == level)
        if transcription_status:
            base = base.where(Video.transcription_status == transcription_status)
            count_q = count_q.where(Video.transcription_status == transcription_status)

        total = (await self.db.execute(count_q)).scalar() or 0
        rows = (
            await self.db.execute(
                base.order_by(UserPracticeVideo.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return rows, total

    async def list_recommendation_candidates(
        self, user_id: str, limit: int = 500
    ) -> list:
        completed_stats = (
            select(
                DictationAttempt.video_id,
                func.count().label("play_count"),
                func.max(DictationAttempt.score).label("best_score"),
            )
            .where(DictationAttempt.status == "completed")
            .group_by(DictationAttempt.video_id)
            .subquery()
        )
        attempted = exists().where(
            DictationAttempt.user_id == user_id,
            DictationAttempt.video_id == Video.id,
        )
        has_transcript = exists().where(Transcript.video_id == Video.id)
        query = (
            select(
                Video,
                func.coalesce(completed_stats.c.play_count, 0).label("play_count"),
                completed_stats.c.best_score,
            )
            .outerjoin(completed_stats, completed_stats.c.video_id == Video.id)
            .where(
                Video.is_active == True,  # noqa: E712
                Video.publish_status == PUBLISH_STATUS_PUBLISHED,
                Video.transcription_status == "ready",
                has_transcript,
                ~attempted,
            )
            .order_by(
                Video.is_curated.desc(),
                func.coalesce(completed_stats.c.play_count, 0).desc(),
                func.coalesce(Video.published_at, Video.created_at).desc(),
                Video.id,
            )
            .limit(limit)
        )
        return (await self.db.execute(query)).all()

    async def list_recent_completed_attempts(
        self, user_id: str, limit: int = 20
    ) -> list:
        query = (
            select(DictationAttempt, Video)
            .join(Video, Video.id == DictationAttempt.video_id)
            .where(
                DictationAttempt.user_id == user_id,
                DictationAttempt.status == "completed",
            )
            .order_by(
                DictationAttempt.completed_at.desc().nullslast(),
                DictationAttempt.updated_at.desc(),
            )
            .limit(limit)
        )
        return (await self.db.execute(query)).all()

    async def list_unattempted_practice_videos(
        self, user_id: str, limit: int = 500
    ) -> list[Video]:
        attempted = exists().where(
            DictationAttempt.user_id == user_id,
            DictationAttempt.video_id == Video.id,
        )
        result = await self.db.execute(
            select(Video)
            .join(UserPracticeVideo, UserPracticeVideo.video_id == Video.id)
            .where(
                UserPracticeVideo.user_id == user_id,
                Video.is_active == True,  # noqa: E712
                ~attempted,
            )
            .order_by(UserPracticeVideo.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_public_tags_for_videos(
        self, video_ids: list[str]
    ) -> list[tuple[str, TopicTag]]:
        if not video_ids:
            return []
        rows = await self.db.execute(
            select(VideoTopicTag.video_id, TopicTag)
            .join(TopicTag, TopicTag.id == VideoTopicTag.tag_id)
            .where(
                VideoTopicTag.video_id.in_(video_ids),
                TopicTag.is_active == True,  # noqa: E712
            )
            .order_by(TopicTag.sort_order, TopicTag.name)
        )
        return [(row[0], row[1]) for row in rows.all()]

    async def list_user_practice_tags_for_videos(
        self, user_id: str, video_ids: list[str]
    ) -> list[tuple[str, TopicTag]]:
        if not video_ids:
            return []
        rows = await self.db.execute(
            select(UserPracticeVideo.video_id, TopicTag)
            .join(
                UserPracticeVideoTag,
                UserPracticeVideoTag.practice_video_id == UserPracticeVideo.id,
            )
            .join(TopicTag, TopicTag.id == UserPracticeVideoTag.tag_id)
            .where(
                UserPracticeVideo.user_id == user_id,
                UserPracticeVideo.video_id.in_(video_ids),
            )
            .order_by(TopicTag.sort_order, TopicTag.name)
        )
        return [(row[0], row[1]) for row in rows.all()]

    async def list_unattempted_practice_tags(
        self, user_id: str
    ) -> list[tuple[str, TopicTag]]:
        attempted = exists().where(
            DictationAttempt.user_id == user_id,
            DictationAttempt.video_id == UserPracticeVideo.video_id,
        )
        rows = await self.db.execute(
            select(UserPracticeVideo.video_id, TopicTag)
            .join(
                UserPracticeVideoTag,
                UserPracticeVideoTag.practice_video_id == UserPracticeVideo.id,
            )
            .join(TopicTag, TopicTag.id == UserPracticeVideoTag.tag_id)
            .where(
                UserPracticeVideo.user_id == user_id,
                TopicTag.is_active == True,  # noqa: E712
                ~attempted,
            )
            .order_by(TopicTag.sort_order, TopicTag.name)
        )
        return [(row[0], row[1]) for row in rows.all()]

    # ── Lookups ─────────────────────────────────────────────────────────────

    async def get_by_youtube_id(self, youtube_id: str) -> Video | None:
        result = await self.db.execute(select(Video).where(Video.youtube_id == youtube_id))
        return result.scalar_one_or_none()

    async def get_by_id(self, video_id: str) -> Video | None:
        result = await self.db.execute(select(Video).where(Video.id == video_id))
        return result.scalar_one_or_none()

    async def get_user_practice(self, user_id: str, video_id: str) -> UserPracticeVideo | None:
        result = await self.db.execute(
            select(UserPracticeVideo).where(
                UserPracticeVideo.user_id == user_id,
                UserPracticeVideo.video_id == video_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_active_topic_tags_by_ids(self, tag_ids: list[str]) -> list[TopicTag]:
        if not tag_ids:
            return []
        result = await self.db.execute(
            select(TopicTag)
            .where(TopicTag.id.in_(tag_ids), TopicTag.is_active == True)  # noqa: E712
            .order_by(TopicTag.sort_order, TopicTag.name)
        )
        return list(result.scalars().all())

    async def list_active_topic_tags(self) -> list[TopicTag]:
        result = await self.db.execute(
            select(TopicTag)
            .where(TopicTag.is_active == True)  # noqa: E712
            .order_by(TopicTag.sort_order, TopicTag.name)
        )
        return list(result.scalars().all())

    async def list_topic_tags(self, include_inactive: bool = False) -> list[TopicTag]:
        query = select(TopicTag).order_by(TopicTag.sort_order, TopicTag.name)
        if not include_inactive:
            query = query.where(TopicTag.is_active == True)  # noqa: E712
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_topic_tag(self, tag_id: str) -> TopicTag | None:
        result = await self.db.execute(select(TopicTag).where(TopicTag.id == tag_id))
        return result.scalar_one_or_none()

    async def get_topic_tag_by_slug(self, slug: str) -> TopicTag | None:
        result = await self.db.execute(select(TopicTag).where(TopicTag.slug == slug))
        return result.scalar_one_or_none()

    async def get_video_public_tags(self, video_id: str) -> list[TopicTag]:
        result = await self.db.execute(
            select(TopicTag)
            .join(VideoTopicTag, VideoTopicTag.tag_id == TopicTag.id)
            .where(VideoTopicTag.video_id == video_id)
            .order_by(TopicTag.sort_order, TopicTag.name)
        )
        return list(result.scalars().all())

    async def get_user_practice_tags(self, user_id: str, video_id: str) -> list[TopicTag]:
        result = await self.db.execute(
            select(TopicTag)
            .join(UserPracticeVideoTag, UserPracticeVideoTag.tag_id == TopicTag.id)
            .join(
                UserPracticeVideo,
                UserPracticeVideo.id == UserPracticeVideoTag.practice_video_id,
            )
            .where(
                UserPracticeVideo.user_id == user_id,
                UserPracticeVideo.video_id == video_id,
            )
            .order_by(TopicTag.sort_order, TopicTag.name)
        )
        return list(result.scalars().all())

    async def get_similar_importers(
        self, video_id: str, exclude_user_id: str | None = None, limit: int = 10
    ) -> tuple[list[User], int]:
        filters = [UserPracticeVideo.video_id == video_id]
        if exclude_user_id:
            filters.append(UserPracticeVideo.user_id != exclude_user_id)
        count_q = select(func.count()).select_from(UserPracticeVideo).where(*filters)
        total = (await self.db.execute(count_q)).scalar() or 0
        rows = await self.db.execute(
            select(User)
            .join(UserPracticeVideo, UserPracticeVideo.user_id == User.id)
            .where(*filters)
            .order_by(UserPracticeVideo.created_at.desc())
            .limit(limit)
        )
        return list(rows.scalars().all()), total

    async def get_transcript_by_id(self, transcript_id: str) -> Transcript | None:
        result = await self.db.execute(select(Transcript).where(Transcript.id == transcript_id))
        return result.scalar_one_or_none()

    async def get_pending_publish_request(
        self, user_id: str, video_id: str
    ) -> VideoPublishRequest | None:
        result = await self.db.execute(
            select(VideoPublishRequest).where(
                VideoPublishRequest.user_id == user_id,
                VideoPublishRequest.video_id == video_id,
                VideoPublishRequest.status == REQUEST_STATUS_PENDING,
            )
        )
        return result.scalar_one_or_none()

    async def count_pending_publish_requests(self, video_id: str) -> int:
        result = await self.db.execute(
            select(func.count())
            .select_from(VideoPublishRequest)
            .where(
                VideoPublishRequest.video_id == video_id,
                VideoPublishRequest.status == REQUEST_STATUS_PENDING,
            )
        )
        return result.scalar() or 0

    async def resolve_user_pending_publish_requests(
        self, user_id: str, video_id: str, note: str
    ) -> int:
        result = await self.db.execute(
            select(VideoPublishRequest).where(
                VideoPublishRequest.user_id == user_id,
                VideoPublishRequest.video_id == video_id,
                VideoPublishRequest.status == REQUEST_STATUS_PENDING,
            )
        )
        now = datetime.now(UTC)
        resolved = 0
        for request in result.scalars().all():
            request.status = REQUEST_STATUS_RESOLVED
            request.reviewed_at = now
            request.admin_note = note
            resolved += 1
        return resolved

    async def list_publish_requests(
        self,
        status: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list, int]:
        base = (
            select(VideoPublishRequest, Video, User)
            .join(Video, VideoPublishRequest.video_id == Video.id)
            .join(User, VideoPublishRequest.user_id == User.id)
        )
        count_q = select(func.count()).select_from(VideoPublishRequest)
        if status:
            base = base.where(VideoPublishRequest.status == status)
            count_q = count_q.where(VideoPublishRequest.status == status)
        total = (await self.db.execute(count_q)).scalar() or 0
        rows = (
            await self.db.execute(
                base.order_by(VideoPublishRequest.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return rows, total

    async def get_publish_request(self, request_id: str) -> VideoPublishRequest | None:
        result = await self.db.execute(
            select(VideoPublishRequest).where(VideoPublishRequest.id == request_id)
        )
        return result.scalar_one_or_none()

    async def list_transcript_feedback(
        self,
        status: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list, int]:
        base = (
            select(TranscriptFeedback, Video, User, Transcript)
            .join(Video, TranscriptFeedback.video_id == Video.id)
            .join(User, TranscriptFeedback.user_id == User.id)
            .outerjoin(Transcript, TranscriptFeedback.transcript_id == Transcript.id)
        )
        count_q = select(func.count()).select_from(TranscriptFeedback)
        if status:
            base = base.where(TranscriptFeedback.status == status)
            count_q = count_q.where(TranscriptFeedback.status == status)
        total = (await self.db.execute(count_q)).scalar() or 0
        rows = (
            await self.db.execute(
                base.order_by(TranscriptFeedback.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return rows, total

    async def get_transcript_feedback(self, feedback_id: str) -> TranscriptFeedback | None:
        result = await self.db.execute(
            select(TranscriptFeedback).where(TranscriptFeedback.id == feedback_id)
        )
        return result.scalar_one_or_none()

    async def get_transcription_status(self, video_id: str):
        result = await self.db.execute(
            select(Video.transcription_status, Video.transcription_error).where(
                Video.id == video_id
            )
        )
        return result.one_or_none()

    async def get_transcripts_ordered(self, video_id: str) -> list[Transcript]:
        result = await self.db.execute(
            select(Transcript).where(Transcript.video_id == video_id).order_by(Transcript.index)
        )
        return list(result.scalars().all())

    async def get_transcripts_by_ids(self, ids: list[str]) -> list[Transcript]:
        result = await self.db.execute(select(Transcript).where(Transcript.id.in_(ids)))
        return list(result.scalars().all())

    async def has_in_progress_attempt(self, user_id: str, video_id: str) -> bool:
        result = await self.db.execute(
            select(DictationAttempt.id)
            .where(
                DictationAttempt.user_id == user_id,
                DictationAttempt.video_id == video_id,
                DictationAttempt.status == "in_progress",
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    # ── Mutations ───────────────────────────────────────────────────────────

    async def delete_transcripts_for_video(self, video_id: str) -> None:
        await self.db.execute(delete(Transcript).where(Transcript.video_id == video_id))

    async def create_user_practice(self, user_id: str, video_id: str) -> UserPracticeVideo:
        practice = UserPracticeVideo(user_id=user_id, video_id=video_id)
        self.add(practice)
        await self.flush()
        return practice

    async def delete_user_practice(self, practice: UserPracticeVideo) -> None:
        await self.db.execute(
            delete(UserPracticeVideoTag).where(
                UserPracticeVideoTag.practice_video_id == practice.id
            )
        )
        await self.db.delete(practice)

    async def set_user_practice_tags(self, practice_video_id: str, tag_ids: list[str]) -> None:
        await self.db.execute(
            delete(UserPracticeVideoTag).where(
                UserPracticeVideoTag.practice_video_id == practice_video_id
            )
        )
        for tag_id in dict.fromkeys(tag_ids):
            self.add(
                UserPracticeVideoTag(
                    practice_video_id=practice_video_id,
                    tag_id=tag_id,
                )
            )

    async def set_video_public_tags(self, video_id: str, tag_ids: list[str]) -> None:
        await self.db.execute(delete(VideoTopicTag).where(VideoTopicTag.video_id == video_id))
        for tag_id in dict.fromkeys(tag_ids):
            self.add(VideoTopicTag(video_id=video_id, tag_id=tag_id))

    async def resolve_pending_publish_requests(
        self, video_id: str, admin_id: str, admin_note: str | None = None
    ) -> None:
        rows = await self.db.execute(
            select(VideoPublishRequest).where(
                VideoPublishRequest.video_id == video_id,
                VideoPublishRequest.status == REQUEST_STATUS_PENDING,
            )
        )
        now = datetime.now(UTC)
        for request in rows.scalars().all():
            request.status = REQUEST_STATUS_RESOLVED
            request.reviewed_by = admin_id
            request.reviewed_at = now
            request.admin_note = admin_note or "Resolved by admin publish"

    async def create_publish_request(
        self, user_id: str, video_id: str, message: str | None
    ) -> VideoPublishRequest:
        request = VideoPublishRequest(user_id=user_id, video_id=video_id, message=message)
        self.add(request)
        await self.flush()
        return request

    async def create_transcript_feedback(
        self,
        user_id: str,
        video_id: str,
        transcript_id: str | None,
        message: str,
        suggested_text: str | None,
    ) -> TranscriptFeedback:
        feedback = TranscriptFeedback(
            user_id=user_id,
            video_id=video_id,
            transcript_id=transcript_id,
            message=message,
            suggested_text=suggested_text,
        )
        self.add(feedback)
        await self.flush()
        return feedback

    async def delete_video_and_related(self, video: Video) -> None:
        """Delete a video plus its dictation attempts/sentences and room sessions."""
        attempts = await self.db.execute(
            select(DictationAttempt).where(DictationAttempt.video_id == video.id)
        )
        for attempt in attempts.scalars().all():
            await self.db.execute(
                delete(DictationSentence).where(DictationSentence.attempt_id == attempt.id)
            )
        await self.db.execute(delete(DictationAttempt).where(DictationAttempt.video_id == video.id))
        await self.db.execute(delete(RoomSession).where(RoomSession.video_id == video.id))
        await self.db.delete(video)

    async def delete(self, instance) -> None:
        await self.db.delete(instance)

    # ── Transaction helpers ─────────────────────────────────────────────────

    def add(self, instance) -> None:
        self.db.add(instance)

    async def flush(self) -> None:
        await self.db.flush()

    async def commit(self) -> None:
        await self.db.commit()

    async def rollback(self) -> None:
        await self.db.rollback()

    async def refresh(self, instance) -> None:
        await self.db.refresh(instance)
