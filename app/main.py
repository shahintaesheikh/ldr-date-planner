from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app import db, settings
from app.routers import (
    catalog_router,
    google_auth_router,
    health_router,
    twilio_router,
)
from app.services.cadence_scheduler import start_scheduler, stop_scheduler


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan handler — initialises and tears down resources."""
    # Startup: initialise scheduler
    start_scheduler()
    yield
    # Shutdown: clean up database connections and scheduler
    stop_scheduler()
    await db.close()


app = FastAPI(
    title="LDR Date Planner API",
    version=settings.app_version,
    lifespan=lifespan,
)

# Register routers
app.include_router(catalog_router)
app.include_router(google_auth_router)
app.include_router(health_router)
app.include_router(twilio_router)