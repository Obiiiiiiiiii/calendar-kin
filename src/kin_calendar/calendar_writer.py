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

_RRULE_DAY = {
    "Mon": "MO", "Tue": "TU", "Wed": "WE", "Thu": "TH",
    "Fri": "FR", "Sat": "SA", "Sun": "SU",
}


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


class CalendarWriter:
    def __init__(self, service, calendar_id: str, timezone: str):
        self.service = service
        self.calendar_id = calendar_id
        self.timezone = timezone

    @classmethod
    def for_dedicated_calendar(cls, service, calendar_name: str, timezone: str) -> "CalendarWriter":
        """Find the dedicated calendar by name, creating it if missing."""
        page_token = None
        while True:
            result = service.calendarList().list(pageToken=page_token).execute()
            for entry in result.get("items", []):
                if entry.get("summary") == calendar_name:
                    return cls(service, entry["id"], timezone)
            page_token = result.get("nextPageToken")
            if not page_token:
                break
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
