import uuid
from datetime import datetime

from sqlalchemy import String, Text, DateTime, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WordCache(Base):
    __tablename__ = "word_cache"
    __table_args__ = (
        UniqueConstraint("word", "context_hash", name="uq_word_cache_word_context"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    word: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    phonetic: Mapped[str | None] = mapped_column(String(100), nullable=True)
    audio_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    vietnamese_meaning: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_translation: Mapped[str | None] = mapped_column(Text, nullable=True)
    part_of_speech: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())