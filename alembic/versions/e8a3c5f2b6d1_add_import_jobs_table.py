"""add import_jobs table

Revision ID: e8a3c5f2b6d1
Revises: d7e2f9a1b3c4
Create Date: 2026-05-29 19:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8a3c5f2b6d1'
down_revision: Union[str, Sequence[str], None] = 'd7e2f9a1b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'import_jobs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('status', sa.String(20), server_default='pending', nullable=False),
        sa.Column('total_words', sa.Integer, server_default='0', nullable=False),
        sa.Column('enriched_count', sa.Integer, server_default='0', nullable=False),
        sa.Column('phase', sa.String(20), server_default='meanings', nullable=False),
        sa.Column('error', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('import_jobs')
