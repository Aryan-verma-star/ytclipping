"""previews table + jobs.preview_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03

Adds the preview pipeline (timeline UI): a `previews` table holding one full
source download + filmstrip metadata per preview, and an optional
`jobs.preview_id` reference so a clip job can reuse the preview's file
instead of downloading the source a second time.
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "previews",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("video_id", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("thumb_dir", sa.String(length=1024), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("thumb_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_previews_video_id", "previews", ["video_id"])
    op.create_index("ix_previews_status", "previews", ["status"])

    op.add_column("jobs", sa.Column("preview_id", sa.String(length=32), nullable=True))
    op.create_index("ix_jobs_preview_id", "jobs", ["preview_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_preview_id", table_name="jobs")
    op.drop_column("jobs", "preview_id")
    op.drop_index("ix_previews_status", table_name="previews")
    op.drop_index("ix_previews_video_id", table_name="previews")
    op.drop_table("previews")
