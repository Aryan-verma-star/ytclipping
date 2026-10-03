"""initial jobs table

Revision ID: 0001
Revises:
Create Date: 2026-10-03
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("source_url", sa.String(length=2048), nullable=False),
        sa.Column("video_id", sa.String(length=16), nullable=False),
        sa.Column("video_title", sa.String(length=512), nullable=True),
        sa.Column("start_seconds", sa.Float(), nullable=False),
        sa.Column("end_seconds", sa.Float(), nullable=False),
        sa.Column("style_id", sa.String(length=64), nullable=False),
        sa.Column("style_params", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("output_filename", sa.String(length=128), nullable=True),
        sa.Column("output_size_bytes", sa.Integer(), nullable=True),
        sa.Column("output_duration_seconds", sa.Float(), nullable=True),
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
        sa.Column("file_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_video_id", "jobs", ["video_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_video_id", table_name="jobs")
    op.drop_table("jobs")
