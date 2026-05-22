"""add_is_admin_to_users

Revision ID: b3f1a2c7d890
Revises: 0e467626dc55
Create Date: 2026-05-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3f1a2c7d890"
down_revision: Union[str, Sequence[str], None] = "0e467626dc55"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.execute("UPDATE users SET is_admin = true WHERE email = 'admin@gmail.com'")


def downgrade() -> None:
    op.drop_column("users", "is_admin")