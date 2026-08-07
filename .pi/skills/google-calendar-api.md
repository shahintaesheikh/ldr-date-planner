---
name: google-calendar-api
description: Use this skill whenever the user wants to read, create, update, or delete Google Calendar events from code — "sync my Google Calendar," "write a script to add events to Calendar," "pull my upcoming meetings," "watch for calendar changes," or any integration using the Google Calendar REST API v3 / google-api-python-client. Covers OAuth setup, the calendarId/eventId model, the events.insert/list/patch/delete calls, recurring events (RRULE), incremental sync via syncToken, and quota/error handling. Trigger this even if the user just says "calendar API" or "Google Calendar integration" without naming the library.
---

# Google Calendar API (v3)

REST API for Google Calendar. Auth is OAuth 2.0 (or a service account for domain-wide access) — there is no API-key-only path for reading/writing a user's private calendar data.

```bash
pip install --upgrade google-api-python-client google-auth-httplib2 google-auth-oauthlib
```

## Mental model

Everything hangs off two IDs:

- **`calendarId`** — a calendar's email-like identifier, or the keyword `'primary'` for the authenticated user's default calendar.
- **`eventId`** — unique per event within a calendar. You can supply your own on creation (format-constrained, base32hex) instead of letting the server generate one — useful for idempotency and for keeping a local DB in sync.

The `events` resource is the one you'll use almost exclusively: `events().insert()`, `.list()`, `.get()`, `.patch()`, `.update()`, `.delete()`. Use `.patch()` for partial updates — `.update()` replaces the whole event body and will silently blank out fields you didn't include, `.insert()` is create-only.

## Auth (OAuth quickstart, desktop/local flow)

1. Enable the Calendar API in a Google Cloud project, configure the OAuth consent screen, create an OAuth Client ID (type: Desktop app), download as `credentials.json`.
2. Scope: use the minimum needed — `calendar.readonly` for read-only, `calendar` for full read/write. Changing scopes means deleting the cached `token.json` and re-authing.

```python
import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

creds = None
if os.path.exists("token.json"):
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)
    with open("token.json", "w") as f:
        f.write(creds.to_json())

service = build("calendar", "v3", credentials=creds)
```

For a backend service acting without a signed-in user (e.g. a Workspace org's shared calendars), use a service account with domain-wide delegation instead of the InstalledAppFlow — different setup, same `build()` call.

## Core operations

**List upcoming events** (always set `singleEvents=True` + `orderBy='startTime'` when you want expanded recurrences in chronological order — without it, recurring events return as one object with an RRULE, not per-occurrence):

```python
import datetime
now = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
events = service.events().list(
    calendarId="primary",
    timeMin=now,
    maxResults=10,
    singleEvents=True,
    orderBy="startTime",
).execute().get("items", [])
```

**Create an event** — only `start` and `end` are required; use `dateTime`+`timeZone` for timed events, `date` for all-day:

```python
event = {
    "summary": "Design review",
    "location": "800 Howard St., San Francisco, CA",
    "description": "Quarterly design review",
    "start": {"dateTime": "2026-08-14T09:00:00-07:00", "timeZone": "America/Los_Angeles"},
    "end":   {"dateTime": "2026-08-14T10:00:00-07:00", "timeZone": "America/Los_Angeles"},
    "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=6"],
    "attendees": [{"email": "a@example.com"}, {"email": "b@example.com"}],
    "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 10}]},
}
created = service.events().insert(calendarId="primary", body=event).execute()
print(created["htmlLink"])
```

Pass `sendUpdates="all"` on insert/patch/delete if attendees should get an email notification — omitted by default.

**Update (partial) — prefer patch over update:**

```python
service.events().patch(
    calendarId="primary", eventId=event_id,
    body={"summary": "Design review (rescheduled)"},
).execute()
```

**Delete:**

```python
service.events().delete(calendarId="primary", eventId=event_id).execute()
```

## Incremental sync (don't re-list everything every time)

For polling a calendar for changes, do a full sync once, store the `nextSyncToken` from the last page, then pass it back in as `syncToken` on subsequent `.list()` calls — you'll only get what changed (including deletions, marked `status: "cancelled"`).

```python
resp = service.events().list(calendarId="primary", syncToken=stored_token).execute()
```

If the token has expired (HTTP 410, `fullSyncRequired`), you must discard local state and do a full resync — the API will not tell you what you missed.

## Error handling essentials

Wrap calls in `try/except HttpError` (`from googleapiclient.errors import HttpError`). Key codes and what they mean:

| Code | Reason | Action |
|---|---|---|
| 401 | invalid/expired token | refresh via refresh_token; re-run OAuth flow if that fails |
| 403/429 | `rateLimitExceeded` / `userRateLimitExceeded` | exponential backoff + retry |
| 404 | not found | resource ID wrong or no access to that calendar |
| 409 | `duplicate` (event ID already exists) | generate a new ID, or use `.update()`/`.patch()` instead of `.insert()` |
| 410 | `fullSyncRequired` (bad/expired syncToken) | wipe local sync state, do a full sync from scratch |
| 412 | etag mismatch on `If-Match` | re-fetch the event, reapply your change, retry |

429/403 rate errors are the most common failure mode under any kind of bulk write — always retry with backoff rather than failing the whole batch, and use `events().batch()` (or client-library batching) for bulk operations instead of hammering `.insert()` in a loop.

## Gotchas specific to this integration

- **`.update()` replaces the whole event** — any field you omit gets cleared. Default to `.patch()` unless you deliberately want a full overwrite.
- **Recurring events need `singleEvents=True`** on `.list()`/`.search()`-equivalent calls to get individual occurrences; without it you get one object with an `RRULE` string you'd have to expand yourself.
- **Client-supplied event IDs** must be lowercase base32hex (`0-9`, `a-v`), 5–1024 chars — this is the mechanism for idempotent writes (retry-safe creates) and for keeping a local DB's primary key aligned with Calendar's.
- **Attendee notifications are opt-in**: `sendUpdates` defaults to not sending anything; explicitly set `"all"` if guests should be emailed.
- **syncToken vs timeMin/timeMax**: don't hand-roll polling with `updatedMin` + diffing — the token-based incremental sync is the supported pattern and is what correctly surfaces deletions.

## Reference

Guides: https://developers.google.com/workspace/calendar/api/guides/overview · Create events: https://developers.google.com/workspace/calendar/api/guides/create-events · Errors: https://developers.google.com/workspace/calendar/api/guides/errors · Sync: https://developers.google.com/workspace/calendar/api/guides/sync · Python quickstart: https://developers.google.com/workspace/calendar/api/quickstart/pyt