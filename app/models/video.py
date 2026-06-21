import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    youtube_id: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    channel: Mapped[str] = mapped_column(String(200), default="")
    duration: Mapped[int] = mapped_column(Integer, default=0)  # seconds
    language: Mapped[str] = mapped_column(String(2), nullable=False, default="en")
    level: Mapped[str | None] = mapped_column(String(5), nullable=True)
    is_curated: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    publish_status: Mapped[str] = mapped_column(
        String(20), default="private", nullable=False, server_default="private"
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_auto_generated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    transcription_status: Mapped[str] = mapped_column(
        String(20), default="ready", nullable=False, server_default="ready"
    )
    transcription_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    difficulty_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    difficulty_level: Mapped[str | None] = mapped_column(String(5), nullable=True)
    difficulty_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    difficulty_factors: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    difficulty_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    avg_words_per_segment: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_words_per_segment: Mapped[int | None] = mapped_column(Integer, nullable=True)
    words_per_minute: Mapped[float | None] = mapped_column(Float, nullable=True)
    segment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_word_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_url: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    transcripts: Mapped[list["Transcript"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )


class TopicTag(Base):
    __tablename__ = "topic_tags"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserPracticeVideo(Base):
    __tablename__ = "user_practice_videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "video_id", name="uq_user_practice_video"),
        Index("idx_user_practice_user_created", "user_id", "created_at"),
    )


class UserPracticeVideoTag(Base):
    __tablename__ = "user_practice_video_tags"

    practice_video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("user_practice_videos.id", ondelete="CASCADE"), nullable=False
    )
    tag_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("topic_tags.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        PrimaryKeyConstraint("practice_video_id", "tag_id", name="pk_user_practice_video_tags"),
    )


class VideoTopicTag(Base):
    __tablename__ = "video_topic_tags"

    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    tag_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("topic_tags.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (PrimaryKeyConstraint("video_id", "tag_id", name="pk_video_topic_tags"),)


class VideoPublishRequest(Base):
    __tablename__ = "video_publish_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TranscriptFeedback(Base):
    __tablename__ = "transcript_feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transcript_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("transcripts.id", ondelete="SET NULL"), nullable=True
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Transcript(Base):
    """Renamed from Sentence to match SRS 7.2.4 'transcripts' table.

    SRS defines segments as JSONB, but current implementation uses row-per-segment
    which is already working. Keeping this approach with the correct table/class name.
    """

    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    video_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False
    )
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)  # seconds
    end_time: Mapped[float] = mapped_column(Float, nullable=False)

    video: Mapped["Video"] = relationship(back_populates="transcripts")

    __table_args__ = (Index("idx_transcripts_video_language", "video_id", "language"),)
