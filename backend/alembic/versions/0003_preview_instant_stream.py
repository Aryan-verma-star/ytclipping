"""previews: instant-stream columns

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03

Instant-load flow: previews store the provider-resolved direct stream URL
(played through the backend proxy while the full download runs in the
background), its required headers, and a 0..1 background-download progress
fraction for the UI. New status values (resolving/streaming) live in the
plain VARCHAR status column, so no constraint change is needed.
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("previews", sa.Column("stream_url", sa.String(length=2048), nullable=True))
    op.add_column("previews", sa.Column("stream_headers", sa.JSON(), nullable=True))
    op.add_column("previews", sa.Column("stream_provider", sa.String(length=32), nullable=True))
    op.add_column("previews", sa.Column("progress", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("previews", "progress")
    op.drop_column("previews", "stream_provider")
    op.drop_column("previews", "stream_headers")
    op.drop_column("previews", "stream_url")
