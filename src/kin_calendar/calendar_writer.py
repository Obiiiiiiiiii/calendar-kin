"""Google Calendar writer.

Writes to a DEDICATED calendar (never the user's real calendars) so real life
doesn't bleed into the fiction. Provenance is stamped into private extended
properties — hidden metadata the kin must never see in title/description/
location. Verify against the live Kindroid integration that private extended
properties do not surface before relying on them (see README, First test).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from .models import GeneratedEvent
from .reconcile import ExistingEvent, first_date_for_weekday

SCOPES = ["https://www.googleapis.com/auth/calendar"]

PROVENANCE_KEY = "kin_source"  # "generated" | "mentioned"
TOOL_KEY = "kin_tool"
TOOL_NAME = "kin-calendar"

def _parse_google_ts(value: Optional[str]) -> Optional[datetime]:
    """Google's created/updated timestamps are RFC3339 UTC, e.g.
    '2026-06-09T18:00:00.000Z'. Returns a timezone-aware datetime."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_RRULE_DAY = {
    "Mon": "MO", "Tue": "TU", "Wed": "WE", "Thu": "TH",
    "Fri": "FR", "Sat": "SA", "Sun": "SU",
}


def service_from_token(token_path: str):
    """Build a Calendar service from a saved token only (no interactive flow).

    Used by the web app, where Google access is granted via the browser
    redirect flow and the token is saved to the data directory.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_file = Path(token_path)
    if not token_file.exists():
        raise RuntimeError("Google Calendar is not connected yet")
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("Google Calendar token is invalid — reconnect")
    return build("calendar", "v3", credentials=creds)


def build_service(credentials_path: str = "credentials.json", token_path: str = "token.json"):
    """OAuth installed-app flow. Caches the token next to the project."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_file = Path(token_path)
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("calendar", "v3", credentials=creds)


def list_calendars(service) -> List[dict]:
    """All calendars visible to the authorized account (for the user to pick from)."""
    entries: List[dict] = []
    page_token = None
    while True:
        result = service.calendarList().list(pageToken=page_token).execute()
        entries.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            return entries


class PrimaryCalendarError(Exception):
    """Raised when the designated calendar is the account's primary calendar."""


class CalendarWriter:
    def __init__(self, service, calendar_id: str, timezone: str):
        self.service = service
        self.calendar_id = calendar_id
        self.timezone = timezone

    @classmethod
    def for_calendar_id(cls, service, calendar_id: str, timezone: str) -> "CalendarWriter":
        """Use a calendar the user designated by ID.

        Refuses the account's primary calendar: the kin's fictional life must
        live on a calendar separate from the user's real one, or real events
        bleed into the fiction (and the fiction into real life).
        """
        for entry in list_calendars(service):
            if entry["id"] == calendar_id or (calendar_id == "primary" and entry.get("primary")):
                if entry.get("primary"):
                    raise PrimaryCalendarError(
                        f"{entry['id']!r} is your main personal calendar — pick or create "
                        "a separate one for the kin (see `kin-calendar calendars`)"
                    )
                return cls(service, entry["id"], timezone)
        raise ValueError(
            f"calendar {calendar_id!r} not found in this account — "
            "run `kin-calendar calendars` to list available IDs"
        )

    @classmethod
    def for_dedicated_calendar(cls, service, calendar_name: str, timezone: str) -> "CalendarWriter":
        """Find the dedicated calendar by name, creating it if missing."""
        for entry in list_calendars(service):
            if entry.get("summary") == calendar_name and not entry.get("primary"):
                return cls(service, entry["id"], timezone)
        created = service.calendars().insert(
            body={"summary": calendar_name, "timeZone": timezone}
        ).execute()
        return cls(service, created["id"], timezone)

    # -- reading (for reconciliation) ---------------------------------------

    def list_existing(self, window_start: datetime, window_end: datetime) -> List[ExistingEvent]:
        events: List[ExistingEvent] = []
        page_token = None
        while True:
            result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=window_start.isoformat() + "Z" if window_start.tzinfo is None else window_start.isoformat(),
                timeMax=window_end.isoformat() + "Z" if window_end.tzinfo is None else window_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                pageToken=page_token,
            ).execute()
            for item in result.get("items", []):
                start = item.get("start", {}).get("dateTime")
                end = item.get("end", {}).get("dateTime")
                if not start or not end:
                    continue  # all-day events are out of scope
                props = item.get("extendedProperties", {}).get("private", {})
                events.append(
                    ExistingEvent(
                        title=item.get("summary", ""),
                        start=datetime.fromisoformat(start).replace(tzinfo=None),
                        end=datetime.fromisoformat(end).replace(tzinfo=None),
                        source=props.get(PROVENANCE_KEY, "generated"),
                        created=_parse_google_ts(item.get("created")),
                        updated=_parse_google_ts(item.get("updated")),
                        event_id=item.get("id"),
                    )
                )
            page_token = result.get("nextPageToken")
            if not page_token:
                return events

    # -- writing -------------------------------------------------------------

    def write_event(
        self,
        event: GeneratedEvent,
        week_start: date,
        provenance: str = "generated",
    ) -> dict:
        body = self._event_body(event, week_start, provenance)
        return self.service.events().insert(calendarId=self.calendar_id, body=body).execute()

    def delete_event(self, event_id: str) -> None:
        """Remove an event displaced by a replacement (e.g. mentioned beats generated)."""
        self.service.events().delete(calendarId=self.calendar_id, eventId=event_id).execute()

    def _event_body(self, event: GeneratedEvent, week_start: date, provenance: str) -> dict:
        if event.type == "oneoff":
            start_date = date.fromisoformat(event.date)
            recurrence: Optional[List[str]] = None
        else:
            # Anchor the recurring series on its first weekday; RRULE carries the rest.
            start_date = min(first_date_for_weekday(week_start, wd) for wd in event.days_of_week)
            by_day = ",".join(_RRULE_DAY[wd] for wd in event.days_of_week)
            recurrence = [f"RRULE:FREQ=WEEKLY;BYDAY={by_day}"]

        start_dt = datetime.combine(start_date, datetime.strptime(event.start, "%H:%M").time())
        end_dt = datetime.combine(start_date, datetime.strptime(event.end, "%H:%M").time())
        if event.crosses_midnight:
            end_dt += timedelta(days=1)

        body: dict = {
            "summary": event.title,
            "description": event.description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": self.timezone},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": self.timezone},
            "extendedProperties": {
                "private": {PROVENANCE_KEY: provenance, TOOL_KEY: TOOL_NAME}
            },
        }
        if event.location:
            body["location"] = event.location
        if recurrence:
            body["recurrence"] = recurrence
        if event.gates_availability:
            body["transparency"] = "opaque"
        else:
            body["transparency"] = "transparent"
        return body
