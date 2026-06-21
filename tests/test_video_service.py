"""Unit tests for VideoService permission/lookup logic (no external providers)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.models.user import User
from app.models.video import (
    TopicTag,
    Transcript,
    TranscriptFeedback,
    UserPracticeVideo,
    Video,
    VideoPublishRequest,
)
from app.schemas.video import (
    ImportPartialResponse,
    ImportVideoRequest,
    TranscriptBulkUpdateRequest,
    TranscriptFeedbackCreate,
    TranscriptUpdateItem,
    VideoTopicTagsUpdate,
)
from app.services.video_service import VideoService
from app.services.youtube_service import TranscriptResult, TranscriptSegment


class FakeVideoRepository:
    def __init__(self):
        self.video = None
        self.by_youtube_id = None
        self.status_row = None
        self.transcripts_by_id: dict[str, Transcript] = {}
        self.in_progress = False
        self.deleted_related = False
        self.committed = 0
        self.refreshed = 0
        self.rolled_back = 0
        self.added = []
        self.user_practices: dict[tuple[str, str], UserPracticeVideo] = {}
        self.user_tags: dict[str, list[str]] = {}
        self.public_tags: dict[str, list[str]] = {}
        self.active_tags: dict[str, TopicTag] = {}
        self.publish_requests: list[VideoPublishRequest] = []
        self.transcript_feedback: list[TranscriptFeedback] = []
        self.transcript_feedback_rows = []
        self.recommendation_candidates = []
        self.recent_completed_attempts = []
        self.unattempted_practice_videos = []
        self.unattempted_practice_tags = []
        self.public_tag_rows = []

    async def get_by_id(self, video_id):
        return self.video

    async def get_by_youtube_id(self, youtube_id):
        return self.by_youtube_id

    async def get_transcription_status(self, video_id):
        return self.status_row

    async def get_user_practice(self, user_id, video_id):
        return self.user_practices.get((user_id, video_id))

    async def get_active_topic_tags_by_ids(self, tag_ids):
        return [self.active_tags[tag_id] for tag_id in tag_ids if tag_id in self.active_tags]

    async def get_video_public_tags(self, video_id):
        return [self.active_tags[tag_id] for tag_id in self.public_tags.get(video_id, [])]

    async def get_user_practice_tags(self, user_id, video_id):
        practice = self.user_practices.get((user_id, video_id))
        if not practice:
            return []
        return [self.active_tags[tag_id] for tag_id in self.user_tags.get(practice.id, [])]

    async def get_similar_importers(self, video_id, exclude_user_id=None, limit=10):
        return [], 0

    async def list_recommendation_candidates(self, user_id, limit=500):
        return self.recommendation_candidates[:limit]

    async def list_recent_completed_attempts(self, user_id, limit=20):
        return self.recent_completed_attempts[:limit]

    async def list_unattempted_practice_videos(self, user_id, limit=500):
        return self.unattempted_practice_videos[:limit]

    async def list_unattempted_practice_tags(self, user_id):
        return self.unattempted_practice_tags

    async def list_public_tags_for_videos(self, video_ids):
        wanted = set(video_ids)
        return [row for row in self.public_tag_rows if row[0] in wanted]

    async def get_transcripts_by_ids(self, ids):
        return [self.transcripts_by_id[i] for i in ids if i in self.transcripts_by_id]

    async def get_transcript_by_id(self, transcript_id):
        return self.transcripts_by_id.get(transcript_id)

    async def get_transcripts_ordered(self, video_id):
        return sorted(
            [row for row in self.transcripts_by_id.values() if row.video_id == video_id],
            key=lambda row: row.index,
        )

    async def has_in_progress_attempt(self, user_id, video_id):
        return self.in_progress

    async def delete(self, instance):
        self.transcripts_by_id.pop(instance.id, None)

    async def delete_video_and_related(self, video):
        self.deleted_related = True

    async def create_user_practice(self, user_id, video_id):
        practice = UserPracticeVideo(
            id=f"upv-{len(self.user_practices) + 1}", user_id=user_id, video_id=video_id
        )
        self.user_practices[(user_id, video_id)] = practice
        return practice

    async def set_user_practice_tags(self, practice_video_id, tag_ids):
        self.user_tags[practice_video_id] = list(dict.fromkeys(tag_ids))

    async def set_video_public_tags(self, video_id, tag_ids):
        self.public_tags[video_id] = list(dict.fromkeys(tag_ids))

    async def resolve_pending_publish_requests(self, video_id, admin_id, admin_note=None):
        return None

    async def resolve_user_pending_publish_requests(self, user_id, video_id, note):
        resolved = 0
        for request in self.publish_requests:
            if (
                request.user_id == user_id
                and request.video_id == video_id
                and request.status == "pending"
            ):
                request.status = "resolved"
                request.admin_note = note
                resolved += 1
        return resolved

    async def count_pending_publish_requests(self, video_id):
        return sum(
            1
            for request in self.publish_requests
            if request.video_id == video_id and request.status == "pending"
        )

    async def create_transcript_feedback(
        self, user_id, video_id, transcript_id, message, suggested_text
    ):
        now = datetime.now(UTC)
        feedback = TranscriptFeedback(
            id=f"feedback-{len(self.transcript_feedback) + 1}",
            user_id=user_id,
            video_id=video_id,
            transcript_id=transcript_id,
            message=message,
            suggested_text=suggested_text,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        self.transcript_feedback.append(feedback)
        return feedback

    async def list_transcript_feedback(self, status, page, page_size):
        rows = self.transcript_feedback_rows
        if status:
            rows = [row for row in rows if row[0].status == status]
        return rows[(page - 1) * page_size : page * page_size], len(rows)

    async def delete_user_practice(self, practice):
        self.user_practices.pop((practice.user_id, practice.video_id), None)
        self.user_tags.pop(practice.id, None)

    def add(self, instance):
        self.added.append(instance)
        if isinstance(instance, Video):
            self.video = instance

    async def flush(self):
        if self.video is not None and self.video.id is None:
            self.video.id = "v1"

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1

    async def refresh(self, instance):
        self.refreshed += 1


def _user(is_admin=False):
    return User(
        id="u1",
        email="u@e.com",
        display_name="U",
        preferred_language="en",
        is_admin=is_admin,
    )


def _segment(text="Hello world.", start=0.0, duration=2.0):
    return TranscriptSegment(text=text, start=start, duration=duration)


def _transcript(
    transcript_id: str,
    *,
    video_id: str = "v1",
    index: int = 0,
    text: str = "Hello.",
    start_time: float = 0.0,
    end_time: float = 1.0,
) -> Transcript:
    return Transcript(
        id=transcript_id,
        video_id=video_id,
        index=index,
        text=text,
        start_time=start_time,
        end_time=end_time,
        language="en",
    )


def _video(
    video_id: str,
    *,
    channel: str = "Channel",
    language: str = "en",
    level: str | None = "B1",
    curated: bool = False,
    published_at: datetime | None = None,
) -> Video:
    now = published_at or datetime.now(UTC)
    return Video(
        id=video_id,
        youtube_id=f"yt-{video_id}",
        title=f"Video {video_id}",
        channel=channel,
        duration=120,
        language=language,
        level=level,
        is_curated=curated,
        is_active=True,
        publish_status="published",
        published_at=published_at,
        is_auto_generated=False,
        transcription_status="ready",
        thumbnail_url=f"https://example.com/{video_id}.jpg",
        created_at=now,
    )


class FakeTaskRunner:
    def __init__(self):
        self.calls = []

    def delay(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id="task-1")


def _patch_import_common(monkeypatch, *, transcript_result=None, duration=120):
    monkeypatch.setattr(
        "app.services.video_service.youtube_service.get_video_metadata",
        lambda video_id: {
            "title": "Video Title",
            "channel": "Channel Name",
            "duration": duration,
            "thumbnail_url": "https://img.youtube.com/vi/abcdefghijk/hqdefault.jpg",
        },
    )
    if transcript_result is not None:
        monkeypatch.setattr(
            "app.services.video_service.youtube_service.get_transcript",
            lambda video_id, languages=None: transcript_result,
        )
    monkeypatch.setattr(
        "app.services.video_service.youtube_service.get_transcript_ytdlp",
        lambda video_id, languages=None: transcript_result,
    )
    monkeypatch.setattr(
        "app.services.video_service.calculate_audio_difficulty",
        lambda *args, **kwargs: {"level": "A1", "factors": {}, "label": "Easy"},
    )
    monkeypatch.setattr(
        "app.services.video_service.difficulty_update_values",
        lambda difficulty: {"difficulty_score": 1.0},
    )


@pytest.mark.asyncio
async def test_get_video_not_found_raises():
    service = VideoService(FakeVideoRepository())
    with pytest.raises(NotFoundError):
        await service.get_video(None, "missing")


@pytest.mark.asyncio
async def test_transcription_status_not_found_raises():
    service = VideoService(FakeVideoRepository())
    with pytest.raises(NotFoundError):
        await service.get_transcription_status(None, "missing")


@pytest.mark.asyncio
async def test_transcription_status_returns_row():
    repo = FakeVideoRepository()
    repo.status_row = ("ready", None)
    repo.video = Video(id="v1", publish_status="published")
    service = VideoService(repo)
    assert await service.get_transcription_status(None, "v1") == {"status": "ready", "error": None}


@pytest.mark.asyncio
async def test_delete_video_rejects_non_owner():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", created_by="someone-else")
    service = VideoService(repo)
    with pytest.raises(ForbiddenError):
        await service.delete_video(_user(), "v1")
    assert repo.deleted_related is False


@pytest.mark.asyncio
async def test_delete_video_rejects_owner_non_admin():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", created_by="u1")
    service = VideoService(repo)
    with pytest.raises(ForbiddenError):
        await service.delete_video(_user(), "v1")
    assert repo.deleted_related is False


@pytest.mark.asyncio
async def test_delete_video_allows_admin_non_owner():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", created_by="someone-else")
    service = VideoService(repo)
    await service.delete_video(_user(is_admin=True), "v1")
    assert repo.deleted_related is True


@pytest.mark.asyncio
async def test_bulk_update_rejects_non_owner():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", created_by="someone-else")
    service = VideoService(repo)
    body = TranscriptBulkUpdateRequest(items=[TranscriptUpdateItem(transcript_id="t1", text="x")])
    with pytest.raises(ForbiddenError):
        await service.bulk_update_transcripts(_user(), "v1", body)


@pytest.mark.asyncio
async def test_bulk_update_unknown_transcript_raises():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", created_by="u1")
    service = VideoService(repo)
    body = TranscriptBulkUpdateRequest(
        items=[TranscriptUpdateItem(transcript_id="ghost", text="x")]
    )
    with pytest.raises(NotFoundError):
        await service.bulk_update_transcripts(_user(is_admin=True), "v1", body)


@pytest.mark.asyncio
async def test_bulk_update_allows_timestamp_edits():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", created_by="u1")
    row = _transcript("t1", text="Old text.", start_time=0.0, end_time=2.0)
    repo.transcripts_by_id[row.id] = row
    service = VideoService(repo)

    response = await service.bulk_update_transcripts(
        _user(is_admin=True),
        "v1",
        TranscriptBulkUpdateRequest(
            items=[
                TranscriptUpdateItem(
                    transcript_id="t1",
                    text="New text.",
                    start_time=0.25,
                    end_time=2.25,
                )
            ]
        ),
    )

    assert response.updated == 1
    assert row.text == "New text."
    assert row.start_time == 0.25
    assert row.end_time == 2.25
    assert repo.committed == 1


@pytest.mark.asyncio
async def test_bulk_update_rejects_invalid_timestamp_range():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", created_by="u1")
    row = _transcript("t1", start_time=0.0, end_time=2.0)
    repo.transcripts_by_id[row.id] = row
    service = VideoService(repo)

    with pytest.raises(BadRequestError):
        await service.bulk_update_transcripts(
            _user(is_admin=True),
            "v1",
            TranscriptBulkUpdateRequest(
                items=[TranscriptUpdateItem(transcript_id="t1", start_time=2.0, end_time=1.0)]
            ),
        )

    assert row.start_time == 0.0
    assert row.end_time == 2.0
    assert repo.committed == 0


@pytest.mark.asyncio
async def test_bulk_update_rejects_overlapping_timestamps():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", created_by="u1")
    first = _transcript("t1", index=0, start_time=0.0, end_time=2.0)
    second = _transcript("t2", index=1, start_time=2.1, end_time=4.0)
    repo.transcripts_by_id[first.id] = first
    repo.transcripts_by_id[second.id] = second
    service = VideoService(repo)

    with pytest.raises(BadRequestError):
        await service.bulk_update_transcripts(
            _user(is_admin=True),
            "v1",
            TranscriptBulkUpdateRequest(
                items=[TranscriptUpdateItem(transcript_id="t2", start_time=1.5)]
            ),
        )

    assert second.start_time == 2.1
    assert repo.committed == 0


@pytest.mark.asyncio
async def test_bulk_update_rejects_duplicate_transcript_ids():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", created_by="u1")
    row = _transcript("t1")
    repo.transcripts_by_id[row.id] = row
    service = VideoService(repo)

    with pytest.raises(BadRequestError):
        await service.bulk_update_transcripts(
            _user(is_admin=True),
            "v1",
            TranscriptBulkUpdateRequest(
                items=[
                    TranscriptUpdateItem(transcript_id="t1", text="A"),
                    TranscriptUpdateItem(transcript_id="t1", text="B"),
                ]
            ),
        )


@pytest.mark.asyncio
async def test_import_manual_captions_does_not_dispatch_stt(monkeypatch):
    repo = FakeVideoRepository()
    task_runner = FakeTaskRunner()
    _patch_import_common(
        monkeypatch,
        transcript_result=TranscriptResult(segments=[_segment()], is_generated=False),
    )
    monkeypatch.setattr("app.services.video_service.run_stt_pipeline", task_runner)

    result = await VideoService(repo).import_video(
        _user(),
        ImportVideoRequest(youtube_url="abcdefghijk"),
    )

    assert result.already_exists is False
    assert result.video.transcription_status == "ready"
    assert result.video.publish_status == "private"
    assert result.video.is_auto_generated is False
    assert task_runner.calls == []
    assert any(isinstance(item, Transcript) for item in repo.added)


@pytest.mark.asyncio
async def test_import_auto_captions_dispatches_stt_without_preanalysis(monkeypatch):
    repo = FakeVideoRepository()
    task_runner = FakeTaskRunner()
    _patch_import_common(
        monkeypatch,
        transcript_result=TranscriptResult(
            segments=[_segment("Auto generated caption text.", 0.0, 3.0)],
            is_generated=True,
        ),
    )

    async def fail_punctuation(*args, **kwargs):
        raise AssertionError("punctuation should not run before STT")

    monkeypatch.setattr("app.services.video_service.punctuate_transcript", fail_punctuation)
    monkeypatch.setattr(
        "app.services.video_service.calculate_audio_difficulty",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("difficulty should not run before STT")
        ),
    )
    monkeypatch.setattr("app.services.video_service.run_stt_pipeline", task_runner)

    result = await VideoService(repo).import_video(
        _user(),
        ImportVideoRequest(youtube_url="abcdefghijk"),
    )

    assert result.video.transcription_status == "pending"
    assert result.video.is_auto_generated is True
    assert len(task_runner.calls) == 1
    assert task_runner.calls[0]["title"] == "Video Title"
    assert "Auto generated caption text" in task_runner.calls[0]["prompt_context"]


@pytest.mark.asyncio
async def test_import_no_captions_dispatches_stt(monkeypatch):
    repo = FakeVideoRepository()
    task_runner = FakeTaskRunner()
    _patch_import_common(monkeypatch, transcript_result=None, duration=120)
    monkeypatch.setattr(
        "app.services.video_service.youtube_service.get_transcript",
        lambda video_id, languages=None: (_ for _ in ()).throw(NotFoundError("none")),
    )
    monkeypatch.setattr(
        "app.services.video_service.youtube_service.get_transcript_ytdlp",
        lambda video_id, languages=None: (_ for _ in ()).throw(RuntimeError("none")),
    )
    monkeypatch.setattr("app.services.video_service.run_stt_pipeline", task_runner)

    result = await VideoService(repo).import_video(
        _user(),
        ImportVideoRequest(youtube_url="abcdefghijk"),
    )

    assert result.video.transcription_status == "pending"
    assert result.video.is_auto_generated is False
    assert len(task_runner.calls) == 1
    assert task_runner.calls[0]["prompt_context"] is None


@pytest.mark.asyncio
async def test_import_auto_captions_too_long_returns_partial(monkeypatch):
    repo = FakeVideoRepository()
    task_runner = FakeTaskRunner()
    _patch_import_common(
        monkeypatch,
        transcript_result=TranscriptResult(segments=[_segment()], is_generated=True),
        duration=601,
    )
    monkeypatch.setattr("app.services.video_service.run_stt_pipeline", task_runner)

    result = await VideoService(repo).import_video(
        _user(),
        ImportVideoRequest(youtube_url="abcdefghijk"),
    )

    assert isinstance(result, ImportPartialResponse)
    assert "AI transcription is limited" in result.message
    assert task_runner.calls == []
    assert repo.added == []


@pytest.mark.asyncio
async def test_import_existing_video_adds_to_my_practice_without_refetch(monkeypatch):
    repo = FakeVideoRepository()
    repo.by_youtube_id = Video(
        id="v-existing",
        youtube_id="abcdefghijk",
        title="Existing",
        channel="Channel",
        duration=120,
        language="en",
        is_curated=False,
        is_active=True,
        publish_status="private",
        is_auto_generated=False,
        transcription_status="ready",
        thumbnail_url="thumb",
    )
    monkeypatch.setattr(
        "app.services.video_service.youtube_service.get_video_metadata",
        lambda video_id: (_ for _ in ()).throw(AssertionError("metadata should not refetch")),
    )

    result = await VideoService(repo).import_video(
        _user(),
        ImportVideoRequest(youtube_url="abcdefghijk"),
    )

    assert result.already_exists is True
    assert result.already_in_my_practice is False
    assert result.video.id == "v-existing"
    assert ("u1", "v-existing") in repo.user_practices
    assert not any(isinstance(item, Transcript) for item in repo.added)


@pytest.mark.asyncio
async def test_admin_import_existing_unpublished_video_publishes_it():
    repo = FakeVideoRepository()
    repo.by_youtube_id = Video(
        id="v-existing",
        youtube_id="abcdefghijk",
        title="Existing",
        channel="Channel",
        duration=120,
        language="en",
        is_curated=False,
        is_active=True,
        publish_status="private",
        is_auto_generated=False,
        transcription_status="ready",
        thumbnail_url="thumb",
    )

    result = await VideoService(repo).import_video(
        _user(is_admin=True),
        ImportVideoRequest(youtube_url="abcdefghijk"),
    )

    assert result.already_exists is True
    assert result.video.publish_status == "published"
    assert repo.by_youtube_id.reviewed_by == "u1"


@pytest.mark.asyncio
async def test_import_rejects_inactive_or_unknown_topic_tags():
    repo = FakeVideoRepository()

    with pytest.raises(BadRequestError):
        await VideoService(repo).import_video(
            _user(),
            ImportVideoRequest(youtube_url="abcdefghijk", topic_tag_ids=["missing-tag"]),
        )


@pytest.mark.asyncio
async def test_admin_updates_video_public_tags():
    repo = FakeVideoRepository()
    repo.video = Video(
        id="v1",
        youtube_id="abcdefghijk",
        title="Existing",
        channel="Channel",
        duration=120,
        language="en",
        is_curated=False,
        is_active=True,
        publish_status="published",
        is_auto_generated=False,
        transcription_status="ready",
        thumbnail_url="thumb",
    )
    repo.active_tags["tag-1"] = TopicTag(
        id="tag-1",
        slug="business",
        name="Business",
        is_active=True,
        sort_order=1,
    )

    result = await VideoService(repo).update_video_public_tags(
        _user(is_admin=True),
        "v1",
        VideoTopicTagsUpdate(topic_tag_ids=["tag-1"]),
    )

    assert [tag.slug for tag in result.topic_tags] == ["business"]
    assert repo.public_tags["v1"] == ["tag-1"]


@pytest.mark.asyncio
async def test_user_removes_video_from_my_practice_without_deleting_shared_video():
    repo = FakeVideoRepository()
    repo.video = Video(
        id="v1",
        youtube_id="abcdefghijk",
        title="Existing",
        channel="Channel",
        duration=120,
        language="en",
        is_curated=False,
        is_active=True,
        publish_status="published",
        is_auto_generated=False,
        transcription_status="ready",
        thumbnail_url="thumb",
    )
    practice = UserPracticeVideo(id="upv-1", user_id="u1", video_id="v1")
    repo.user_practices[("u1", "v1")] = practice
    repo.user_tags[practice.id] = ["tag-1"]

    await VideoService(repo).remove_from_my_practice(_user(), "v1")

    assert ("u1", "v1") not in repo.user_practices
    assert practice.id not in repo.user_tags
    assert repo.deleted_related is False
    assert repo.video is not None
    assert repo.committed == 1


@pytest.mark.asyncio
async def test_remove_from_my_practice_resolves_pending_publish_request():
    repo = FakeVideoRepository()
    repo.video = Video(
        id="v1",
        youtube_id="abcdefghijk",
        title="Existing",
        channel="Channel",
        duration=120,
        language="en",
        is_curated=False,
        is_active=True,
        publish_status="pending_review",
        is_auto_generated=False,
        transcription_status="ready",
        thumbnail_url="thumb",
    )
    repo.user_practices[("u1", "v1")] = UserPracticeVideo(id="upv-1", user_id="u1", video_id="v1")
    request = VideoPublishRequest(
        id="req-1",
        user_id="u1",
        video_id="v1",
        status="pending",
        message="Please review",
    )
    repo.publish_requests.append(request)

    await VideoService(repo).remove_from_my_practice(_user(), "v1")

    assert request.status == "resolved"
    assert request.admin_note == "Requester removed the video from My Practice"
    assert repo.video.publish_status == "private"


@pytest.mark.asyncio
async def test_user_can_send_feedback_for_published_video_outside_my_practice():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", publish_status="published")

    result = await VideoService(repo).create_transcript_feedback(
        _user(),
        "v1",
        TranscriptFeedbackCreate(message="The transcript has an incorrect word."),
    )

    assert result.video_id == "v1"
    assert result.user_id == "u1"
    assert result.status == "pending"
    assert len(repo.transcript_feedback) == 1
    assert repo.committed == 1


@pytest.mark.asyncio
async def test_user_can_send_feedback_for_private_video_in_my_practice():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", publish_status="private")
    repo.user_practices[("u1", "v1")] = UserPracticeVideo(
        id="upv-1",
        user_id="u1",
        video_id="v1",
    )

    result = await VideoService(repo).create_transcript_feedback(
        _user(),
        "v1",
        TranscriptFeedbackCreate(message="Please check this transcript."),
    )

    assert result.video_id == "v1"
    assert result.status == "pending"
    assert len(repo.transcript_feedback) == 1


@pytest.mark.asyncio
async def test_user_cannot_send_feedback_for_inaccessible_private_video():
    repo = FakeVideoRepository()
    repo.video = Video(id="v1", publish_status="private")

    with pytest.raises(
        ForbiddenError,
        match="public videos or private videos in My Practice",
    ):
        await VideoService(repo).create_transcript_feedback(
            _user(),
            "v1",
            TranscriptFeedbackCreate(message="Please check this transcript."),
        )

    assert repo.transcript_feedback == []


@pytest.mark.asyncio
async def test_admin_feedback_queue_returns_flat_frontend_contract():
    repo = FakeVideoRepository()
    now = datetime.now(UTC)
    feedback = TranscriptFeedback(
        id="feedback-1",
        user_id="u1",
        video_id="v1",
        transcript_id="t1",
        message="The final word is incorrect.",
        suggested_text="world",
        status="pending",
        created_at=now,
        updated_at=now,
    )
    video = Video(id="v1", title="A real video title", publish_status="published")
    requester = _user()
    transcript = _transcript("t1", text="Hello word.")
    repo.transcript_feedback_rows = [(feedback, video, requester, transcript)]

    result = await VideoService(repo).list_transcript_feedback("pending", 1, 20)

    assert result.total == 1
    assert result.total_pages == 1
    assert result.items[0].video_title == "A real video title"
    assert result.items[0].user_name == "U"
    assert result.items[0].user_email == "u@e.com"
    assert result.items[0].message == "The final word is incorrect."
    assert result.items[0].transcript_text == "Hello word."


@pytest.mark.asyncio
async def test_recommendations_rank_matching_topic_channel_and_progression_first():
    repo = FakeVideoRepository()
    business = TopicTag(
        id="tag-business",
        slug="business",
        name="Business",
        is_active=True,
        sort_order=1,
    )
    practiced_video = _video("practiced", channel=" BBC Learning English ", level="B1")
    attempt = SimpleNamespace(score=0.92)
    repo.recent_completed_attempts = [(attempt, practiced_video)]

    matching = _video("matching", channel="bbc learning english", level="B2")
    unrelated = _video("unrelated", channel="Other Channel", level="A1")
    repo.recommendation_candidates = [(unrelated, 20, 0.9), (matching, 1, 0.8)]
    repo.public_tag_rows = [
        (practiced_video.id, business),
        (matching.id, business),
    ]

    result = await VideoService(repo).get_recommendations(_user(), limit=2)

    assert result.strategy == "personalized"
    assert [item.video.id for item in result.items] == ["matching", "unrelated"]
    assert result.items[0].reason_code == "topic_match"
    assert result.items[0].reason_text == "Matches your Business practice"


@pytest.mark.asyncio
async def test_recommendations_use_cold_start_ranking_without_behavior():
    repo = FakeVideoRepository()
    english = _video("english", language="en", published_at=datetime.now(UTC))
    japanese = _video("japanese", language="ja", curated=True)
    repo.recommendation_candidates = [(japanese, 0, None), (english, 0, None)]

    result = await VideoService(repo).get_recommendations(_user(), limit=2)

    assert result.strategy == "cold_start"
    assert result.items[0].video.id == "english"
    assert result.items[0].reason_code == "preferred_language"


@pytest.mark.asyncio
async def test_recommendations_limit_each_channel_to_two_items():
    repo = FakeVideoRepository()
    repo.unattempted_practice_videos = [_video("signal", channel="Preferred Channel")]
    repo.recommendation_candidates = [
        (_video("same-1", channel="Preferred Channel"), 5, None),
        (_video("same-2", channel=" preferred channel "), 4, None),
        (_video("same-3", channel="PREFERRED CHANNEL"), 3, None),
        (_video("other", channel="Other Channel"), 0, None),
    ]

    result = await VideoService(repo).get_recommendations(_user(), limit=4)

    ids = [item.video.id for item in result.items]
    assert ids[:2] == ["same-1", "same-2"]
    assert "same-3" not in ids
    assert "other" in ids
    assert len(result.items) == 3
