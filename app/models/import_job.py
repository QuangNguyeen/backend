import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ImportJob(Base):
    """Tracks the state and progress of a vocabulary import enrichment job.

    The API creates the row when an import requests enrichment; the Celery
    worker writes incremental progress (``enriched_count`` + ``phase``) that the
    status endpoint polls.
    """

    __tablename__ = "import_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # "pending" | "processing" | "completed" | "failed"
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    total_words: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enriched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # "meanings" | "audio" | "translations" | "done"
    phase: Mapped[str] = mapped_column(String(20), default="meanings", nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
