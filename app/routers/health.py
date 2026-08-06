from datetime import datetime, timezone

from fastapi import APIRouter

from app import db, settings
from app.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint.

    Verifies the application is running and the database is reachable.
    """
    db_status = "disconnected"
    try:
        async with db.session() as session:
            await session.execute(
                "SELECT 1"
            )  # lightweight connection check
            db_status = "connected"
    except Exception:
        db_status = "disconnected"

    return HealthResponse(
        status="ok",
        database=db_status,
        version=settings.app_version,
        checked_at=datetime.now(timezone.utc),
    )