"""Calendar adapter package.

Provides a common ``CalendarAdapter`` protocol that all calendar providers
implement, plus the ``TimeRange`` value object used by ``get_busy_blocks()``.
"""

from app.adapters.caldav import CalDAVAdapter
from app.adapters.google import GoogleCalendarAdapter
from app.adapters.protocol import CalendarAdapter, TimeRange
from app.adapters.sms import (
    LoggingSMSGateway,
    SMSGateway,
    TwilioSMSGateway,
    build_sms_gateway,
)

__all__ = [
    "CalDAVAdapter",
    "CalendarAdapter",
    "GoogleCalendarAdapter",
    "LoggingSMSGateway",
    "SMSGateway",
    "TimeRange",
    "TwilioSMSGateway",
    "build_sms_gateway",
]