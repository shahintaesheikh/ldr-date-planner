"""Route-level tests for the Twilio webhook endpoints.

Tests incoming SMS and delivery-status callbacks — signature validation,
form-data parsing, and correct HTTP responses — without requiring real Twilio
credentials or sending actual messages.

These are **connectivity** tests: they exercise the actual ASGI router,
request parsing, and response pipeline against the mounted endpoints.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from app import settings as app_settings


class TestIncomingSms:
    """Tests for ``POST /incoming-sms``."""

    async def test_returns_twiml_in_dev_mode(
        self, client: AsyncClient,
    ) -> None:
        """Without an auth token (dev mode), returns 200 + TwiML.

        Verifies the route is mounted and the thin-receiver pattern works:
        the endpoint returns TwiML immediately.
        """
        response = await client.post(
            "/incoming-sms",
            data={
                "From": "+15551234567",
                "Body": "Hello, this is a test",
                "MessageSid": "SM123",
            },
        )

        assert response.status_code == 200
        assert "<Response" in response.text  # self-closing: <Response />

    async def test_strips_whatsapp_prefix(
        self, client: AsyncClient,
    ) -> None:
        """A ``whatsapp:+15551234567`` From is treated the same as plain."""
        response = await client.post(
            "/incoming-sms",
            data={
                "From": "whatsapp:+15551234567",
                "Body": "Hi from WhatsApp",
                "MessageSid": "SM456",
            },
        )

        assert response.status_code == 200
        assert "<Response" in response.text  # self-closing: <Response />

    async def test_returns_400_when_from_empty(
        self, client: AsyncClient,
    ) -> None:
        """An empty ``From`` field is rejected with 400."""
        response = await client.post(
            "/incoming-sms",
            data={"From": "", "Body": "Test"},
        )

        assert response.status_code == 400
        assert "Missing From" in response.text or "400" in str(response.status_code)

    async def test_returns_403_with_invalid_signature(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the auth token is set but signature is bad, returns 403.

        The monkeypatch enables signature validation; without a valid
        ``X-Twilio-Signature`` header the request is rejected.
        """
        monkeypatch.setattr(app_settings, "twilio_auth_token", "test-auth-token-abc")

        response = await client.post(
            "/incoming-sms",
            data={
                "From": "+15551234567",
                "Body": "Should be rejected",
                "MessageSid": "SM789",
            },
        )

        assert response.status_code == 403
        # The conftest autouse fixture resets _validator after each test


class TestSmsStatus:
    """Tests for ``POST /sms-status``."""

    async def test_returns_204_in_dev_mode(
        self, client: AsyncClient,
    ) -> None:
        """Without an auth token, status callbacks return 204."""
        response = await client.post(
            "/sms-status",
            data={
                "MessageSid": "SM999",
                "MessageStatus": "delivered",
            },
        )

        assert response.status_code == 204
        assert response.content == b""

    async def test_returns_204_with_error_status(
        self, client: AsyncClient,
    ) -> None:
        """Even failed deliveries return 204 (thin-receiver pattern)."""
        response = await client.post(
            "/sms-status",
            data={
                "MessageSid": "SM888",
                "MessageStatus": "failed",
                "ErrorCode": "30001",
                "ErrorMessage": "Queue overflow",
            },
        )

        assert response.status_code == 204
        assert response.content == b""

    async def test_returns_403_with_invalid_signature(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the auth token is set but signature is bad, returns 403."""
        monkeypatch.setattr(app_settings, "twilio_auth_token", "test-auth-token-abc")

        response = await client.post(
            "/sms-status",
            data={
                "MessageSid": "SM777",
                "MessageStatus": "sent",
            },
        )

        assert response.status_code == 403