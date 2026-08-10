"""SMS gateway adapter — outbound SMS delivery for the agent.

Defines the ``SMSGateway`` protocol that the ``deliver_sms`` node calls, plus
two implementations:

- ``TwilioSMSGateway`` — real provider send (Phase 4). The ``twilio`` package
  is imported lazily so the agent graph can be imported and tested without it.
- ``LoggingSMSGateway`` — dev/no-op stand-in that records messages in the
  graph state instead of sending. Used as the default when Twilio credentials
  are not configured, so the graph is runnable end-to-end in development and
  tests.

The agent's ``deliver_sms`` node only knows the ``SMSGateway`` protocol; the
FastAPI layer (or a test) injects the concrete gateway.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class SMSGateway(Protocol):
    """Common async interface for outbound SMS delivery."""

    async def send(self, to_phone: str, body: str) -> str:
        """Send an SMS to *to_phone* (E.164).

        Returns a provider message identifier (Twilio SID / dev id).
        """
        ...


@dataclass
class TwilioSMSGateway:
    """Twilio outbound gateway (lazy import of the ``twilio`` package).

    Parameters
    ----------
    account_sid:
        Twilio Account SID (``TWILIO_ACCOUNT_SID``).
    auth_token:
        Twilio Auth Token (``TWILIO_AUTH_TOKEN``).
    from_phone:
        The provisioned Twilio number to send from (``TWILIO_PHONE_NUMBER``).
    """

    account_sid: str
    auth_token: str
    from_phone: str

    def __post_init__(self) -> None:
        self._client = None

    def _client_factory(self):
        """Lazily import and build the Twilio REST client.

        Kept as a separate method so tests can patch it without importing
        ``twilio``.
        """
        try:
            from twilio.rest import Client
        except ImportError as exc:  # pragma: no cover - guarded import
            raise RuntimeError(
                "The 'twilio' package is not installed. Add `twilio>=9.0,<10.0` "
                "to requirements.txt to use TwilioSMSGateway."
            ) from exc
        return Client(self.account_sid, self.auth_token)

    def _client(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    async def send(self, to_phone: str, body: str) -> str:
        """Send via Twilio (the REST client is sync; dispatched to a thread)."""
        client = self._client()
        message = await asyncio.to_thread(
            lambda: client.messages.create(
                to=to_phone, from_=self.from_phone, body=body
            )
        )
        return message.sid


@dataclass
class LoggingSMSGateway:
    """Dev stand-in: logs messages instead of sending.

    Used as the default gateway when Twilio credentials are not configured so
    the agent graph is runnable in development and tests without an SMS
    provider. Returns a deterministic pseudo-SID so callers can assert on it.
    """

    async def send(self, to_phone: str, body: str) -> str:
        logger.warning("[dev SMS gateway] -> %s: %s", to_phone, body)
        digest = hashlib.sha1(f"{to_phone}:{body}".encode("utf-8")).hexdigest()[:12]
        return f"dev-{digest}"


def build_sms_gateway(
    *,
    account_sid: str | None,
    auth_token: str | None,
    from_phone: str | None,
) -> SMSGateway:
    """Build the production gateway if Twilio is configured, else the dev one.

    Intended for the FastAPI layer / test harness to construct once per app.
    """
    if account_sid and auth_token and from_phone:
        return TwilioSMSGateway(
            account_sid=account_sid,
            auth_token=auth_token,
            from_phone=from_phone,
        )
    logger.warning(
        "Twilio credentials not configured — using dev LoggingSMSGateway. "
        "SMS will not actually be delivered."
    )
    return LoggingSMSGateway()