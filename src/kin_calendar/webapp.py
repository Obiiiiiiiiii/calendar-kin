"""Single-user web interface over the same pipeline as the CLI.

Designed for self-hosting (e.g. one click on Railway): paste the backstory,
review the spine, review the events, write to the designated calendar, and
manage the phase-2 chat scanner — no command line, no local Python.

Security model: one shared password (APP_PASSWORD) gates everything, because
a deployed instance holds calendar access. Google is connected via the
browser OAuth flow; the token lives in DATA_DIR.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from pydantic import ValidationError
from werkzeug.middleware.proxy_fix import ProxyFix

from .models import GenerationOutput, GenerationResult, Spine
from .reconcile import Decision, coverage_check, expand_occurrences, reconcile
from .store import Store

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ---------------------------------------------------------------------------
# auth helper
# ---------------------------------------------------------------------------


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if os.environ.get("APP_PASSWORD") and not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


# ---------------------------------------------------------------------------
# scanner background loop
# ---------------------------------------------------------------------------


def _background_loop(store: Store) -> None:
    """One loop, two jobs: the chat scanner poll and the weekly auto-refresh."""
    from .refresh import auto_refresh, refresh_due
    from .scanner import log_scan, next_poll_minutes, scan_once
    from .timeutil import local_now

    base = int(os.environ.get("KIN_POLL_MINUTES", "15"))
    idle_streak = 0
    scan_due = 0.0
    refresh_retry_gate = 0.0  # backoff after a failed refresh (don't hammer the LLM)

    while True:
        time.sleep(30)
        state = store.state()

        if state.get("scanner_enabled"):
            if time.monotonic() >= scan_due:
                result = scan_once(store)
                log_scan(store, result)
                # Back off while the chat is quiet; snap back when something arrives.
                if result.error is None and result.messages_seen > 0:
                    idle_streak = 0
                else:
                    idle_streak += 1
                scan_due = time.monotonic() + next_poll_minutes(base, idle_streak) * 60
        else:
            idle_streak = 0  # re-enabling starts fresh at the base rate

        if (
            state.get("auto_refresh_enabled", True)
            and time.monotonic() >= refresh_retry_gate
            and refresh_due(state, local_now(store.timezone()).date())
        ):
            result = auto_refresh(store)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            store.append_log(f"[{stamp}] {result.summary()}")
            if result.error:
                refresh_retry_gate = time.monotonic() + 6 * 3600


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------


def create_app(store: Store | None = None) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)  # Railway TLS proxy
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    store = store or Store()
    app.config["STORE"] = store

    if os.environ.get("DISABLE_SCANNER_THREAD") != "1":
        thread = threading.Thread(target=_background_loop, args=(store,), daemon=True)
        thread.start()

    # -- auth ----------------------------------------------------------------

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not os.environ.get("APP_PASSWORD"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            if secrets.compare_digest(
                request.form.get("password", ""), os.environ["APP_PASSWORD"]
            ):
                session["authed"] = True
                return redirect(request.args.get("next") or url_for("dashboard"))
            flash("Wrong password.")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # -- dashboard -----------------------------------------------------------

    @app.get("/")
    @login_required
    def dashboard():
        state = store.state()
        status = {
            "password_set": bool(os.environ.get("APP_PASSWORD")),
            "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "google_connected": store.token_path.exists(),
            "calendar": state.get("calendar_label"),
            "kindroid": bool(
                store.setting("kindroid_api_key", "KINDROID_API_KEY")
                and store.setting("kindroid_ai_id", "KINDROID_AI_ID")
            ),
            "spine_exists": store.spine_path.exists(),
            "events_exist": store.events_path.exists(),
            "scanner_enabled": state.get("scanner_enabled"),
        }
        return render_template("dashboard.html", status=status, state=state)

    # -- step 1: backstory -> spine -------------------------------------------

    @app.route("/extract", methods=["GET", "POST"])
    @login_required
    def extract():
        if request.method == "POST":
            backstory = request.form.get("backstory", "").strip()
            if not backstory:
                flash("Paste a backstory first.")
                return redirect(url_for("extract"))
            from .llm import extract_spine

            try:
                spine = extract_spine(backstory)
            except Exception as e:  # noqa: BLE001 — show the failure, don't 500
                flash(f"Extraction failed: {e}")
                return redirect(url_for("extract"))
            store.write_text(store.spine_path, spine.model_dump_json(indent=2) + "\n")
            store.update_state(kin_name=spine.kin_name)
            flash("Spine extracted — review it below before generating.")
            return redirect(url_for("spine"))
        return render_template("extract.html")

    # -- step 2: review spine, then generate -----------------------------------

    @app.route("/spine", methods=["GET", "POST"])
    @login_required
    def spine():
        if request.method == "POST":
            raw = request.form.get("spine_json", "")
            try:
                parsed = Spine.model_validate_json(raw)
            except ValidationError as e:
                flash(f"That edit isn't valid spine JSON: {e.errors()[0].get('msg', e)}")
                return render_template("spine.html", spine_json=raw, flags=[], questions=[])
            store.write_text(store.spine_path, parsed.model_dump_json(indent=2) + "\n")
            store.update_state(kin_name=parsed.kin_name)
            flash("Spine saved.")
            return redirect(url_for("spine"))

        raw = store.read_text(store.spine_path)
        if raw is None:
            flash("No spine yet — start by pasting the backstory.")
            return redirect(url_for("extract"))
        parsed = Spine.model_validate_json(raw)
        return render_template(
            "spine.html",
            spine_json=raw,
            flags=parsed.review_flags(),
            questions=parsed.open_questions,
            today=date.today().isoformat(),
        )

    @app.post("/generate")
    @login_required
    def generate():
        raw = store.read_text(store.spine_path)
        if raw is None:
            flash("No spine yet.")
            return redirect(url_for("extract"))
        parsed = Spine.model_validate_json(raw)
        week_start = request.form.get("week_start") or date.today().isoformat()
        weather = request.form.get("weather") or None
        from .llm import generate_events

        try:
            result = generate_events(parsed, week_start, weather)
        except Exception as e:  # noqa: BLE001
            flash(f"Generation failed: {e}")
            return redirect(url_for("spine"))
        output = GenerationOutput(kin_name=parsed.kin_name, week_start=week_start, result=result)
        store.write_text(store.events_path, output.model_dump_json(indent=2) + "\n")
        flash("Week generated — review the events below before writing.")
        return redirect(url_for("events"))

    # -- step 3: review events, then write --------------------------------------

    def _events_context(raw: str):
        output = GenerationOutput.model_validate_json(raw)
        week_start = date.fromisoformat(output.week_start)
        report = reconcile(output.result.events, [], week_start)
        rows = []
        for ev in output.result.events:
            when = (
                f"{','.join(ev.days_of_week)} (weekly)" if ev.type == "standing" else ev.date
            )
            rows.append(
                {
                    "when": when,
                    "time": f"{ev.start}–{ev.end}",
                    "title": ev.title,
                    "gates": ev.gates_availability,
                }
            )
        problems = [f"{v.event.title}: {v.decision.value} — {v.reason}" for v in report.rejected]
        return output, rows, problems, report.warnings

    @app.route("/events", methods=["GET", "POST"])
    @login_required
    def events():
        if request.method == "POST":
            raw = request.form.get("events_json", "")
            try:
                GenerationOutput.model_validate_json(raw)
            except ValidationError as e:
                flash(f"That edit isn't valid events JSON: {e.errors()[0].get('msg', e)}")
                return render_template(
                    "events.html", events_json=raw, rows=[], problems=[], warnings=[],
                    routine_note=None, calendar_label=None,
                )
            store.write_text(store.events_path, raw if raw.endswith("\n") else raw + "\n")
            flash("Events saved.")
            return redirect(url_for("events"))

        raw = store.read_text(store.events_path)
        if raw is None:
            flash("No events yet — generate them from the spine first.")
            return redirect(url_for("spine"))
        output, rows, problems, warnings = _events_context(raw)
        return render_template(
            "events.html",
            events_json=raw,
            rows=rows,
            problems=problems,
            warnings=warnings,
            routine_note=output.result.suggested_routine_note,
            calendar_label=store.state().get("calendar_label"),
        )

    @app.post("/write")
    @login_required
    def write():
        raw = store.read_text(store.events_path)
        state = store.state()
        if raw is None:
            flash("No events to write.")
            return redirect(url_for("events"))
        if not state.get("calendar_id"):
            flash("Designate a target calendar in Settings first.")
            return redirect(url_for("settings"))

        from .calendar_writer import CalendarWriter, service_from_token
        from .timeutil import aware_now, local_now

        output = GenerationOutput.model_validate_json(raw)
        week_start = date.fromisoformat(output.week_start)
        tz = store.timezone()
        try:
            service = service_from_token(str(store.token_path))
            writer = CalendarWriter(service, state["calendar_id"], tz)
            window_start = aware_now(tz) - timedelta(hours=24)
            window_end = aware_now(tz) + timedelta(days=8)
            existing = writer.list_existing(window_start, window_end)
            report = reconcile(output.result.events, existing, week_start)
            written = 0
            for ev in report.accepted:
                writer.write_event(ev, week_start, provenance="generated")
                written += 1
        except Exception as e:  # noqa: BLE001
            flash(f"Write failed: {e}")
            return redirect(url_for("events"))

        store.update_state(last_week_start=output.week_start)
        flash(f"Wrote {written} event(s) to {state.get('calendar_label')!r}.")
        for v in report.rejected:
            flash(f"Skipped {v.event.title!r}: {v.decision.value} — {v.reason}")
        occurrences = [
            occ for ev in report.accepted for occ in expand_occurrences(ev, week_start)
        ]
        for note in coverage_check(occurrences, local_now(tz)):
            flash(f"Heads-up: {note}")
        flash(
            "Now verify in Kindroid that the events render to the kin and the hidden "
            "provenance metadata does not surface."
        )
        return redirect(url_for("events"))

    @app.post("/clear")
    @login_required
    def clear():
        state = store.state()
        if not state.get("calendar_id"):
            flash("No target calendar designated.")
            return redirect(url_for("settings"))
        from .calendar_writer import CalendarWriter, service_from_token

        try:
            service = service_from_token(str(store.token_path))
            writer = CalendarWriter(service, state["calendar_id"], store.timezone())
            deleted = writer.delete_all_tool_events()
        except Exception as e:  # noqa: BLE001
            flash(f"Cleanup failed: {e}")
            return redirect(url_for("events"))
        store.update_state(last_week_start=None)
        flash(
            f"Removed {deleted} event(s) this tool had created on "
            f"{state.get('calendar_label')!r}. Events from other sources were not touched."
        )
        return redirect(url_for("events"))

    # -- settings + google ------------------------------------------------------

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        if request.method == "POST":
            from .timeutil import is_valid_timezone

            changes = {}
            tz_value = request.form.get("timezone", "").strip()
            if tz_value:
                if is_valid_timezone(tz_value):
                    changes["timezone"] = tz_value
                else:
                    flash(
                        f"{tz_value!r} is not a recognized timezone — use an IANA name "
                        "like America/New_York or Europe/London."
                    )
            for form_key, state_key in [
                ("kindroid_api_key", "kindroid_api_key"),
                ("kindroid_ai_id", "kindroid_ai_id"),
            ]:
                value = request.form.get(form_key, "").strip()
                if value:
                    changes[state_key] = value
            if request.form.get("general_form"):
                changes["auto_refresh_enabled"] = bool(request.form.get("auto_refresh"))
            choice = request.form.get("calendar_choice", "")
            if choice:
                cal_id, _, label = choice.partition("|")
                changes["calendar_id"] = cal_id
                changes["calendar_label"] = label or cal_id
            if changes:
                store.update_state(**changes)
                flash("Settings saved.")
            return redirect(url_for("settings"))

        calendars = []
        google_error = None
        if store.token_path.exists():
            from .calendar_writer import list_calendars, service_from_token

            try:
                service = service_from_token(str(store.token_path))
                calendars = [
                    {
                        "id": c["id"],
                        "name": c.get("summary", "(unnamed)"),
                        "primary": bool(c.get("primary")),
                    }
                    for c in list_calendars(service)
                ]
            except Exception as e:  # noqa: BLE001
                google_error = str(e)
        state = store.state()
        return render_template(
            "settings.html",
            state=state,
            calendars=calendars,
            google_connected=store.token_path.exists(),
            google_error=google_error,
            google_env_ready=bool(
                os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")
            ),
            kindroid_key_set=bool(store.setting("kindroid_api_key", "KINDROID_API_KEY")),
        )

    def _google_flow(state: str | None = None, code_verifier: str | None = None):
        from google_auth_oauthlib.flow import Flow

        client_config = {
            "web": {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        kwargs: dict = {}
        if state:
            kwargs["state"] = state
        if code_verifier:
            # The callback runs in a fresh Flow object; hand back the PKCE
            # verifier generated at /google/connect or token exchange fails
            # with "invalid_grant: Missing code verifier".
            kwargs["code_verifier"] = code_verifier
            kwargs["autogenerate_code_verifier"] = False
        return Flow.from_client_config(
            client_config,
            scopes=GOOGLE_SCOPES,
            redirect_uri=url_for("google_callback", _external=True),
            **kwargs,
        )

    @app.get("/google/connect")
    @login_required
    def google_connect():
        if not (os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")):
            flash("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first (see README).")
            return redirect(url_for("settings"))
        flow = _google_flow()
        auth_url, oauth_state = flow.authorization_url(
            access_type="offline",
            # select_account: always show the account chooser — users with
            # multiple Google accounts must pick the one that owns the kin's
            # calendar, not whichever Google used last.
            prompt="consent select_account",
            include_granted_scopes="true",
        )
        session["oauth_state"] = oauth_state
        session["code_verifier"] = flow.code_verifier
        return redirect(auth_url)

    @app.get("/google/callback")
    @login_required
    def google_callback():
        flow = _google_flow(
            state=session.get("oauth_state"),
            code_verifier=session.pop("code_verifier", None),
        )
        try:
            flow.fetch_token(authorization_response=request.url)
        except Exception as e:  # noqa: BLE001
            flash(f"Google connection failed: {e}")
            return redirect(url_for("settings"))
        store.write_text(store.token_path, flow.credentials.to_json())
        flash("Google Calendar connected. Now pick the kin's calendar below.")
        return redirect(url_for("settings"))

    @app.post("/google/disconnect")
    @login_required
    def google_disconnect():
        store.token_path.unlink(missing_ok=True)
        store.update_state(calendar_id=None, calendar_label=None)
        flash("Google Calendar disconnected.")
        return redirect(url_for("settings"))

    # -- scanner -----------------------------------------------------------------

    @app.route("/scanner", methods=["GET", "POST"])
    @login_required
    def scanner():
        if request.method == "POST":
            action = request.form.get("action")
            if action == "enable":
                store.update_state(scanner_enabled=True)
                flash(
                    f"Scanner enabled — polls every {os.environ.get('KIN_POLL_MINUTES', '15')} "
                    "minutes while chat is active, backing off automatically when quiet."
                )
            elif action == "disable":
                store.update_state(scanner_enabled=False)
                flash("Scanner disabled.")
            elif action == "scan_now":
                from .scanner import log_scan, scan_once

                result = scan_once(store)
                log_scan(store, result)
                flash(f"Scan complete: {result.summary()}")
            elif action == "reset_cursor":
                store.update_state(scanner_cursor=None)
                flash("Cursor reset — the next scan reads from the beginning of available history.")
            return redirect(url_for("scanner"))

        state = store.state()
        return render_template(
            "scanner.html",
            state=state,
            interval=os.environ.get("KIN_POLL_MINUTES", "15"),
            max_interval=os.environ.get("KIN_POLL_MAX_MINUTES", "240"),
            configured=bool(
                store.setting("kindroid_api_key", "KINDROID_API_KEY")
                and store.setting("kindroid_ai_id", "KINDROID_AI_ID")
                and state.get("calendar_id")
            ),
        )

    return app
