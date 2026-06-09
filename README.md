# Kin Life Calendar Generator

Gives a Kindroid AI companion ("kin") a believable offscreen life by generating
calendar events from its backstory and writing them to a **dedicated Google
Calendar** that the kin reads through Kindroid's built-in calendar integration.

The tool runs entirely outside Kindroid. It only generates events and writes
them — Kindroid handles reading the calendar, proactive-message timing, and
time awareness natively.

## How it works

```
[backstory input] → [extraction] → [user review/confirm] → [generation] → [reconciliation] → [write to dedicated calendar]
   (swappable)                                                                   ↑
                                                  [reactive poller — PHASE 2] reads chat, proposes events
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

# 3. Reconcile + write to the dedicated calendar
kin-calendar write events.json --calendar-name "Kin Life" --timezone America/New_York

# Or check what would happen without touching Google:
kin-calendar write events.json --dry-run
```

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

## Phase 2 (described, not built)

A reactive poller will read new chat messages via Kindroid's
`GET /get-chat-messages` (with the `start_after_timestamp` cursor) and detect
natural-language mentions of plannable future events, tagging them
`source = mentioned`. Each candidate runs through the same reconciliation in
`kin_calendar/reconcile.py` — the semantic layer (`semantic_review`) is the
stub it fills in. Out of scope for the MVP alongside recurring auto-refresh
and multi-user hosting.

## Development

```sh
pytest          # mechanical logic (models, reconciliation) — no network needed
```
