---
name: caldav-icloud
description: Use this skill whenever the user wants to read, create, update, or delete events/tasks on an Apple Calendar (iCloud) account, or any CalDAV server, from Python. Covers the python-caldav library — authentication (including iCloud's app-specific password requirement), the DAVClient → Principal → Calendar → Event object hierarchy, searching/filtering events by date, and creating or editing icalendar data. Trigger this for requests like "sync my iCloud calendar," "pull events from my Apple Calendar," "write a script to add events to iCloud," or any CalDAV/RFC 4791 integration work, even if the user just says "calendar API" without naming CalDAV explicitly.
---

# CalDAV / iCloud (Apple Calendar) integration

Python's `caldav` library (RFC 4791 client) is the standard way to programmatically read/write Apple Calendar, since iCloud has no public REST API for calendar data — only CalDAV.

```bash
pip install caldav --break-system-packages
```

## Mental model

CalDAV is WebDAV (folders/files over HTTP) specialized for calendars. The library mirrors this with a strict object hierarchy — always drill down through it, never guess URLs:

```
DAVClient (auth + connection)
  └─ Principal (the logged-in user)
       └─ Calendar (one calendar, e.g. "Home" or "Work")
            └─ Event / Todo / Journal (a VEVENT / VTODO / VJOURNAL)
```

Everything below assumes `from caldav import get_davclient` (v3.x API — this is the current recommended entry point, not `caldav.DAVClient(...)` directly, though that still works).

## iCloud auth (the part that trips people up)

iCloud does **not** accept your normal Apple ID password over CalDAV. You need an **app-specific password**:

1. Sign in at https://appleid.apple.com
2. Sign-In & Security → App-Specific Passwords → generate one
3. Use the Apple ID email as username, the generated password as password

Server URL: `https://caldav.icloud.com/` (the library resolves the actual per-user principal/calendar-home URLs from there — don't hardcode the `pXX-caldav.icloud.com/<id>/...` paths yourself, they're internal and can change).

```python
from caldav import get_davclient

with get_davclient(
    url="https://caldav.icloud.com/",
    username="user@icloud.com",
    password="xxxx-xxxx-xxxx-xxxx",  # app-specific password, not the Apple ID password
) as client:
    principal = client.get_principal()
    calendars = principal.calendars()
    for cal in calendars:
        print(cal.name, cal.url)
```

Best practice for credentials: keep them out of source, e.g. `~/.config/caldav/calendar.conf` (see the library's config-file support) or environment variables — never hardcode.

## Core operations

**Get a calendar directly** (skip the client/principal boilerplate when you just need one calendar):

```python
from caldav import get_calendar
with get_calendar(url="https://caldav.icloud.com/", username="...", password="...",
                   calendar_name="Home") as cal:
    ...
```

**Create an event** — either from kwargs or raw icalendar text:

```python
import datetime
cal.add_event(
    dtstart=datetime.datetime(2026, 5, 17, 8),
    dtend=datetime.datetime(2026, 5, 17, 9),
    uid="unique-id-123",       # generate your own; used later to find/update/delete
    summary="Team sync",
    rrule={"FREQ": "WEEKLY"},  # optional recurrence
)
```

**Search** (the primary way to read events — always prefer this over pulling everything):

```python
from datetime import date
events = cal.search(
    event=True,                # VEVENT only (also: todo=True, journal=True)
    start=date(2026, 5, 1),
    end=date(2026, 6, 1),
    expand=True,                # expand recurrences into individual instances in range
)
```
`expand=True` matters for recurring events — without it you get one component representing the whole series, not each occurrence.

**Read data safely** — `get_icalendar_component()` only reflects the *original* recurrence rule, not a specific modified instance, unless the object came from an `expand=True` search:

```python
comp = events[0].get_icalendar_component()
print(comp["summary"], comp.start, comp.end)
```

**Modify an event** — don't string-replace `.data`; borrow an editable instance instead:

```python
with events[0].edit_icalendar_component() as ical:
    ical["summary"] = "Rescheduled sync"
events[0].save()
```

**Delete:**

```python
events[0].delete()
# or, if you only have the UID:
cal.get_event_by_uid("unique-id-123").delete()
```

**Tasks** (VTODO) work the same way via `add_todo()` / `search(todo=True)`, plus a `.complete()` method. Some servers require a dedicated tasklist (`make_calendar(supported_calendar_component_set=['VTODO'])`) — iCloud generally doesn't.

## Gotchas specific to this integration

- **Rate limits / throttling**: iCloud will silently degrade or reject rapid-fire requests. Batch reads with `search()` date ranges instead of looping single-event fetches.
- **No official iCloud CalDAV docs**: Apple never published a spec; the library's `compatibility_hints.py` encodes known quirks. If something fails ungracefully, try passing `features="icloud"` to `get_davclient()` — it auto-applies known workarounds.
- **UIDs are yours to manage**: always set an explicit `uid` on creation so you can reliably find/update/delete the same event later — don't rely on searching by summary/time.
- **Two-factor is mandatory** for app-specific passwords to even be generatable — if the user says 2FA isn't enabled, that's the blocker, not the code.
- **`.data` is a live string, not a dict** — for anything beyond trivial one-line replacements, use `edit_icalendar_component()`/`edit_icalendar_instance()` (returns a proper `icalendar` object) rather than string manipulation, or you risk breaking iCalendar line-folding.

## Reference

Full docs: https://caldav.readthedocs.io/stable/ (Tutorial, How-To Guides, and Reference: CalDAV pages cover everything above in more depth). Source: https://github.com/python-caldav/caldav