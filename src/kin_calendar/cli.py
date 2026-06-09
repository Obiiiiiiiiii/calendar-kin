"""One-shot MVP flow as three commands with human review gates between them:

    kin-calendar extract backstory.txt -o spine.json
        (review/edit spine.json — the character-fidelity safeguard)
    kin-calendar generate spine.json -o events.json
        (review events.json and the optional routine note)
    kin-calendar write events.json

The review steps are deliberate: each generated event becomes asserted canon
the kin treats as true, so a human confirms the spine and the events before
anything is written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .backstory import source_from_arg
from .models import GenerationOutput, Spine
from .reconcile import Decision, coverage_check, expand_occurrences, reconcile


def _default_timezone() -> str:
    return os.environ.get("KIN_TIMEZONE", "UTC")


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def cmd_extract(args: argparse.Namespace) -> int:
    from .llm import extract_spine

    backstory = source_from_arg(args.backstory).fetch().strip()
    if not backstory:
        print("error: backstory is empty", file=sys.stderr)
        return 1

    print("Extracting life spine (grounded in the backstory only)...", file=sys.stderr)
    spine = extract_spine(backstory)

    out = Path(args.output)
    out.write_text(spine.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"Spine written to {out}", file=sys.stderr)

    flags = spine.review_flags()
    print("\n=== REVIEW BEFORE GENERATING ===", file=sys.stderr)
    if flags:
        print("Check these inferred/unknown fields (edit the JSON to correct):", file=sys.stderr)
        for f in flags:
            print(f"  - {f}", file=sys.stderr)
    else:
        print("All fields are stated in the backstory.", file=sys.stderr)
    if spine.open_questions:
        print("Open questions to settle before generation:", file=sys.stderr)
        for q in spine.open_questions:
            print(f"  ? {q}", file=sys.stderr)
    print(f"\nWhen the spine looks right:\n  kin-calendar generate {out}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> int:
    from .llm import generate_events

    spine = Spine.model_validate_json(Path(args.spine).read_text(encoding="utf-8"))
    week_start = args.week_start or date.today().isoformat()

    print(f"Generating a lean week for {spine.kin_name} starting {week_start}...", file=sys.stderr)
    result = generate_events(spine, week_start, args.weather)

    output = GenerationOutput(kin_name=spine.kin_name, week_start=week_start, result=result)
    out = Path(args.output)
    out.write_text(output.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"Events written to {out}", file=sys.stderr)

    # Dry reconcile against an empty calendar to surface overlaps/density now.
    report = reconcile(result.events, [], date.fromisoformat(week_start))

    print("\n=== PROPOSED WEEK ===", file=sys.stderr)
    for event in result.events:
        when = (
            f"{','.join(event.days_of_week)} (weekly)"
            if event.type == "standing"
            else event.date
        )
        gate = " [gates availability]" if event.gates_availability else ""
        print(f"  {when} {event.start}-{event.end}{gate}: {event.title}", file=sys.stderr)
    for v in report.rejected:
        print(f"  !! {v.event.title!r}: {v.decision.value} — {v.reason}", file=sys.stderr)
    for w in report.warnings:
        print(f"  ~ {w}", file=sys.stderr)

    if result.suggested_routine_note:
        print("\nSuggested routine note (NOT auto-written — yours to keep/edit/place/ignore):", file=sys.stderr)
        print(f"  {result.suggested_routine_note}", file=sys.stderr)
        print(
            "  If you want it, paste it into a profile field yourself; this tool never edits the kin's profile.",
            file=sys.stderr,
        )

    print(f"\nWhen the events look right:\n  kin-calendar write {out}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# serve / poll
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    from .webapp import create_app

    create_app().run(host="0.0.0.0", port=args.port)
    return 0


def cmd_poll(args: argparse.Namespace) -> int:
    from .scanner import log_scan, scan_once
    from .store import Store

    store = Store()
    result = scan_once(store)
    log_scan(store, result)
    print(result.summary(), file=sys.stderr)
    return 1 if result.error else 0


# ---------------------------------------------------------------------------
# calendars
# ---------------------------------------------------------------------------


def cmd_calendars(args: argparse.Namespace) -> int:
    """List the account's calendars so the user can designate one by ID."""
    from .calendar_writer import build_service, list_calendars

    service = build_service(args.credentials, args.token)
    print("Calendars in this Google account:\n")
    for entry in list_calendars(service):
        marker = "  (your main calendar — do NOT use for the kin)" if entry.get("primary") else ""
        print(f"  {entry.get('summary', '(unnamed)')}{marker}")
        print(f"      id: {entry['id']}\n")
    print("Designate one with:  kin-calendar write events.json --calendar-id <id>")
    return 0


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


def cmd_write(args: argparse.Namespace) -> int:
    data = GenerationOutput.model_validate_json(Path(args.events).read_text(encoding="utf-8"))
    week_start = date.fromisoformat(args.week_start or data.week_start)
    timezone = args.timezone or _default_timezone()

    if args.dry_run:
        existing = []
        writer = None
    else:
        from .calendar_writer import CalendarWriter, PrimaryCalendarError, build_service

        service = build_service(args.credentials, args.token)
        calendar_id = args.calendar_id or os.environ.get("KIN_CALENDAR_ID")
        try:
            if calendar_id:
                writer = CalendarWriter.for_calendar_id(service, calendar_id, timezone)
                label = calendar_id
            else:
                writer = CalendarWriter.for_dedicated_calendar(service, args.calendar_name, timezone)
                label = args.calendar_name
        except (PrimaryCalendarError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        window_start = datetime.now().astimezone() - timedelta(hours=24)
        window_end = datetime.now().astimezone() + timedelta(days=8)
        existing = writer.list_existing(window_start, window_end)
        print(
            f"Target calendar: {label!r} ({len(existing)} existing events in window)",
            file=sys.stderr,
        )

    report = reconcile(data.result.events, existing, week_start)

    for v in report.verdicts:
        if v.decision == Decision.ADD:
            print(f"  + {v.event.title}", file=sys.stderr)
        else:
            print(f"  - {v.event.title}: {v.decision.value} — {v.reason}", file=sys.stderr)
    for w in report.warnings:
        print(f"  ~ {w}", file=sys.stderr)

    occurrences = [
        occ for event in report.accepted for occ in expand_occurrences(event, week_start)
    ]
    for note in coverage_check(occurrences, datetime.now()):
        print(f"  ~ {note}", file=sys.stderr)

    if args.dry_run:
        print(f"\nDry run: {len(report.accepted)} event(s) would be written.", file=sys.stderr)
        return 0

    written = 0
    for event in report.accepted:
        writer.write_event(event, week_start, provenance="generated")
        written += 1
    print(f"\nWrote {written} event(s) to {label!r}.", file=sys.stderr)
    print(
        "Verify in Kindroid that the events render to the kin and that the hidden\n"
        "provenance metadata does NOT surface in what the kin sees.",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kin-calendar",
        description="Generate a believable offscreen life for a Kindroid companion.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("extract", help="backstory text -> life spine JSON (for review)")
    p.add_argument("backstory", help="path to backstory text file, or '-' for stdin")
    p.add_argument("-o", "--output", default="spine.json")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("generate", help="confirmed spine -> lean event list (for review)")
    p.add_argument("spine", help="path to reviewed spine JSON")
    p.add_argument("-o", "--output", default="events.json")
    p.add_argument("--week-start", help="YYYY-MM-DD (default: today)")
    p.add_argument("--weather", help="optional weather forecast text")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("serve", help="run the web interface locally")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("poll", help="run one chat-scanner cycle (phase 2)")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("calendars", help="list your Google calendars (to pick one by ID)")
    p.add_argument("--credentials", default="credentials.json", help="Google OAuth client secrets")
    p.add_argument("--token", default="token.json", help="cached OAuth token path")
    p.set_defaults(func=cmd_calendars)

    p = sub.add_parser("write", help="reconcile and write events to the designated calendar")
    p.add_argument("events", help="path to reviewed events JSON")
    p.add_argument(
        "--calendar-id",
        help="write to this exact calendar (see `kin-calendar calendars`); also $KIN_CALENDAR_ID",
    )
    p.add_argument(
        "--calendar-name",
        default=os.environ.get("KIN_CALENDAR_NAME", "Kin Life"),
        help="fallback when no --calendar-id: find or create a calendar with this name",
    )
    p.add_argument("--week-start", help="override the week start saved at generate time")
    p.add_argument("--timezone", help="IANA timezone for events (default: $KIN_TIMEZONE or UTC)")
    p.add_argument("--credentials", default="credentials.json", help="Google OAuth client secrets")
    p.add_argument("--token", default="token.json", help="cached OAuth token path")
    p.add_argument("--dry-run", action="store_true", help="reconcile and report without writing")
    p.set_defaults(func=cmd_write)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
