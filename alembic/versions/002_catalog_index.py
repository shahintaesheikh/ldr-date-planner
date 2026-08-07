"""catalog_index

Revision ID: 002
Revises: 001
Create Date: 2025-01-02

Adds:
- embedding_model_version column to date_activities for drift detection
- IVFFlat index on the embedding column for efficient approximate NN search
- Partial index for non-null embeddings (most queries filter on non-null)
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Add embedding_model_version column ---
    op.add_column(
        "date_activities",
        sa.Column(
            "embedding_model_version",
            sa.String(64),
            nullable=True,
            comment="Tracks which embedding model produced the vector, for drift detection",
        ),
    )

    # --- IVFFlat index on embedding column ---
    # IVFFlat (Inverted File with Flat) is pgvector's approximate nearest
    # neighbour index. The lists parameter controls the number of clusters;
    # sqrt(n_rows) is a good starting point. With ~20 seed rows + growth,
    # lists=10 is a reasonable default.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_date_activities_embedding "
        "ON date_activities USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 10)"
    )

    # --- Partial index for non-null embeddings ---
    # Speeds up the "WHERE embedding IS NOT NULL" clause used in semantic
    # search queries.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_date_activities_embedding_not_null "
        "ON date_activities (id) "
        "WHERE embedding IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_date_activities_embedding_not_null")
    op.execute("DROP INDEX IF EXISTS idx_date_activities_embedding")
    op.drop_column("date_activities", "embedding_model_version")