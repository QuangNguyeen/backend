"""add_transcription_status_to_videos

Revision ID: c5d2e8f1a4b7
Revises: b3f1a2c7d890
Create Date: 2026-05-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5d2e8f1a4b7"
down_revision: Union[str, Sequence[str], None] = "b3f1a2c7d890"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "videos",
        sa.Column("transcription_status", sa.String(20), nullable=False, server_default="ready"),
    )
    op.add_column(
        "videos",
        sa.Column("transcription_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("videos", "transcription_error")
    op.drop_column("videos", "transcription_status")
