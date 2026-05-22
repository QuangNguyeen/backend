import uuid
from datetime import datetime

from sqlalchemy import String, Integer, Float, Boolean, Text, DateTime, ForeignKey, UniqueConstraint, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RoomSession(Base):
    __tablename__ = "room_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    room_code: Mapped[str] = mapped_column(String(6), unique=True, nullable=False, index=True)
    host_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    video_id: Mapped[str] = mapped_column(String(36), ForeignKey("videos.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="waiting")
    max_players: Mapped[int] = mapped_column(Integer, default=10)
    max_replays: Mapped[int] = mapped_column(Integer, default=3)
    exam_duration_minutes: Mapped[int] = mapped_column(Integer, default=30)
    total_sentences: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RoomMember(Base):
    __tablename__ = "room_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    room_id: Mapped[str] = mapped_column(String(36), ForeignKey("room_sessions.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    total_score: Mapped[float] = mapped_column(Float, default=0.0)
    sentences_done: Mapped[int] = mapped_column(Integer, default=0)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    is_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("room_id", "user_id", name="uq_room_member"),
        Index("idx_room_members_room", "room_id"),
    )


class RoomAnswer(Base):
    __tablename__ = "room_answers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    room_id: Mapped[str] = mapped_column(String(36), ForeignKey("room_sessions.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    sentence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    user_input: Mapped[str] = mapped_column(Text, default="")
    is_skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    accuracy_score: Mapped[float] = mapped_column(Float, default=0.0)
    time_bonus: Mapped[float] = mapped_column(Float, default=0.0)
    final_score: Mapped[float] = mapped_column(Float, default=0.0)
    replay_count: Mapped[int] = mapped_column(Integer, default=0)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("room_id", "user_id", "sentence_index", name="uq_room_answer"),
        Index("idx_room_answers_room_sentence", "room_id", "sentence_index"),
    )
