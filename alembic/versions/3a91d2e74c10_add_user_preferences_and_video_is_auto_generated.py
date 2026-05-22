"""add user.preferences and videos.is_auto_generated

Revision ID: 3a91d2e74c10
Revises: 6e4d1649c168
Create Date: 2026-04-26 01:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3a91d2e74c10"
down_revision: Union[str, Sequence[str], None] = "6e4d1649c168"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "preferences",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "videos",
        sa.Column(
            "is_auto_generated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("videos", "is_auto_generated")
    op.drop_column("users", "preferences")
