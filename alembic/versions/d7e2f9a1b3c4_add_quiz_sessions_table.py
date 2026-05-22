"""add quiz_sessions table

Revision ID: d7e2f9a1b3c4
Revises: 6a3955453f30
Create Date: 2026-05-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7e2f9a1b3c4'
down_revision: Union[str, Sequence[str], None] = '6a3955453f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'quiz_sessions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('video_id', sa.String(36), sa.ForeignKey('videos.id'), nullable=False),
        sa.Column('status', sa.String(20), server_default='in_progress'),
        sa.Column('total_questions', sa.Integer, server_default='0'),
        sa.Column('correct_count', sa.Integer, nullable=True),
        sa.Column('score', sa.Float, nullable=True),
        sa.Column('questions', sa.JSON, nullable=True),
        sa.Column('answers', sa.JSON, nullable=True),
        sa.Column('duration_seconds', sa.Integer, nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('quiz_sessions')