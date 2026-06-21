"""add my practice publish tags feedback

Revision ID: 9c0d1e2f3a4b
Revises: f9b7a6c5d4e3
Create Date: 2026-06-05 00:00:00.000000

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9c0d1e2f3a4b"
down_revision: str | Sequence[str] | None = "f9b7a6c5d4e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "videos",
        sa.Column(
            "publish_status",
            sa.String(length=20),
            nullable=False,
            server_default="published",
        ),
    )
    op.add_column("videos", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("videos", sa.Column("reviewed_by", sa.String(length=36), nullable=True))
    op.add_column("videos", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("videos", sa.Column("review_note", sa.Text(), nullable=True))
    op.create_index("idx_videos_publish_status", "videos", ["publish_status"], unique=False)

    op.create_table(
        "topic_tags",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_topic_tags_slug"), "topic_tags", ["slug"], unique=True)

    op.create_table(
        "user_practice_videos",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("video_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "video_id", name="uq_user_practice_video"),
    )
    op.create_index(
        "idx_user_practice_user_created",
        "user_practice_videos",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_practice_videos_user_id"), "user_practice_videos", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_user_practice_videos_video_id"), "user_practice_videos", ["video_id"], unique=False
    )

    op.create_table(
        "user_practice_video_tags",
        sa.Column("practice_video_id", sa.String(length=36), nullable=False),
        sa.Column("tag_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["practice_video_id"], ["user_practice_videos.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tag_id"], ["topic_tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("practice_video_id", "tag_id", name="pk_user_practice_video_tags"),
    )

    op.create_table(
        "video_topic_tags",
        sa.Column("video_id", sa.String(length=36), nullable=False),
        sa.Column("tag_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["topic_tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("video_id", "tag_id", name="pk_video_topic_tags"),
    )

    op.create_table(
        "video_publish_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("video_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_video_publish_requests_user_id"),
        "video_publish_requests",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_video_publish_requests_video_id"),
        "video_publish_requests",
        ["video_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_video_publish_requests_status"), "video_publish_requests", ["status"], unique=False
    )

    op.create_table(
        "transcript_feedback",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("video_id", sa.String(length=36), nullable=False),
        sa.Column("transcript_id", sa.String(length=36), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("suggested_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.String(length=36), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transcript_id"], ["transcripts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_transcript_feedback_user_id"), "transcript_feedback", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_transcript_feedback_video_id"), "transcript_feedback", ["video_id"], unique=False
    )
    op.create_index(
        op.f("ix_transcript_feedback_status"), "transcript_feedback", ["status"], unique=False
    )

    bind = op.get_bind()
    videos = sa.table(
        "videos",
        sa.column("id", sa.String),
        sa.column("created_by", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    users = sa.table("users", sa.column("id", sa.String))
    user_practice_videos = sa.table(
        "user_practice_videos",
        sa.column("id", sa.String),
        sa.column("user_id", sa.String),
        sa.column("video_id", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(
        sa.select(videos.c.id, videos.c.created_by, videos.c.created_at)
        .select_from(videos.join(users, users.c.id == videos.c.created_by))
        .where(videos.c.created_by.is_not(None))
    ).all()
    if rows:
        bind.execute(
            user_practice_videos.insert(),
            [
                {
                    "id": str(uuid.uuid4()),
                    "user_id": row.created_by,
                    "video_id": row.id,
                    "created_at": row.created_at,
                }
                for row in rows
            ],
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_transcript_feedback_status"), table_name="transcript_feedback")
    op.drop_index(op.f("ix_transcript_feedback_video_id"), table_name="transcript_feedback")
    op.drop_index(op.f("ix_transcript_feedback_user_id"), table_name="transcript_feedback")
    op.drop_table("transcript_feedback")
    op.drop_index(op.f("ix_video_publish_requests_status"), table_name="video_publish_requests")
    op.drop_index(op.f("ix_video_publish_requests_video_id"), table_name="video_publish_requests")
    op.drop_index(op.f("ix_video_publish_requests_user_id"), table_name="video_publish_requests")
    op.drop_table("video_publish_requests")
    op.drop_table("video_topic_tags")
    op.drop_table("user_practice_video_tags")
    op.drop_index(op.f("ix_user_practice_videos_video_id"), table_name="user_practice_videos")
    op.drop_index(op.f("ix_user_practice_videos_user_id"), table_name="user_practice_videos")
    op.drop_index("idx_user_practice_user_created", table_name="user_practice_videos")
    op.drop_table("user_practice_videos")
    op.drop_index(op.f("ix_topic_tags_slug"), table_name="topic_tags")
    op.drop_table("topic_tags")
    op.drop_index("idx_videos_publish_status", table_name="videos")
    op.drop_column("videos", "review_note")
    op.drop_column("videos", "reviewed_at")
    op.drop_column("videos", "reviewed_by")
    op.drop_column("videos", "published_at")
    op.drop_column("videos", "publish_status")
