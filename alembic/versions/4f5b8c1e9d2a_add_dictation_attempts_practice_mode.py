"""add dictation_attempts.practice_mode

Revision ID: 4f5b8c1e9d2a
Revises: 3a91d2e74c10
Create Date: 2026-04-26 01:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4f5b8c1e9d2a"
down_revision: Union[str, Sequence[str], None] = "3a91d2e74c10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "dictation_attempts",
        sa.Column(
            "practice_mode",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'sentence'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("dictation_attempts", "practice_mode")
