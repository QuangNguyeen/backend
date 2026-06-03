"""add video difficulty fields

Revision ID: f9b7a6c5d4e3
Revises: e8a3c5f2b6d1
Create Date: 2026-05-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f9b7a6c5d4e3"
down_revision: Union[str, Sequence[str], None] = "e8a3c5f2b6d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("difficulty_score", sa.Float(), nullable=True))
    op.add_column("videos", sa.Column("difficulty_level", sa.String(length=5), nullable=True))
    op.add_column("videos", sa.Column("difficulty_label", sa.String(length=32), nullable=True))
    op.add_column("videos", sa.Column("difficulty_factors", postgresql.JSONB(), nullable=True))
    op.add_column("videos", sa.Column("difficulty_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("videos", sa.Column("avg_words_per_segment", sa.Float(), nullable=True))
    op.add_column("videos", sa.Column("max_words_per_segment", sa.Integer(), nullable=True))
    op.add_column("videos", sa.Column("words_per_minute", sa.Float(), nullable=True))
    op.add_column("videos", sa.Column("segment_count", sa.Integer(), nullable=True))
    op.add_column("videos", sa.Column("total_word_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("videos", "total_word_count")
    op.drop_column("videos", "segment_count")
    op.drop_column("videos", "words_per_minute")
    op.drop_column("videos", "max_words_per_segment")
    op.drop_column("videos", "avg_words_per_segment")
    op.drop_column("videos", "difficulty_updated_at")
    op.drop_column("videos", "difficulty_factors")
    op.drop_column("videos", "difficulty_label")
    op.drop_column("videos", "difficulty_level")
    op.drop_column("videos", "difficulty_score")
