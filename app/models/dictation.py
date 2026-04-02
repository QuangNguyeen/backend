import uuid
from datetime import datetime

from sqlalchemy import String, Integer, Float, Text, DateTime, ForeignKey, Index, JSON, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class DictationAttempt(Base):
    """Renamed from DictationSession to match SRS 7.2.5 'dictation_attempts' table."""
    __tablename__ = "dictation_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    video_id: Mapped[str] = mapped_column(String(36), ForeignKey("videos.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="in_progress")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_sentences: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_sentence_index: Mapped[int] = mapped_column(Integer, default=0)
    total_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    correct_words: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sentences: Mapped[list["DictationSentence"]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_attempts_user_completed", "user_id", "status"),
        Index("idx_attempts_user_in_progress", "user_id", "current_sentence_index"),
    )


class DictationSentence(Base):
    """Renamed from SentenceResult to match SRS 7.2.6 'dictation_sentences' table."""
    __tablename__ = "dictation_sentences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    attempt_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("dictation_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sentence_index: Mapped[int] = mapped_column(Integer, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    user_input: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    word_diff: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    hints_used: Mapped[int] = mapped_column(Integer, default=0)
    replay_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    attempt: Mapped["DictationAttempt"] = relationship(back_populates="sentences")
