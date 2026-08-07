"""Pydantic schemas for the date_activities catalog."""

from datetime import datetime
from enum import Enum as PyEnum

from pydantic import BaseModel, Field


class ActivitySource(str, PyEnum):
    seed = "seed"
    llm = "llm"
    user = "user"


class DateActivityCreate(BaseModel):
    """Schema for creating a new catalog entry."""

    name: str = Field(..., min_length=1, max_length=255, description="Activity name")
    description: str | None = Field(
        None, max_length=10000, description="Detailed description of the activity"
    )
    est_duration_min: int = Field(
        60, ge=15, le=1440, description="Estimated duration in minutes"
    )
    cost_tag: str | None = Field(
        None, max_length=32, description="Cost category (unused in v1)"
    )
    source: ActivitySource = Field(
        ActivitySource.seed, description="Origin of the activity entry"
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags for categorisation (e.g. 'cook-along', 'virtual-tour')",
    )


class DateActivityRead(BaseModel):
    """Schema for reading a catalog entry (includes id and timestamps)."""

    id: int
    name: str
    description: str | None
    est_duration_min: int
    cost_tag: str | None
    source: ActivitySource
    tags: list[str]
    embedding_model_version: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DateActivitySearchResult(BaseModel):
    """A single result from a semantic catalog search."""

    id: int
    name: str
    description: str | None
    est_duration_min: int
    cost_tag: str | None
    source: ActivitySource
    tags: list[str]
    similarity: float = Field(
        ..., ge=-1.0, le=1.0, description="Cosine similarity score"
    )


class DateActivitySearchQuery(BaseModel):
    """Input schema for searching the catalog semantically."""

    query_text: str = Field(
        ..., min_length=1, description="Natural language query text to embed and search"
    )
    max_results: int = Field(
        10, ge=1, le=50, description="Maximum number of results to return"
    )
    min_similarity: float | None = Field(
        None, ge=-1.0, le=1.0, description="Minimum cosine similarity threshold"
    )
    duration_max_min: int | None = Field(
        None,
        ge=15,
        description="Optional filter: max estimated duration in minutes",
    )


class DateActivityDedupResult(BaseModel):
    """Result of a dedup check before inserting a new catalog entry."""

    is_duplicate: bool
    existing_id: int | None = None
    existing_name: str | None = None
    similarity: float | None = None
    threshold: float