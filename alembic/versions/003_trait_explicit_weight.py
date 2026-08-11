"""trait_explicit_weight

Revision ID: 003
Revises: 002
Create Date: 2025-01-15

Adds explicit_weight column to the traits table — the second weight track
for explicit ratings, blended with the existing (now implicit) weight at
read time. See ldr-phase6-plan.md §1.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "traits",
        sa.Column(
            "explicit_weight",
            sa.Float(),
            nullable=True,
            comment=(
                "Explicit weight track for blended read. NULL means no "
                "explicit rating has been recorded; the effective weight "
                "falls back to the implicit weight alone."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("traits", "explicit_weight")