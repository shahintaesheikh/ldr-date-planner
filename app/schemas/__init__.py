from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.trait import TraitCreate, TraitRead, TraitSet, TraitUpdate


class HealthResponse(BaseModel):
    """Response from the /health endpoint."""

    status: str = Field(..., description="Overall health status")
    database: str = Field(..., description="Database connection status")
    version: str = Field(..., description="Application version")
    checked_at: datetime = Field(..., description="Timestamp of the health check")


__all__ = [
    "HealthResponse",
    "TraitCreate",
    "TraitRead",
    "TraitSet",
    "TraitUpdate",
]