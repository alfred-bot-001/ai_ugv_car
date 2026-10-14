"""Initial admin, session, device and metadata audit schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admins",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("password_hash", sa.String, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
    )
    op.create_table(
        "browser_sessions",
        sa.Column("token_hash", sa.String, primary_key=True),
        sa.Column("csrf_hash", sa.String, nullable=False),
        sa.Column("generation", sa.Integer, nullable=False),
        sa.Column("created", sa.Float, nullable=False),
        sa.Column("touched", sa.Float, nullable=False),
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("token_hash", sa.String, nullable=False),
    )
    op.create_table(
        "audit",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("at", sa.Float, nullable=False),
        sa.Column("event", sa.String, nullable=False),
    )


def downgrade() -> None:
    for table in ("audit", "agents", "browser_sessions", "admins"):
        op.drop_table(table)
