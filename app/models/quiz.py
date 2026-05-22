import uuid
from datetime import datetime

from sqlalchemy import String, Integer, Float, DateTime, ForeignKey, JSON, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class QuizSession(Base):
    __tablename__ = "quiz_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    video_id: Mapped[str] = mapped_column(String(36), ForeignKey("videos.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="in_progress")
    total_questions: Mapped[int] = mapped_column(Integer, default=0)
    correct_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    questions: Mapped[list | None] = mapped_column(JSON, nullable=True)
    answers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())