"""Shared fixtures for route-level integration tests.

Provides an async HTTP client that talks to the FastAPI app in-process
(no real server required), plus cleanup for module-level caches that would
otherwise leak between tests.
"""

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app


@pytest.fixture
async def client() -> AsyncClient:
    """Yield an ``httpx.AsyncClient`` wired to the FastAPI app.

    All requests are handled in-process by FastAPI/ASGI — no real HTTP
    server, no database required for routes that don't need one.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _reset_twilio_validator() -> None:
    """Reset the cached Twilio ``RequestValidator`` between tests.

    ``app.routers.twilio._validator`` is a module-level global that caches
    the validator once ``twilio_auth_token`` is set.  Without resetting it,
    tests that monkeypatch the auth token would pollute one another.
    """
    from app.routers import twilio as twilio_module

    twilio_module._validator = None
    yield