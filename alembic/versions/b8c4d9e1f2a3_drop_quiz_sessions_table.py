"""drop quiz_sessions table (Quiz feature removed)

Revision ID: b8c4d9e1f2a3
Revises: 9c0d1e2f3a4b
Create Date: 2026-06-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b8c4d9e1f2a3"
down_revision: Union[str, Sequence[str], None] = "9c0d1e2f3a4b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("quiz_sessions")


def downgrade() -> None:
    op.create_table(
        "quiz_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("video_id", sa.String(36), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("status", sa.String(20), server_default="in_progress"),
        sa.Column("total_questions", sa.Integer, server_default="0"),
        sa.Column("correct_count", sa.Integer, nullable=True),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("questions", sa.JSON, nullable=True),
        sa.Column("answers", sa.JSON, nullable=True),
        sa.Column("duration_seconds", sa.Integer, nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )