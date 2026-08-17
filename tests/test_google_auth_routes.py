"""Route-level tests for the Google Calendar OAuth 2.0 endpoints.

Tests that both routes (start + callback) are properly mounted, return the
correct HTTP status codes, and handle edge cases — without requiring real
Google OAuth credentials or a database.

These are **connectivity** tests: they exercise the actual ASGI router and
request/response pipeline.  No mock replaces the router itself.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import AsyncIterator

import pytest
from asgiref.testing import ApplicationCommunicator
from httpx import AsyncClient

from app import settings as app_settings
from app.main import app


# ── ASGI helper (bypasses httpx redirect bug on ``data:text/html`` URLs) ────


async def _asgi_get(
    path: str, query_params: dict[str, str] | None = None
) -> tuple[int, dict[bytes, bytes], bytes]:
    """Send a raw ASGI ``GET`` request and return ``(status, headers, body)``.

    ``httpx`` (even with ``follow_redirects=False``) crashes on ``data:``
    scheme Location headers (httpx ≤ 0.28.x bug).  This helper talks directly
    to the ASGI app via ``asgiref.testing.ApplicationCommunicator`` so we can
    test redirects with ``data:text/html`` targets.
    """
    query_string = urllib.parse.urlencode(query_params or {}).encode()
    scope: dict = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": [(b"host", b"testserver")],
        "scheme": "http",
        "client": ("127.0.0.1", 0),
        "server": ("testserver", 80),
    }

    communicator = ApplicationCommunicator(app, scope)
    await communicator.send_input(
        {"type": "http.request", "body": b"", "more_body": False}
    )

    status_code: int | None = None
    headers: dict[bytes, bytes] = {}
    body = b""

    while True:
        event = await communicator.receive_output(timeout=5)
        if event["type"] == "http.response.start":
            status_code = event["status"]
            headers = dict(event["headers"])
        elif event["type"] == "http.response.body":
            body += event.get("body", b"")
            if not event.get("more_body", False):
                break

    assert status_code is not None, "No response received from ASGI app"
    return status_code, headers, body


# ── Tests ──────────────────────────────────────────────────────────────────


class TestGoogleAuthStart:
    """Tests for ``GET /auth/google?user_id=<int>``."""

    async def test_redirects_to_google_when_configured(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With credentials set, returns 302 with Google's consent URL."""
        monkeypatch.setattr(app_settings, "google_client_id", "test-client-id")
        monkeypatch.setattr(app_settings, "google_client_secret", "test-client-secret")
        monkeypatch.setattr(
            app_settings, "google_redirect_uri", "http://localhost/callback"
        )

        response = await client.get(
            "/auth/google", params={"user_id": 1}, follow_redirects=False
        )

        assert response.status_code == 302
        location = response.headers.get("location", "")
        assert "accounts.google.com" in location
        assert "client_id=test-client-id" in location

    async def test_returns_500_when_credentials_missing(
        self, client: AsyncClient,
    ) -> None:
        """When Google credentials are not set, returns 500 with detail."""
        # Ensure credentials are None (default from Settings with no .env)
        response = await client.get("/auth/google", params={"user_id": 1})

        assert response.status_code == 500
        detail = response.json().get("detail", "").lower()
        assert "not configured" in detail

    async def test_returns_422_when_user_id_missing(
        self, client: AsyncClient,
    ) -> None:
        """Omitting the required ``user_id`` query param yields 422."""
        response = await client.get("/auth/google")

        assert response.status_code == 422
        errors = response.json().get("detail", [])
        assert any("user_id" in str(e) for e in errors)


class TestGoogleAuthCallback:
    """Tests for ``GET /auth/google/callback``.

    Uses a raw ASGI helper because httpx (≤ 0.28.x) crashes on
    ``data:text/html`` redirect Location headers even when redirect
    following is disabled.
    """

    async def test_error_param_redirects_to_error_page(self) -> None:
        """When Google passes an ``error`` param, user sees error page."""
        status, headers, _ = await _asgi_get(
            "/auth/google/callback", {"error": "access_denied"}
        )

        assert status == 303
        location = urllib.parse.unquote(headers.get(b"location", b"").decode())
        assert "Connection Failed" in location

    async def test_missing_code_and_state_returns_error(self) -> None:
        """Absent ``code`` and ``state`` yields a missing-params error."""
        status, headers, _ = await _asgi_get("/auth/google/callback")

        assert status == 303
        location = urllib.parse.unquote(headers.get(b"location", b"").decode())
        assert "Connection Failed" in location

    async def test_invalid_state_returns_error(self) -> None:
        """A ``state`` not in the in-memory store yields invalid-session."""
        status, headers, _ = await _asgi_get(
            "/auth/google/callback",
            {"code": "dummy-auth-code", "state": "never-seen-before"},
        )

        assert status == 303
        location = urllib.parse.unquote(headers.get(b"location", b"").decode())
        assert "Connection Failed" in location