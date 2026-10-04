"""jobs: restart_retries counter

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-04

Free-tier services restart mid-clip (OOM under 60 fps sources, redeploys,
spin-down). Instead of failing those jobs outright, startup recovery now
REQUEUES them once — the provider chain re-fetches the source and the clip
renders on the (restarted) Render backend. This column counts those
automatic retries so the loop is capped at one attempt.
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("restart_retries", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("jobs", "restart_retries")
