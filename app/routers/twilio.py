"""Twilio webhook router — inbound SMS + delivery status callbacks.

Routes
------
- ``POST /incoming-sms`` — Twilio inbound SMS webhook entry point. Validates
  ``X-Twilio-Signature``, returns an empty TwiML ``<Response/>`` immediately
  (within the 15-second window), and dispatches the message to the
  ``sms_graph`` for processing.
- ``POST /sms-status`` — Delivery status callback. Validates the signature,
  logs the status transition, and returns ``204``.

Architecture
------------
The inbound webhook follows the **thin-receiver pattern** (see
``twilio-reliability-patterns``): accept the callback, return immediately, and
process asynchronously.  The ``sms_graph`` handles all business logic —
intent classification, keyword routing, NL editing — and sends replies via the
``SMSGateway`` (a separate Twilio API call), not via inline TwiML.

Security
--------
All webhook endpoints validate ``X-Twilio-Signature`` using the Twilio SDK's
``RequestValidator``.  Signature validation requires the Auth Token (not an
API key), which is the one legitimate production use of the Auth Token (see
``twilio-security-hardening::Credential Management``).  When
``TWILIO_AUTH_TOKEN`` is not set (dev mode), validation is skipped and a
warning is logged.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app import db, settings
from app.adapters.sms import build_sms_gateway
from app.agent.deps import GraphDeps
from app.agent.sms_graph import sms_graph

logger = logging.getLogger(__name__)

router = APIRouter(tags=["twilio"])

# Lazy-built validator (requires auth token, which may be None in dev).
_validator: RequestValidator | None = None

# Regex to strip channel prefixes from Twilio address fields.
# Twilio may send From as "+15551234567" (SMS), "whatsapp:+15551234567",
# or "rcs:+15551234567".  We strip the prefix for internal lookups.
_CHANNEL_PREFIX_RE = re.compile(r"^(whatsapp|rcs|messenger):", re.IGNORECASE)


def _get_validator() -> RequestValidator | None:
    """Return the cached RequestValidator, or None if not configured."""
    global _validator
    if _validator is None and settings.twilio_auth_token:
        _validator = RequestValidator(settings.twilio_auth_token)
    return _validator


def _validate_signature(request: Request, form_data: dict[str, str]) -> bool:
    """Validate ``X-Twilio-Signature`` against the request URL and POST body.

    Returns ``True`` when the signature is valid *or* when validation is
    disabled (dev mode).  In production, always returns ``True`` only for
    genuine Twilio requests.
    """
    validator = _get_validator()
    if validator is None:
        logger.warning(
            "TWILIO_AUTH_TOKEN not set — skipping webhook signature validation"
        )
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    return validator.validate(str(request.url), form_data, signature)


def _strip_channel_prefix(address: str) -> str:
    """Remove the ``whatsapp:``, ``rcs:``, or ``messenger:`` prefix from an
    address, returning the bare E.164 number (or whatever follows the colon).

    If no prefix is present, the address is returned unchanged.
    """
    return _CHANNEL_PREFIX_RE.sub("", address).strip()


# =========================================================================
# Inbound SMS webhook
# =========================================================================


@router.post("/incoming-sms")
async def incoming_sms(request: Request) -> Response:
    """Handle an inbound SMS / WhatsApp / RCS message from Twilio.

    **Protocol** (per ``twilio-messaging-webhooks``):

    1. Validate ``X-Twilio-Signature`` (reject with 403 on mismatch).
    2. Extract ``From`` (sender) and ``Body`` (message text) from the form.
    3. Strip any channel prefix (``whatsapp:``, ``rcs:``) from ``From``.
    4. Return an empty TwiML ``<Response/>`` immediately (within 15 s).
    5. Dispatch the parsed message to the ``sms_graph`` via a background task.

    The graph handles all downstream logic — user resolution, intent
    classification, keyword routing, NL editing, and sending SMS replies
    through the ``SMSGateway``.
    """
    form_data = await request.form()
    form_dict: dict[str, str] = {k: v for k, v in form_data.items()}

    # ------------------------------------------------------------------
    # 1. Signature validation
    # ------------------------------------------------------------------
    if not _validate_signature(request, form_dict):
        logger.warning(
            "Rejected inbound SMS — invalid X-Twilio-Signature from %s",
            form_dict.get("From", "unknown"),
        )
        return PlainTextResponse("Forbidden", status_code=403)

    # ------------------------------------------------------------------
    # 2. Extract and normalise fields
    # ------------------------------------------------------------------
    from_phone = form_dict.get("From", "").strip()
    raw_body = form_dict.get("Body", "").strip()
    message_sid = form_dict.get("MessageSid", "unknown")

    # Strip channel prefix (whatsapp: / rcs: / messenger:).
    from_phone_e164 = _strip_channel_prefix(from_phone)

    if not from_phone_e164:
        logger.warning(
            "Inbound SMS %s with empty From field — returning 400",
            message_sid,
        )
        return PlainTextResponse("Missing From", status_code=400)

    # Check for muted couple (logged, not rejected — message still processed
    # so the user can UNMUTE by replying, but we track it).
    # Resolution happens inside classify_intent in the graph.

    # ------------------------------------------------------------------
    # 3. Return empty TwiML immediately (thin-receiver pattern)
    # ------------------------------------------------------------------
    twiml_resp = str(MessagingResponse())

    # ------------------------------------------------------------------
    # 4. Process in background
    # ------------------------------------------------------------------
    import asyncio

    asyncio.ensure_future(
        _process_inbound_sms(
            from_phone=from_phone_e164,
            raw_body=raw_body,
            original_from=from_phone,
            message_sid=message_sid,
        )
    )

    logger.info(
        "Inbound SMS %s from %s (e164=%s): %s",
        message_sid,
        from_phone,
        from_phone_e164,
        raw_body[:80],
    )

    return HTMLResponse(content=twiml_resp, media_type="text/xml")


async def _process_inbound_sms(
    from_phone: str,
    raw_body: str,
    original_from: str | None = None,
    message_sid: str | None = None,
) -> None:
    """Process an inbound SMS through the ``sms_graph``.

    Runs in a background ``asyncio.Task`` with its own database session.
    Errors are logged but not propagated to avoid unhandled-task crashes.

    The SMS gateway is built from settings so that the production
    ``TwilioSMSGateway`` is used when credentials are configured, and the
    dev ``LoggingSMSGateway`` is used otherwise.
    """
    async with db.session() as session:
        try:
            gateway = build_sms_gateway(
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token,
                from_phone=settings.twilio_phone_number,
                status_callback_url=settings.twilio_status_callback_url,
            )
            deps = GraphDeps(db=session, sms_gateway=gateway).resolved()
            await sms_graph.ainvoke(
                {
                    "from_phone": from_phone,
                    "raw_body": raw_body,
                    "couple_id": None,
                    "user_id": None,
                    "proposal_id": None,
                    "intent": None,
                    "edit": None,
                    "edit_valid": None,
                    "needs_clarification": None,
                    "clarification_msg": None,
                    "draft": None,
                    "proposal": None,
                    "sms_copy": None,
                    "delivery_results": [],
                    "clarification_sent": None,
                    "errors": [],
                },
                {"configurable": {"deps": deps}},
            )
            await session.commit()
        except Exception:
            logger.exception(
                "Failed to process inbound SMS msg_sid=%s from=%s (e164=%s)",
                message_sid or "?",
                original_from or from_phone,
                from_phone,
            )
            await session.rollback()


# =========================================================================
# Delivery status callback
# =========================================================================


@router.post("/sms-status")
async def sms_status_callback(request: Request) -> Response:
    """Handle delivery status callbacks from Twilio.

    Per ``twilio-webhook-architecture``:

    - Status callbacks are signed with ``X-Twilio-Signature`` — validate first.
    - They do **not** expect TwiML — return ``204 No Content``.
    - Implement the **thin-receiver pattern**: log the status and return
      immediately; no heavy processing in this handler.

    Status flow: ``queued`` → ``sent`` → ``delivered`` (or ``undelivered``/``failed``).
    When using a Messaging Service: ``accepted`` → ``queued`` → ...

    Error codes on ``failed``/``undelivered`` are logged for observability.
    """
    form_data = await request.form()
    form_dict: dict[str, str] = {k: v for k, v in form_data.items()}

    # ------------------------------------------------------------------
    # 1. Signature validation
    # ------------------------------------------------------------------
    if not _validate_signature(request, form_dict):
        logger.warning(
            "Rejected SMS status callback — invalid X-Twilio-Signature"
        )
        return PlainTextResponse("Forbidden", status_code=403)

    # ------------------------------------------------------------------
    # 2. Extract and log
    # ------------------------------------------------------------------
    message_sid = form_dict.get("MessageSid", "unknown")
    status = form_dict.get("MessageStatus", "unknown")
    error_code = form_dict.get("ErrorCode")
    error_message = form_dict.get("ErrorMessage")

    if error_code:
        logger.warning(
            "SMS delivery failed: sid=%s status=%s error=%s: %s",
            message_sid,
            status,
            error_code,
            error_message,
        )
    else:
        logger.info(
            "SMS status: sid=%s status=%s",
            message_sid,
            status,
        )

    # ------------------------------------------------------------------
    # 3. Acknowledge immediately (no body expected)
    # ------------------------------------------------------------------
    return Response(status_code=204)