import uuid
from datetime import datetime

from sqlalchemy import String, Integer, Boolean, DateTime, Float, Text, ForeignKey, Index, func
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
    thumbnail_url: Mapped[str] = mapped_column(String(500), default="")
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    transcripts: Mapped[list["Transcript"]] = relationship(back_populates="video", cascade="all, delete-orphan")


class Transcript(Base):
    """Renamed from Sentence to match SRS 7.2.4 'transcripts' table.
    
    SRS defines segments as JSONB, but current implementation uses row-per-segment
    which is already working. Keeping this approach with the correct table/class name.
    """
    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    video_id: Mapped[str] = mapped_column(String(36), ForeignKey("videos.id", ondelete="CASCADE"), nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en")
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)  # seconds
    end_time: Mapped[float] = mapped_column(Float, nullable=False)

    video: Mapped["Video"] = relationship(back_populates="transcripts")

    __table_args__ = (
        Index("idx_transcripts_video_language", "video_id", "language"),
    )
