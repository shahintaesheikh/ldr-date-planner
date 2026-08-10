"""Pydantic schemas for the health check endpoint."""

from datetime import datetime

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Response from the /health endpoint."""

    status: str = Field(..., description="Overall health status")
    database: str = Field(..., description="Database connection status")
    version: str = Field(..., description="Application version")
    checked_at: datetime = Field(..., description="Timestamp of the health check")