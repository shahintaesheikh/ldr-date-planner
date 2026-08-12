"""onboarding_sessions

Revision ID: 004
Revises: 003
Create Date: 2025-06-01

Creates the onboarding_sessions table for the SMS-native onboarding flow.
See .pi/sms-auth.md for the full conversation design.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "onboarding_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("phone_number", sa.String(32), nullable=False, unique=True),
        sa.Column(
            "step",
            sa.Enum(
                "await_name",
                "await_partner_phone",
                "await_partner_name",
                "await_partner_confirm",
                "await_calendar_choice",
                "await_google_done",
                "await_apple_email",
                "await_apple_password",
                "await_traits_activity",
                "await_traits_energy",
                "complete",
                name="onboarding_step",
            ),
            nullable=False,
            server_default="await_name",
        ),
        sa.Column(
            "data",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("onboarding_sessions")
    op.execute("DROP TYPE IF EXISTS onboarding_step")