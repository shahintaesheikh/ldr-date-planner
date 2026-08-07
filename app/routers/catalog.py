"""REST router for catalog (date_activities) CRUD operations.

Provides endpoints for listing, creating, searching, and deleting catalog
entries. The semantic search endpoint is used by the agent's `catalog_search`
tool, and the create-with-dedup endpoint mirrors the `add_to_catalog` tool
logic.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import DatabaseManager
from app import db as db_manager
from app.models.couple import ActivitySource
from app.schemas.catalog import (
    DateActivityCreate,
    DateActivityDedupResult,
    DateActivityRead,
    DateActivitySearchQuery,
    DateActivitySearchResult,
)
from app.services.catalog import catalog_service

router = APIRouter(prefix="/catalog", tags=["catalog"])


async def get_db() -> AsyncSession:
    """Yield an async database session."""
    async with db_manager.session() as session:
        yield session


@router.get("/seed", status_code=status.HTTP_200_OK)
async def seed_catalog(db: AsyncSession = Depends(get_db)) -> dict:
    """Seed the catalog with default activities if it is empty.

    Returns the number of seed entries inserted (0 if already seeded).
    """
    inserted = await catalog_service.seed_if_empty(db)
    await db.commit()
    return {"inserted": inserted, "message": "Catalog seeded successfully" if inserted else "Catalog already contains data"}


@router.get("", response_model=list[DateActivityRead])
async def list_activities(
    source: ActivitySource | None = Query(None, description="Filter by source"),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[DateActivityRead]:
    """List catalog entries, optionally filtered by source."""
    activities = await catalog_service.get_all(
        db, source=source, limit=limit, offset=offset
    )
    return [DateActivityRead.model_validate(a) for a in activities]


@router.get("/{activity_id}", response_model=DateActivityRead)
async def get_activity(
    activity_id: int,
    db: AsyncSession = Depends(get_db),
) -> DateActivityRead:
    """Get a single catalog entry by id."""
    activity = await catalog_service.get_by_id(db, activity_id)
    if activity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Activity with id {activity_id} not found",
        )
    return DateActivityRead.model_validate(activity)


@router.post("", response_model=DateActivityRead, status_code=status.HTTP_201_CREATED)
async def create_activity(
    activity_in: DateActivityCreate,
    db: AsyncSession = Depends(get_db),
) -> DateActivityRead:
    """Create a new catalog entry with generated embedding."""
    activity = await catalog_service.create(db, activity_in)
    await db.commit()
    await db.refresh(activity)
    return DateActivityRead.model_validate(activity)


@router.post("/with-dedup", response_model=dict)
async def create_activity_with_dedup(
    activity_in: DateActivityCreate,
    dedup_threshold: float | None = Query(
        None, ge=0.0, le=1.0, description="Cosine similarity threshold for dedup"
    ),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a catalog entry with dedup check.

    If a near-duplicate exists (cosine similarity >= threshold), returns the
    existing entry instead of creating a new one.
    """
    activity, was_created = await catalog_service.create_with_dedup(
        db, activity_in, dedup_threshold=dedup_threshold
    )
    await db.commit()
    await db.refresh(activity)
    return {
        "activity": DateActivityRead.model_validate(activity).model_dump(),
        "was_created": was_created,
    }


@router.post("/search", response_model=list[DateActivitySearchResult])
async def search_activities(
    query: DateActivitySearchQuery,
    db: AsyncSession = Depends(get_db),
) -> list[DateActivitySearchResult]:
    """Search the catalog via semantic (cosine similarity) search.

    Embeds the query text and finds the nearest neighbours in the
    date_activities catalog using pgvector.
    """
    results = await catalog_service.search_semantic(
        db,
        query.query_text,
        max_results=query.max_results,
        min_similarity=query.min_similarity,
        duration_max_min=query.duration_max_min,
    )
    return results


@router.post("/check-duplicate", response_model=DateActivityDedupResult)
async def check_duplicate(
    name: str = Query(..., min_length=1),
    description: str | None = Query(None),
    tags: str = Query("", description="Comma-separated tags"),
    threshold: float | None = Query(None, ge=0.0, le=1.0),
    db: AsyncSession = Depends(get_db),
) -> DateActivityDedupResult:
    """Check if a proposed activity is a near-duplicate of an existing entry."""
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    result = await catalog_service.check_duplicate(
        db,
        name=name,
        description=description,
        tags=tag_list,
        threshold=threshold,
    )
    return result


@router.delete("/{activity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_activity(
    activity_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a catalog entry by id."""
    deleted = await catalog_service.delete(db, activity_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Activity with id {activity_id} not found",
        )
    await db.commit()


@router.get("/count", response_model=dict)
async def count_activities(
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Return the total number of catalog entries."""
    count = await catalog_service.count(db)
    return {"count": count}