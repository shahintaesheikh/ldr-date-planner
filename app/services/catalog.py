"""Catalog service for date_activities.

Provides CRUD operations, semantic search via pgvector cosine similarity,
dedup-checked inserts, and seed data management. This is the service layer
that the agent's `catalog_search` and `add_to_catalog` tools call into.
"""

import math
from typing import Sequence

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.couple import DateActivity, ActivitySource
from app.schemas.catalog import (
    DateActivityCreate,
    DateActivityDedupResult,
    DateActivityRead,
    DateActivitySearchResult,
)
from app.services.embedding import embedding_service
from app.services.catalog_seed import SEED_ACTIVITIES


class CatalogService:
    """Service for managing the date_activities catalog."""

    # Default cosine similarity threshold for dedup checks
    DEDUP_THRESHOLD = 0.92

    # Minimum cosine similarity for a semantic search result to be considered
    # relevant. Below this, the agent should fall back to web search.
    DEFAULT_SEARCH_THRESHOLD = 0.75

    async def create(
        self,
        db: AsyncSession,
        activity_in: DateActivityCreate,
    ) -> DateActivity:
        """Create a new catalog entry, generating its embedding.

        Args:
            db: Database session.
            activity_in: The activity data to insert.

        Returns:
            The newly created DateActivity ORM instance.

        Raises:
            RuntimeError: If embedding generation fails (e.g. missing API key).
        """
        embedding = await embedding_service.embed_activity(
            name=activity_in.name,
            description=activity_in.description,
            tags=activity_in.tags,
        )

        activity = DateActivity(
            name=activity_in.name,
            description=activity_in.description,
            est_duration_min=activity_in.est_duration_min,
            cost_tag=activity_in.cost_tag,
            source=activity_in.source,
            tags=activity_in.tags,
            embedding=embedding,
            embedding_model_version=embedding_service.model_version,
        )
        db.add(activity)
        await db.flush()
        return activity

    async def get_by_id(self, db: AsyncSession, activity_id: int) -> DateActivity | None:
        """Retrieve a single catalog entry by its id.

        Args:
            db: Database session.
            activity_id: The id of the activity to retrieve.

        Returns:
            The DateActivity if found, else None.
        """
        result = await db.execute(
            select(DateActivity).where(DateActivity.id == activity_id)
        )
        return result.scalar_one_or_none()

    async def get_all(
        self,
        db: AsyncSession,
        *,
        source: ActivitySource | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[DateActivity]:
        """List catalog entries, optionally filtered by source.

        Args:
            db: Database session.
            source: Optional source filter (seed, llm, user).
            limit: Maximum number of entries to return.
            offset: Number of entries to skip.

        Returns:
            A list of DateActivity ORM instances.
        """
        query = select(DateActivity).order_by(DateActivity.created_at.desc())

        if source is not None:
            query = query.where(DateActivity.source == source)

        query = query.offset(offset).limit(limit)
        result = await db.execute(query)
        return list(result.scalars().all())

    async def search_semantic(
        self,
        db: AsyncSession,
        query_text: str,
        *,
        max_results: int = 10,
        min_similarity: float | None = None,
        duration_max_min: int | None = None,
    ) -> list[DateActivitySearchResult]:
        """Search the catalog via semantic (cosine similarity) search.

        Embeds the query text and finds the nearest neighbours in the
        date_activities catalog using pgvector's <=> (cosine distance)
        operator.

        Args:
            db: Database session.
            query_text: Natural language query to embed and search against.
            max_results: Max number of results to return.
            min_similarity: Optional minimum cosine similarity threshold.
            duration_max_min: Optional filter: max est_duration_min.

        Returns:
            A list of DateActivitySearchResult sorted by similarity (highest
            first).
        """
        query_embedding = await embedding_service.embed_text(query_text)
        # pgvector's <=> is cosine distance (0 = identical, 2 = opposite).
        # Convert to cosine similarity: similarity = 1 - distance.
        embedding_literal = str(query_embedding)

        params: dict = {
            "embedding": embedding_literal,
            "limit": max_results,
        }

        where_clauses = ["embedding IS NOT NULL"]

        if min_similarity is not None:
            # Cosine distance = 1 - similarity
            max_distance = 1.0 - min_similarity
            params["max_distance"] = max_distance
            where_clauses.append("embedding <=> :embedding <= :max_distance")

        if duration_max_min is not None:
            params["duration_max"] = duration_max_min
            where_clauses.append("est_duration_min <= :duration_max")

        where_sql = " AND ".join(where_clauses)

        sql = text(f"""
            SELECT
                id,
                name,
                description,
                est_duration_min,
                cost_tag,
                source,
                tags,
                1 - (embedding <=> :embedding) AS similarity
            FROM date_activities
            WHERE {where_sql}
            ORDER BY embedding <=> :embedding
            LIMIT :limit
        """)

        result = await db.execute(sql, params)
        rows = result.fetchall()

        return [
            DateActivitySearchResult(
                id=row.id,
                name=row.name,
                description=row.description,
                est_duration_min=row.est_duration_min,
                cost_tag=row.cost_tag,
                source=row.source,
                tags=row.tags,
                similarity=float(row.similarity),
            )
            for row in rows
        ]

    async def check_duplicate(
        self,
        db: AsyncSession,
        name: str,
        description: str | None,
        tags: list[str],
        *,
        threshold: float | None = None,
    ) -> DateActivityDedupResult:
        """Check if an activity is a near-duplicate of an existing catalog entry.

        Computes the candidate's embedding and compares cosine similarity
        against all existing catalog entries. If the highest similarity
        exceeds the threshold, the candidate is considered a duplicate.

        Args:
            db: Database session.
            name: Candidate activity name.
            description: Candidate activity description.
            tags: Candidate activity tags.
            threshold: Cosine similarity threshold (default 0.92).

        Returns:
            A DateActivityDedupResult indicating whether the candidate is a
            duplicate and, if so, the most similar existing entry.
        """
        threshold = threshold or self.DEDUP_THRESHOLD
        candidate_embedding = await embedding_service.embed_activity(
            name=name, description=description, tags=tags
        )

        sql = text("""
            SELECT
                id,
                name,
                1 - (embedding <=> :embedding) AS similarity
            FROM date_activities
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> :embedding
            LIMIT 1
        """)

        result = await db.execute(sql, {"embedding": candidate_embedding})
        row = result.fetchone()

        if row is None:
            return DateActivityDedupResult(
                is_duplicate=False,
                threshold=threshold,
            )

        similarity = float(row.similarity)
        if similarity >= threshold:
            return DateActivityDedupResult(
                is_duplicate=True,
                existing_id=row.id,
                existing_name=row.name,
                similarity=similarity,
                threshold=threshold,
            )

        return DateActivityDedupResult(
            is_duplicate=False,
            similarity=similarity,
            threshold=threshold,
        )

    async def create_with_dedup(
        self,
        db: AsyncSession,
        activity_in: DateActivityCreate,
        *,
        dedup_threshold: float | None = None,
    ) -> tuple[DateActivity, bool]:
        """Create a catalog entry only if it's not a near-duplicate.

        If the entry is a near-duplicate of an existing entry, returns the
        existing entry instead.

        Args:
            db: Database session.
            activity_in: The activity data to insert.
            dedup_threshold: Cosine similarity threshold for dedup.

        Returns:
            A tuple of (DateActivity, was_created) where was_created is True
            if a new entry was inserted, False if an existing entry was returned.
        """
        dedup = await self.check_duplicate(
            db,
            name=activity_in.name,
            description=activity_in.description,
            tags=activity_in.tags,
            threshold=dedup_threshold,
        )

        if dedup.is_duplicate and dedup.existing_id is not None:
            existing = await self.get_by_id(db, dedup.existing_id)
            if existing is not None:
                return existing, False

        activity = await self.create(db, activity_in)
        return activity, True

    async def delete(self, db: AsyncSession, activity_id: int) -> bool:
        """Delete a catalog entry by id.

        Args:
            db: Database session.
            activity_id: The id of the entry to delete.

        Returns:
            True if the entry was deleted, False if not found.
        """
        activity = await self.get_by_id(db, activity_id)
        if activity is None:
            return False
        await db.delete(activity)
        return True

    async def count(self, db: AsyncSession) -> int:
        """Return the total number of catalog entries.

        Args:
            db: Database session.

        Returns:
            The total count of date_activities.
        """
        result = await db.execute(
            select(text("COUNT(*)")).select_from(DateActivity.__table__)
        )
        return result.scalar_one()

    async def seed_if_empty(self, db: AsyncSession) -> int:
        """Insert seed data if the catalog is empty.

        Counts existing entries. If zero, inserts all SEED_ACTIVITIES with
        generated embeddings.

        Args:
            db: Database session.

        Returns:
            The number of seed entries inserted.
        """
        existing_count = await self.count(db)
        if existing_count > 0:
            return 0

        # Batch-embed all seed activities
        texts = [
            embedding_service._build_input_text(
                name=a.name, description=a.description, tags=a.tags
            )
            for a in SEED_ACTIVITIES
        ]
        embeddings = await embedding_service.embed_many(texts)

        activities = []
        for activity_in, emb in zip(SEED_ACTIVITIES, embeddings):
            activity = DateActivity(
                name=activity_in.name,
                description=activity_in.description,
                est_duration_min=activity_in.est_duration_min,
                cost_tag=activity_in.cost_tag,
                source=activity_in.source,
                tags=activity_in.tags,
                embedding=emb,
                embedding_model_version=embedding_service.model_version,
            )
            activities.append(activity)

        db.add_all(activities)
        await db.flush()
        return len(activities)


# Module-level singleton
catalog_service = CatalogService()