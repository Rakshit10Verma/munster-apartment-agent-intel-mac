# Münster Apartment Agent

This is Rakshit Verma's safety-gated apartment discovery and first-contact tool for Münster. It
discovers listings, rejects deterministic hard mismatches before using AI, makes one structured
cloud-AI call for viable listings, validates the resulting message independently, prepares the
contact, and records every decision in SQLite.

The production path is cloud-only. The inherited llama.cpp/Gemma experiment is ignored.

## How Rakshit uses this

1. Run `./setup.sh` once.
2. Open `.env` locally. Add one or more provider keys, leave the provider order as
   `anthropic,openai,gemini`, and set `BEWERBERMAPPE_PATH` to an absolute PDF path. Never paste
   these values into chat or source code.
3. Run `./login.sh wg` and complete the normal WG-Gesucht login, CAPTCHA, or 2FA in the opened
   browser. If Gmail alerts/email are enabled, also run `./login.sh gmail` once.
4. Run `./doctor.sh`. It reports presence, never values, of secrets and performs a small live AI
   contract check when a key is available. Use `./doctor.sh --skip-ai` to avoid the API call.
5. Test a real WG-Gesucht listing with `./dry_run.sh 'https://www.wg-gesucht.de/...'`. The browser
   opens the listing, fills the composer, attaches the approved Bewerbermappe only when the
   legitimacy gate passes, takes a screenshot, and stops before Send.
6. Start continuous discovery with `./start.sh`; inspect it with `./status.sh`; stop it with
   `./stop.sh`.

Keep `DRY_RUN=true` and `AUTO_SEND=false` until real drafts and screenshots have been reviewed.
Actual sending requires both `DRY_RUN=false` and `AUTO_SEND=true`, plus every deterministic gate.

## Inputs and discovery

- WG-Gesucht saved-search alerts can be read through Gmail when `ENABLE_GMAIL=true`. Listing pages
  are revisited through the persistent browser profile to obtain visible current text. The tool
  never bypasses login, CAPTCHA, anti-bot controls, or rate limits.
- AStA Münster and na dann public pages are checked at low frequency. Adapters share one interface.
- A browser-based Kleinanzeigen saved search is available when `KLEINANZEIGEN_SEARCH_URL` is set;
  it uses the same no-bypass, persistent-session policy as WG-Gesucht.
- Put manual `.txt` listings or complete `SourceListing` JSON files into `data/inbox/`.
- Analyze one file with `python -m app.cli analyze --file FILE --platform wg_gesucht`.
- Export the SQLite record with `python -m app.cli export --output data/listings.csv`.

Gmail OAuth uses a local desktop-app credentials file (default `credentials.json`) and creates a
local token (default `token.json`). Both are ignored by Git. Email attachments are sent only when a
listing explicitly requests documents. WG-Gesucht attachments require a low-risk legitimacy
assessment. Other channels do not receive the Bewerbermappe automatically.

## Safety model

Deterministic parsing runs before AI and catches rent, duration, WBS, gender exclusion, hard age,
religious membership, clear scams, and configured warnings. For viable listings, provider fallback
defaults to Anthropic → OpenAI → Gemini. One response contains facts, scam assessment, hidden
questions/commands, decision, and final message. A provider that times out is briefly cooled down
during daemon operation so one outage does not add the same delay to every listing.

Before contact, code—not model confidence—checks all hard rules, exact command text, hidden-answer
IDs and known answer content, language/register, message length/style, forbidden personal wording,
listing-specific hooks, scam ambiguity, document policy, and duplicates. SQLite has a unique index
that permits at most one actual contact claim per listing, even across restarts.

## Operations

Useful commands:

```bash
./setup.sh
./doctor.sh
./login.sh wg
./login.sh gmail
./dry_run.sh URL
./start.sh
./status.sh
./stop.sh
./run_sample.sh
python -m app.cli run --once
python -m app.cli export --output data/listings.csv
```

Application events are JSON lines in `logs/agent.jsonl`; daemon stdout/stderr is in
`logs/daemon-console.log`; Playwright screenshots and failure HTML are under `logs/browser/`.

## Development verification

```bash
. .venv/bin/activate
python -m ruff format --check app tests
python -m ruff check app tests
python -m mypy app
python -m pytest -q
```

Live provider and WG-Gesucht smoke tests only run when their credentials/session are available.
Tests never contact or send to an unrelated real person.
