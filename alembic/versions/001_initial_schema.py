"""initial_schema

Revision ID: 001
Revises:
Create Date: 2025-01-01

Creates all 8 tables for the LDR Date Planner data model:
- users, couples, calendar_connections, traits, date_activities,
  proposals, feedback, sms_thread

Also enables the pgvector extension for semantic catalog search.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Enable pgvector extension ---
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ENUM types (calendar_provider, connection_status, trait_source,
    # activity_source, proposal_status, feedback_signal) are created
    # automatically by the sa.Enum columns below.

    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("phone_number", sa.String(32), nullable=False, unique=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # --- couples ---
    op.create_table(
        "couples",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "partner_a_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "partner_b_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("suggestions_muted", sa.Boolean(), server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # --- calendar_connections ---
    op.create_table(
        "calendar_connections",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "provider",
            sa.Enum("google", "caldav", name="calendar_provider"),
            nullable=False,
        ),
        sa.Column("oauth_token", sa.Text(), nullable=True),
        sa.Column("caldav_credentials", sa.Text(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("active", "disconnected", "expired", name="connection_status"),
            nullable=False,
            server_default="active",
        ),
    )

    # --- traits (EAV) ---
    op.create_table(
        "traits",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "couple_id",
            sa.BigInteger(),
            sa.ForeignKey("couples.id"),
            nullable=False,
        ),
        sa.Column("trait_key", sa.String(64), nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column(
            "source",
            sa.Enum("implicit", "explicit", name="trait_source"),
            nullable=False,
            server_default="explicit",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # --- date_activities (catalog) ---
    op.create_table(
        "date_activities",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("est_duration_min", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("cost_tag", sa.String(32), nullable=True),
        sa.Column(
            "source",
            sa.Enum("seed", "llm", "user", name="activity_source"),
            nullable=False,
            server_default="seed",
        ),
        sa.Column("tags", sa.ARRAY(sa.String(64)), server_default="{}"),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # --- proposals ---
    op.create_table(
        "proposals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "couple_id",
            sa.BigInteger(),
            sa.ForeignKey("couples.id"),
            nullable=False,
        ),
        sa.Column(
            "activity_id",
            sa.BigInteger(),
            sa.ForeignKey("date_activities.id"),
            nullable=False,
        ),
        sa.Column("proposed_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("proposed_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "confirmed", "rejected", "expired", name="proposal_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "confirmed_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # --- feedback ---
    op.create_table(
        "feedback",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.BigInteger(),
            sa.ForeignKey("proposals.id"),
            nullable=False,
        ),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column(
            "implicit_signal",
            sa.Enum("accept", "reject", "rerun", "mute", name="feedback_signal"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # --- sms_thread ---
    op.create_table(
        "sms_thread",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.BigInteger(),
            sa.ForeignKey("proposals.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("raw_body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    # Drop tables in reverse dependency order
    op.drop_table("sms_thread")
    op.drop_table("feedback")
    op.drop_table("proposals")
    op.drop_table("date_activities")
    op.drop_table("traits")
    op.drop_table("calendar_connections")
    op.drop_table("couples")
    op.drop_table("users")

    # Drop ENUM types
    op.execute("DROP TYPE IF EXISTS feedback_signal")
    op.execute("DROP TYPE IF EXISTS proposal_status")
    op.execute("DROP TYPE IF EXISTS activity_source")
    op.execute("DROP TYPE IF EXISTS trait_source")
    op.execute("DROP TYPE IF EXISTS connection_status")
    op.execute("DROP TYPE IF EXISTS calendar_provider")