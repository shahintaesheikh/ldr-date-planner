"""Google Calendar OAuth 2.0 routes.

Two endpoints implement the OAuth authorization-code flow:

- ``GET /auth/google?user_id=<int>`` — redirects the user to Google's
  consent screen (offline access, so we get a refresh token).
- ``GET /auth/google/callback`` — handles the redirect back from Google,
  exchanges the auth code for tokens, and persists them in the
  ``calendar_connections`` table.

State management
----------------
A simple in-memory dict maps ``state`` tokens to ``user_id`` values so the
callback can associate the OAuth response with the correct user.  This is
adequate for single-server development; production should use Redis or the
database.

After a successful handshake the user is redirected to the frontend at
``GOOGLE_SUCCESS_URL`` (default: ``/``).
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow
from sqlalchemy import select

from app import db, settings
from app.models import CalendarConnection, CalendarProvider, ConnectionStatus

router = APIRouter(prefix="/auth", tags=["google-calendar"])

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# ---------------------------------------------------------------------------
# In-memory OAuth state store  (state_token -> user_id)
# ---------------------------------------------------------------------------
_oauth_states: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Client config template  (filled from settings at call time)
# ---------------------------------------------------------------------------

def _client_config() -> dict[str, Any]:
    """Build the Google OAuth client config dict from settings."""
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }


def _build_flow(state: str | None = None) -> Flow:
    """Create a :class:`Flow` from the application settings."""
    return Flow.from_client_config(
        _client_config(),
        scopes=SCOPES,
        state=state,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/google")
async def google_auth_start(
    user_id: int = Query(..., description="ID of the user connecting their Google Calendar"),
) -> RedirectResponse:
    """Start the Google Calendar OAuth 2.0 authorization-code flow.

    Redirects the user's browser to Google's consent screen.  After
    authorization Google redirects to ``/auth/google/callback``.
    """
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=500,
            detail="Google OAuth credentials not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET)",
        )

    # Generate a random state token and associate it with the user
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = user_id

    flow = _build_flow(state=state)
    flow.redirect_uri = str(settings.google_redirect_uri)

    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # Force consent screen so Google always issues a refresh_token
    )

    return RedirectResponse(url=authorization_url, status_code=302)


@router.get("/google/callback")
async def google_auth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Handle the OAuth callback from Google.

    Exchanges the authorization code for access + refresh tokens, stores them
    in the ``calendar_connections`` table, and redirects to a simple
    "Connected!" page so the user can return to SMS.

    On failure redirects to a simple error page.
    """
    if error:
        return RedirectResponse(
            url="data:text/html," + _error_page(f"Google OAuth returned an error: {error}"),
            status_code=303,
        )

    if not code or not state:
        return RedirectResponse(
            url="data:text/html," + _error_page("Missing OAuth parameters."),
            status_code=303,
        )

    # Resolve the user_id from the stored state
    user_id = _oauth_states.pop(state, None)
    if user_id is None:
        return RedirectResponse(
            url="data:text/html," + _error_page("Invalid or expired OAuth session. Please re-start the connection flow."),
            status_code=303,
        )

    # Exchange the auth code for tokens
    flow = _build_flow(state=state)
    flow.redirect_uri = str(settings.google_redirect_uri)

    try:
        flow.fetch_token(code=code)
    except Exception as exc:
        return RedirectResponse(
            url="data:text/html," + _error_page(f"Failed to connect: {exc}"),
            status_code=303,
        )

    creds = flow.credentials

    # Persist the tokens in the calendar_connections table
    async with db.session() as session:
        # Check for an existing Google connection for this user
        result = await session.execute(
            select(CalendarConnection).where(
                CalendarConnection.user_id == user_id,
                CalendarConnection.provider == CalendarProvider.google,
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.oauth_token = creds.to_json()
            existing.refresh_token = creds.refresh_token
            existing.status = ConnectionStatus.active
        else:
            conn = CalendarConnection(
                user_id=user_id,
                provider=CalendarProvider.google,
                oauth_token=creds.to_json(),
                refresh_token=creds.refresh_token,
                status=ConnectionStatus.active,
            )
            session.add(conn)

        await session.commit()

    return RedirectResponse(
        url="data:text/html," + _success_page(),
        status_code=303,
    )


# ---------------------------------------------------------------------------
# Inline HTML pages for the redirect (no static HTML, no React frontend)
# ---------------------------------------------------------------------------


def _success_page() -> str:
    """A simple "Connected!" page shown after successful Google OAuth."""
    import urllib.parse

    html = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connected!</title>
<style>
  body { font-family: -apple-system, sans-serif; text-align: center; padding: 40px 20px; }
  h1 { color: #2e7d32; font-size: 24px; }
  p { color: #555; font-size: 16px; }
</style>
</head>
<body>
  <h1>✅ Connected!</h1>
  <p>Your Google Calendar is linked. You can close this page and return to SMS.</p>
</body>
</html>"""
    return urllib.parse.quote(html)


def _error_page(reason: str) -> str:
    """A simple error page shown when Google OAuth fails."""
    import urllib.parse

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connection Failed</title>
<style>
  body {{ font-family: -apple-system, sans-serif; text-align: center; padding: 40px 20px; }}
  h1 {{ color: #c62828; font-size: 24px; }}
  p {{ color: #555; font-size: 16px; }}
</style>
</head>
<body>
  <h1>❌ Connection Failed</h1>
  <p>{{ reason }}</p>
  <p>Please try again from the SMS conversation.</p>
</body>
</html>"""
    return urllib.parse.quote(html)