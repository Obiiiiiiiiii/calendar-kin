# Kin Life Calendar Generator

Gives a Kindroid AI companion ("kin") a believable offscreen life by generating
calendar events from its backstory and writing them to a **dedicated Google
Calendar** that the kin reads through Kindroid's built-in calendar integration.

The tool runs entirely outside Kindroid. It only generates events and writes
them — Kindroid handles reading the calendar, proactive-message timing, and
time awareness natively.

There are two ways to run it: a **web app you self-host** (e.g. on Railway —
no local Python needed) or a **command-line tool** on your own machine. Both
drive the same pipeline.

## Self-host on Railway (no local Python)

1. **Deploy.** Create a new Railway project from this GitHub repo. The included
   `Dockerfile` and `railway.toml` are picked up automatically.
2. **Add a volume** mounted at `/data` (Railway → service → Volumes). This is
   where the app keeps its state, the Google token, and the scanner's
   place-marker, so they survive redeploys.
3. **Set environment variables** (Railway → service → Variables):
   - `APP_PASSWORD` — pick one; it gates the whole web UI.
   - `ANTHROPIC_API_KEY` — from console.anthropic.com.
   - `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — see Google setup below.
   - `KIN_TIMEZONE` — your IANA timezone (e.g. `America/New_York`). All date
     reasoning ("tonight", "Thursday", the ±24h window) uses this, never the
     server clock (Railway runs UTC). Can also be set on the Settings page.
   - optional: `KIN_POLL_MINUTES` (scanner interval, default 15),
     `SECRET_KEY` (keeps logins across restarts).
4. **Google setup (one-time, ~10 min).** In Google Cloud Console: create a
   project → enable the **Google Calendar API** → configure the OAuth consent
   screen (External, add your own Google account as a test user) → create an
   **OAuth client ID of type "Web application"** with the authorized redirect
   URI set to `https://<your-railway-domain>/google/callback`. Copy the client
   ID and secret into the Railway variables above.
5. **Open the app.** Log in, then in **Settings**: Connect Google Calendar,
   pick the kin's target calendar (your main calendar is blocked on purpose),
   set your timezone, and — for the chat scanner — your Kindroid API key and
   the kin's AI id.
6. **Run the flow**: paste backstory → review spine → generate → review events
   → write. Then connect the calendar in Kindroid and verify the events render
   to the kin (and that the hidden provenance metadata does not surface).

### Auto-refresh (keeps the week from going stale)

After your first manual write, the app keeps the calendar textured on its
own: a few days before the generated week runs out, it regenerates a fresh
week from your **confirmed spine** and writes it through the same
reconciliation (standing commitments already on the calendar are skipped;
only new one-off "deltas" land). Toggle it on the Settings page. The spine
stays the human-reviewed source of truth — auto-refresh never re-extracts or
edits it, so revisit the spine when the character's life changes.

### Starting over

The Events page has a "Remove all events created by this tool" button. It
finds events by the hidden tool tag, so it can only ever delete what this
tool wrote — events from any other source are untouched.

### The chat scanner (phase 2)

Once Kindroid credentials are set, the **Chat scanner** page lets you enable a
background poll. Polling is adaptive to stay light on Kindroid's API: every
`KIN_POLL_MINUTES` (default 15) while chat is active, doubling after each
quiet check up to `KIN_POLL_MAX_MINUTES` (default 240); one new message snaps
it back to the fast rate, and the LLM is only invoked when there are new
messages. Each cycle reads new chat messages via Kindroid's
`/get-chat-messages` cursor (paging until caught up), detects plannable
future events the kin mentioned in plain dialogue ("I've got that gig
Thursday"), checks each candidate for semantic duplicates and clashes, and
writes survivors tagged `source = mentioned`. Chat is ground truth: a mentioned event displaces a
clashing generated one (the generated event is deleted). Low-confidence
detections are logged, not written. "Scan now" runs a single cycle for
testing.

> Note: the exact field names in Kindroid's chat-message payload are
> normalized defensively (`kindroid.py`) — verify against the live API on
> first run.

## Run locally instead (CLI)

## How it works

```
[backstory input] → [extraction] → [user review/confirm] → [generation] → [reconciliation] → [write to dedicated calendar]
   (swappable)                                                                   ↑
                                                  [reactive poller] reads chat, proposes events (source = mentioned)
```

1. **Extract** — an LLM reads the pasted backstory and produces a structured
   "life spine" (occupation, setting, standing commitments, daily rhythm),
   tagging every field `stated` / `inferred` / `unknown`.
2. **Review** — you edit the spine JSON. This is the character-fidelity
   safeguard: every generated event becomes asserted canon the kin treats as
   true, so confirm the spine before trusting generation.
3. **Generate** — a second LLM call turns the confirmed spine into a lean
   event list (standing commitments + availability-gating blocks + a few
   one-off deltas), plus an optional suggested routine note.
4. **Review again** — check the proposed week and the routine note.
5. **Reconcile + write** — mechanical checks (overlaps, exact duplicates, the
   20-event window budget) run in plain code, then accepted events are written
   to the dedicated calendar with provenance stamped in **private extended
   properties** (hidden from the kin).

## Setup

```sh
pip install -e ".[dev]"
```

You need:

- **`ANTHROPIC_API_KEY`** in the environment (see `.env.example`).
- **Google OAuth client secrets** — create an OAuth "Desktop app" client in
  Google Cloud Console with the Calendar API enabled, download it as
  `credentials.json` into the working directory. The first `write` run opens a
  browser to authorize and caches `token.json`.

## Usage

```sh
# 1. Backstory → spine (then review/edit spine.json; the CLI lists every
#    inferred/unknown field and open question to check)
kin-calendar extract backstory.txt -o spine.json

# 2. Confirmed spine → lean week of events (then review events.json)
kin-calendar generate spine.json -o events.json --week-start 2026-06-09

# 3. Pick which Google calendar the kin's life goes on
kin-calendar calendars                  # lists your calendars and their IDs

# 4. Reconcile + write to the calendar you designated
kin-calendar write events.json --calendar-id <id> --timezone America/New_York

# Or check what would happen without touching Google:
kin-calendar write events.json --dry-run
```

If you don't designate a calendar with `--calendar-id` (or `KIN_CALENDAR_ID`),
the tool finds or creates one named "Kin Life" (`--calendar-name` /
`KIN_CALENDAR_NAME`). Either way it refuses to write into your **primary**
calendar — the kin's fictional life must stay separate from your real one.

`generate` accepts `--weather "..."` to keep outdoor events out of bad weather.

## Design rules this encodes (from the build brief)

- **No metronome events.** The generation prompt forbids wake/meals/sleep
  events and dense routine enumeration; dense routine is abstracted into a
  single gating block.
- **Stay well under 20 events** in the −24h/+7d window (Kindroid randomly
  samples beyond that, hiding events). Reconciliation hard-stops at 18
  occurrences in the window and warns above ~3/day.
- **Self-attribution.** Every title/description is the kin's own activity in
  third person by name, because Kindroid frames the calendar as the user's.
- **Chat is ground truth.** Provenance (`generated` vs `mentioned`) is stamped
  from day one in Google Calendar private extended properties; the phase-2
  reconciler resolves conflicts by provenance (`mentioned` beats `generated`,
  newer beats older).
- **Never writes to the kin's profile.** The optional routine note is shown on
  the review step for *you* to keep/edit/place/ignore — it is never
  auto-written to Key Memories/backstory/directive, and the tool never calls
  Kindroid's `/update-info`.
- **All LLM output is untrusted until parsed.** Both calls use structured
  outputs validated against Pydantic schemas, with retry on malformed output;
  nothing unvalidated reaches the calendar API.
- **Swappable backstory source.** `kin_calendar/backstory.py` is the seam — a
  future Kindroid profile-read endpoint drops in as a new `BackstorySource`
  without touching anything downstream.

## First test (a kin with no chat history)

1. Run `extract` and confirm the `inferred`/`unknown` tagging is honest before
   trusting generation.
2. After `write`, verify in Kindroid that:
   - events render correctly to the kin (title, description, location, times);
   - at least one event falls in the past 24h **and** next 24h (the
     integration needs both to register — `write` warns if not);
   - the hidden provenance metadata does **not** surface in what the kin sees.
     This is an assumption to verify against the live integration before
     relying on it.

## Still out of scope

Recurring automatic re-generation of future weeks, and multi-*user* hosting
(one shared instance serving strangers). The self-host model sidesteps the
latter: each user deploys their own private copy with their own keys.

## Development

```sh
pytest          # mechanical logic (models, reconciliation) — no network needed
```
