# Münster Apartment Agent

This is Rakshit Verma's safety-gated apartment discovery and first-contact tool for Münster. It
discovers listings, rejects deterministic hard mismatches before using AI, makes one structured
cloud-AI call for viable listings, validates the resulting message independently, prepares the
contact, and records every decision in SQLite.

The production path is cloud-only. The inherited llama.cpp/Gemma experiment is ignored.

## How Rakshit uses this

1. Run `./setup.sh` once.
2. Open `.env` locally. Add one or more provider keys and leave the provider order as
   `anthropic,openai,gemini`. `BEWERBERMAPPE_PATH` is optional and is used only for email;
   WG-Gesucht uses the Bewerbermappe already stored in the account. Never paste keys into chat or
   source code.
3. Run `./login.sh wg` and complete the normal WG-Gesucht login, CAPTCHA, or 2FA in the opened
   browser. If Gmail alerts/email are enabled, also run `./login.sh gmail` once.
4. Run `./doctor.sh`. It reports presence, never values, of secrets and performs a small live AI
   contract check when a key is available. Use `./doctor.sh --skip-ai` to avoid the API call.
5. Test a real WG-Gesucht listing with `./dry_run.sh 'https://www.wg-gesucht.de/...'`. The browser
   checks that no conversation exists, verifies the actual WG+ message-priority feature, fills the
   composer, selects and verifies “Meine Bewerbermappe,” takes a screenshot, and stops before Send.
6. Start continuous discovery with `./start.sh`; inspect it with `./status.sh`; stop it with
   `./stop.sh`.

For the first controlled production send only, explicitly set `AUTO_SEND=true` in `.env`, keep the
daemon stopped, and run `./send_once.sh 'WG_GESUCHT_URL'`. The command accepts exactly one HTTPS
WG-Gesucht URL and refuses to start unless `AUTO_SEND=true`. It scopes `DRY_RUN=false` to that one
foreground process, then runs the same deterministic filters, AI/universal fallback, validator,
WG+ priority, composer, account Bewerbermappe, final gates, sent-state verification, and SQLite
logging used by production. It exits nonzero unless WG-Gesucht confirms the message was sent.

If sent-state verification cannot positively confirm a Send click (rare, but possible if
WG-Gesucht's page changes), the listing is recorded as `send_state_unknown`, never as a plain
failure, and is automatically locked from any further automatic Send attempt. Recover from this
state with `./reconcile_send.sh 'WG_GESUCHT_URL'`, which only reads the conversation page — it can
never click Send — and updates SQLite to `sent`, `not_sent`, or leaves it `send_state_unknown` if
the page still is not conclusive.

Keep `DRY_RUN=true` and `AUTO_SEND=false` until real drafts and screenshots have been reviewed.
`DRY_RUN=true` is an absolute gate: nothing ever sends for real regardless of `AUTO_SEND` or how a
send was triggered. Once `DRY_RUN=false`, `AUTO_SEND` only controls the **daemon's own autonomous**
sending — `AUTO_SEND=false` means "no autonomous send," not "no sending at all": an explicit human
approval (`./approve.sh`, a confirmed Telegram Send, or the dashboard's Approve & Send) may still
send for real with `DRY_RUN=false` even while `AUTO_SEND=false`, after its own confirmation step and
every other deterministic gate. `./send_once.sh` additionally still requires `AUTO_SEND=true` itself
as an extra script-level precaution for a first controlled send.
Keep `WG_USE_PREMIUM_BOOST=true`. WG+ priority is attempted before every send, but it is a bonus
feature, not a hard requirement: by default (`WG_PREMIUM_STRICT=false`) an unverifiable boost only
records a `premium_boost_unverified` warning and the application still proceeds through every other
gate — a missed or unprovable boost should never cost a good apartment. Set `WG_PREMIUM_STRICT=true`
to block sending instead whenever priority cannot be positively verified.

## Inputs and discovery

- A near-real-time WG-Gesucht search watcher polls the configured search result page(s) using the
  same logged-in persistent browser profile as everything else — no separate login. It never
  redesigns the existing per-listing pipeline (extraction → filters → AI/fallback → validation →
  WG+/Bewerbermappe → send/logging); it only feeds newly discovered listing URLs into it as soon as
  they appear, instead of waiting on Gmail's email delay. Configure one or more searches in
  `config.yaml` under `wg_watch.searches` (each a `name`/`url` pair); `WG_SEARCH_URL` in `.env`
  remains a legacy single-search fallback used only if that list is empty. Polling interval is
  randomized between `WG_WATCH_MIN_SECONDS` and `WG_WATCH_MAX_SECONDS` (default 30–45s) and backs
  off automatically on navigation errors, repeated errors, and rate-limiting; if a CAPTCHA, 2FA
  prompt, or expired session is detected, the watcher pauses that search as
  `human_verification_required` and never attempts to bypass it — run `./login.sh wg` and it
  recovers on the next check. On first run for a given search, all currently visible listings are
  recorded as a baseline and never enqueued; only listings that appear after that baseline are
  ever treated as new, so restarting discovery never causes a burst of contact attempts against
  pre-existing results. Run `./watch_once.sh` for a single, read-only discovery cycle that reports
  what it saw (baseline vs. new) without ever analyzing, drafting, or contacting anything; `./status.sh`
  shows a live watcher block (running state, per-search last/next check, last new listing, listings
  discovered today, queue depth, and whether human verification or backoff is active).
- WG-Gesucht saved-search alerts can be read through Gmail when `ENABLE_GMAIL=true`. Listing pages
  are revisited through the persistent browser profile to obtain visible current text. The tool
  never bypasses login, CAPTCHA, anti-bot controls, or rate limits.
- AStA Münster and na dann public pages are disabled by default (`ENABLE_ASTA=false`,
  `ENABLE_NA_DANN=false`) while WG-Gesucht is made extremely reliable first; the adapters are
  unchanged and still share one interface, so setting either flag to `true` turns that source back
  on without any other changes.
- A browser-based Kleinanzeigen saved search is available when `KLEINANZEIGEN_SEARCH_URL` is set;
  it uses the same no-bypass, persistent-session policy as WG-Gesucht.
- Put manual `.txt` listings or complete `SourceListing` JSON files into `data/inbox/`.
- Analyze one file with `python -m app.cli analyze --file FILE --platform wg_gesucht`.
- Export the SQLite record with `python -m app.cli export --output data/listings.csv`.

Gmail OAuth uses a local desktop-app credentials file (default `credentials.json`) and creates a
local token (default `token.json`). Both are ignored by Git. Email attachments use the optional
local `BEWERBERMAPPE_PATH` only when a listing explicitly requests documents. WG-Gesucht never
uploads that local file: after a low-risk legitimacy assessment, Playwright selects the existing
account Bewerbermappe separately for each message and requires a positive attached-state signal.
`APPLICANT_PHOTO_PATH` is a separate absolute JPG/PNG path. The photo is never included by default:
only a listing that explicitly requires a current photo with the first application triggers a
pre-browser file check and a separate WG-Gesucht Foto/Datei upload. The browser must see the photo
filename in an attached-file element before Send; an upload call alone is insufficient. If the
photo is missing or WG-Gesucht does not visibly confirm it, the listing stays in review and no
Send click occurs. The configured applicant profile confirms four Münster MSc semesters, no pets
(but comfort with pets and a liking for dogs), private liability insurance, and own Werkstudent
income with a salary slip in the Bewerbermappe. It does **not** confirm an Elternbürgschaft: an
explicit parental-guarantor requirement stays in review unless the listing accepts income proof
as an alternative. Availability starting in April 2027 or later is a timing mismatch and is
filtered before AI. Existing historical DB rows are not automatically reprocessed by this change.

## Safety model

Deterministic parsing runs before AI and catches rent, duration, WBS, gender exclusion, religious
membership, clear scams, and configured warnings. For viable listings, provider fallback
defaults to Anthropic → OpenAI → Gemini. Timeouts default to 30 seconds and can be overridden with
`ANTHROPIC_TIMEOUT_SECONDS`, `OPENAI_TIMEOUT_SECONDS`, and `GEMINI_TIMEOUT_SECONDS`; OpenAI has a
20-second safety floor. One response contains facts, scam assessment, hidden
questions/commands, decision, and final message. A provider that times out is briefly cooled down
during daemon operation so one outage does not add the same delay to every listing.

A clearly priced one-time fee, furniture takeover, or accessories takeover is accepted when the
combined known total is strictly below €500. A total of €500 or more requires review. A furniture or
inventory takeover mentioned without any price at all (ordinary, negotiable WG practice — "Abschlag
je nachdem was du übernimmst") is not treated as review-worthy by itself and never implies the
applicant agreed to pay anything; only a genuinely arbitrary, non-property fee (e.g. an unpriced
processing/reservation fee) still requires review when unpriced. This never excuses suspicious
payment methods or a demand to pay before viewing; those retain the normal scam review or hard-skip
behavior.

An explicit Zwischenmiete (temporary sublet) is judged by a maximum duration, not a minimum: it is
accepted when the stated (or move-in/end-date-derived) duration is 6 months or less, or regardless
of duration when the warm rent is below €400. A longer Zwischenmiete above that price is a hard
skip. Ordinary long-term listings keep the unrelated general minimum-duration requirement.

A WG age range such as “25–35 gesucht” is always a soft preference, even when it is stated as
mandatory/exclusive/without exceptions and even when Rakshit is outside it — age mismatch is never a
hard skip, never forces `review_required`, and never withholds an application on its own; only
`age_requirement_strength` (informational) records how strictly the listing worded it. The message
acknowledges that he is younger and reassures the WG that he'd still fit in — exactly once, in a
natural spoken clause rather than a list of traits; deterministic code strips any age-mismatch
sentence a cloud provider wrote on its own and replaces it with a single canonical sentence, and the
validator independently blocks a draft that mentions the age mismatch, or repeats the same
personality-trait listing (e.g. “ruhig, verlässlich und ordentlich”), more than once. Register is
selected from the application context: current-flatmate, student, and
private-user WG listings use du/ihr and the WG profile; genuine landlords, Hausverwaltung, and
property companies use the formal Sie profile, where polished application language is expected —
elsewhere the validator blocks generic cover-letter phrasing (“ich bringe die nötige Reife mit”,
“ich erfülle die Anforderungen”). Informal WG drafts use the simple, warm tone of the early test
messages and avoid manufactured slang or repeated canned phrases. Length is optimized for sounding
natural, not for hitting an exact count: a typical WG draft runs 120–180 words, gets a mild warning
only above 180 without real content behind the extra length, a stronger one above 210, and is
blocked only above roughly 220 words (240 when the listing has hidden questions or an age mismatch
genuinely justifying more detail) or when it is repetitive, generic, or duplicates a fact instead of
just running a little long.

If every provider fails (including timeouts and exhausted API credit), a viable low-risk listing
uses a deterministic, config-driven fallback composer. It selects separate natural templates for a
WG room, studio, whole apartment, larger share-later apartment, one-month-or-longer Zwischenmiete,
or eligible student housing; it also adapts register, WG/landlord closing, furnishing, listing
hooks, age-fit note, and known hidden-question answers. Personal facts come from `config.yaml` and
`answer_bank.yaml`; missing facts are omitted rather than invented. Unknown questions, commands
that cannot be verified mechanically, medium/high scam risk, unresolved eligibility, and every
normal hard skip still stop at review/skip. Every fallback passes the same validator and WG+
priority, Bewerbermappe, composer, duplicate, and final send gates as an AI draft.

SQLite records `message_source=universal_fallback` (or
`universal_answer_bank_fallback`) plus whether AI was attempted, its failure reason, the selected
fallback scenario/template, optional clauses, deterministic hidden-question answers, and the final
message. Existing databases receive the new `generation_trace_json` column automatically when the
tool next starts; no manual migration command is required.

Before contact, code—not model confidence—checks all hard rules, exact command text, hidden-answer
IDs and known answer content, language/register, message length/style, forbidden personal wording,
listing-specific hooks for AI drafts, scam ambiguity, document policy, and duplicates. WG action
state comes from link destinations/component state rather than rotating text. An existing
conversation is saved as `already_contacted` before AI runs. SQLite has a unique index that permits
at most one actual contact claim per listing, even across restarts; the claim occurs only after all
browser-side gates pass, and the exact message/fingerprint attempted is persisted immediately after
the Send click, before waiting for confirmation, so a browser or process crash mid-verification
cannot lose the record or cause a resend.

WG+ message-priority verification distinguishes how far activation actually got —
`premium_entry_found`, `premium_priority_available`, `premium_priority_activated`, or
`premium_priority_verified` — instead of collapsing everything into one state. Rotating
WG-Gesucht+ marketing copy and a merely plausible account-wide entitlement are never treated as
proof; only `premium_priority_verified` counts as full success. By default
(`WG_PREMIUM_STRICT=false`) anything less is recorded as a `premium_boost_unverified` warning and
sending still proceeds if every other gate passes; set `WG_PREMIUM_STRICT=true` to block sending
instead whenever priority is not positively verified. WG-Gesucht intermittently shows a
security/privacy/policy popup after navigating or after clicking the Premium or message actions;
`dismiss_or_accept_policy_modal_if_present` detects and accepts it when present and is a no-op when
it is not, scoped strictly to an actual cookie/consent/privacy/security-labeled modal so it can
never click WG's own Premium popup or any other unrelated page element.

After a Send click, sent-state confirmation combines several independent, structural signals
(conversation view, a fingerprint of the exact sent text reappearing in the page, the Bewerbermappe
showing attached, an emptied composer, a state transition to an existing conversation) rather than
depending on one exact marker or German wording. If none of that adds up to confident confirmation,
the listing is recorded as `send_state_unknown` — never as a plain failure — and is locked from any
further automatic Send attempt; `./reconcile_send.sh` can later resolve it to `sent` or `not_sent`
by reading the conversation without ever clicking Send.

Every German WG draft ends with the configured Berlin/video-call/viewing paragraph and sign-off
verbatim. This wording is applied deterministically after cloud generation and checked again by the
validator; WG message text never claims that the separately selected Bewerbermappe is attached.

## Human review queue

Anything the deterministic pipeline could not resolve on its own waits in SQLite rather than being
guessed at. `./review.sh` lists every such listing grouped by status (REVIEW REQUIRED, SEND STATE
UNKNOWN, AI FAILED, PREMIUM BOOST FAILED, DRAFTED BUT NOT SENT) with its rent, decision, the reason
it needs attention, and its URL; `./review.sh <DB_ID>` shows the full detail for one listing —
listing facts, the rule decision, hidden questions/commands and whether each was actually resolved,
the exact drafted message, every send-gate result, and the latest screenshot if one exists. Both are
read-only.

`./approve.sh <DB_ID>` shows that exact message and the current send mode, then asks
`Send this application? [y/N]` — the default is No, and only an explicit `y` proceeds. Approval
routes the listing back into the same `contact_listing`/browser pipeline used everywhere else, so
every hard gate (Bewerbermappe, Premium, composer match, the one-actual-contact-per-listing SQLite
constraint, sent-state verification) still applies exactly as it would for `send_once.sh`; the human
approval only stands in for the soft "needs a decision" state, and cannot override a genuine
deterministic hard skip, a high scam-risk finding, or a missing draft — those still block approval
outright, without even prompting. `send_state_unknown` listings can never be approved this way (a
Send may already have happened); trying prints an explicit refusal pointing at `./reconcile.sh`
instead. `./reject.sh <DB_ID>` asks for confirmation and marks a listing `manually_rejected`; the
existing permanent dedup record means it is never reprocessed even if it reappears in search
results.

`./reconcile.sh` lists every `send_state_unknown` listing; `./reconcile.sh <DB_ID>` reconciles one
by DB id using the same read-only, never-clicks-Send logic as `./reconcile_send.sh` (which still
takes a URL directly and is unchanged). `./status.sh` shows a one-line "Human attention" summary
(counts only, pointing at `./review.sh`) whenever anything is waiting.

`./reprocess.sh <DB_ID>` re-runs the normal decision/generation pipeline on a listing's own stored
text, so a row affected by a since-fixed rule/validator/fallback bug can be safely re-evaluated
without re-fetching the listing. It only re-drafts and never sends; run `./approve.sh` afterwards
if the new result looks good. `sent`, `already_contacted`, and `send_state_unknown` are always
refused (a settled or ambiguous send state must never be silently reanalyzed); `manually_rejected`
requires `--force` since a human already made that call once. `--fallback-only` makes zero
cloud-provider calls and exercises exactly the production deterministic-fallback path, so it can be
tested against a stored listing without depending on a real provider outage.

`./reset_run.sh` clears old processing clutter (`discovered`, `filtered_skip`, `review_required`,
`drafted`, `ai_failed`, `premium_boost_failed`, `dry_run_ready`, a reconciled `not_sent`, and their
draft messages/events) so `./status.sh`, `./review.sh`, and the dashboard start clean for a new run.
It never clears a listing that is `sent`, `already_contacted`, `send_state_unknown`, a
`manually_rejected` decision, or has any `contact_attempts` row showing Send was actually clicked
(including a process-crash-before-verification case) — regardless of that listing's own status
label; only that click-level evidence is the authoritative duplicate-send signal, not merely having
attempted an actual-mode contact (e.g. a Premium/Bewerbermappe failure that never reached Send is
not a duplicate-send risk and is safely cleared). It takes a timestamped backup under `backups/`
first, shows a preview, and requires explicit confirmation (default: No); the whole reset is one
transaction, so a failure rolls back with nothing deleted. `./status.sh` afterward shows "Current
run" (should read empty or near-empty) separately from "Preserved contact history" (never
sacrificed for a clean-looking count). Restore a backup by stopping the daemon and copying it back
over `data/apartment_agent.sqlite`.

Pass `--baseline-current` to also record the currently visible WG-Gesucht search results as the
watcher's new "already seen" baseline immediately after the reset, so the next scan only treats
listings appearing *after* that point as newly discovered — without analyzing or contacting any of
the currently visible ones. This is the one case that opens the browser; without the flag,
`./reset_run.sh` only ever touches SQLite. Run `./baseline_watcher.sh` on its own for the same
re-baselining step independent of a full reset.

## Telegram mobile notifications/controls

Optional; disabled by default and the agent behaves identically without it. Telegram is only a
mobile control/notification surface — SQLite and the same functions behind `approve.sh`/
`reject.sh`/`reconcile.sh` remain the source of truth and the only place any decision is actually
made. A Telegram failure (timeout, invalid token, rate limit, network down) never blocks discovery,
analysis, drafting, sending, or send verification; it is only ever logged.

Setup:

1. Message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`, and copy the token it gives
   you.
2. Message [@userinfobot](https://t.me/userinfobot) to get your numeric Telegram user id.
3. Message your new bot once (anything) so it can see your chat; your chat id is the same as your
   user id for a private 1:1 chat.
4. Set in `.env`:
   ```
   TELEGRAM_ENABLED=true
   TELEGRAM_BOT_TOKEN=<token from BotFather>
   TELEGRAM_ALLOWED_USER_ID=<your numeric user id>
   TELEGRAM_CHAT_ID=<same id for a private chat>
   ```
5. Run `./telegram_bot.sh` (its own long-polling process, independent of `./start.sh`;
   `nohup ./telegram_bot.sh >> logs/telegram-console.log 2>&1 &` to run it in the background).

Only `TELEGRAM_ALLOWED_USER_ID` in `TELEGRAM_CHAT_ID` can trigger anything; every other user/chat is
silently ignored. The daemon notifies (at most once per listing per state — no repeat spam across
watcher cycles) for `review_required`, `drafted`, `ai_failed`, `premium_boost_failed`,
`bewerbermappe_attachment_failed`, `send_state_unknown`, `already_contacted`, and `sent`; never for
`filtered_skip`. Each notification shows the listing's rent/size/type/availability/age fit, status, a
short human-readable reason, whether cloud AI or the deterministic fallback drafted it, and a message
preview — never a raw internal debug trace (see `./review.sh <id>` for that).

Buttons are always state-aware, rendered fresh from the current SQLite row every time a message is
sent or edited — never from stale notification content. `✅ Send`/`❌ Reject` only ever appear for a
status in `APPROVABLE_STATUSES` (`review.py`'s own approval-gate allowlist: `review_required`,
`drafted`, `ai_failed`, `premium_boost_failed`); every other status (`sent`, `already_contacted`,
`manually_rejected`, `filtered_skip`, `send_state_unknown`, or a reconciled `not_sent`) shows Open
Listing only (`send_state_unknown` additionally gets Reconcile), so a listing that was already sent or
already has an existing WG-Gesucht conversation can never be offered Send again. After every
callback/action the message is re-read from the DB and refreshed, so an impossible action can never
survive a state change. `already_contacted` and `sent` get a concise confirmation
(e.g. "✅ DB 71 was already contacted earlier. An existing WG-Gesucht conversation was detected, so no
duplicate application was sent.") instead of the raw DOM detection string, which stays in
`./review.sh <id>` only.

Buttons: **Open Listing** links straight to the stored WG-Gesucht URL and never touches SQLite.
**✅ Send** never sends on the first tap — it only opens a **✅ Confirm Send / ↩ Cancel** prompt;
only Confirm Send re-reads the listing fresh, re-checks every safety gate, and calls the exact same
`approve_listing`/`contact_listing` pipeline as `./approve.sh` and `AUTO_SEND`, including live
verification through WG-Gesucht Messages. `sent`/`already_contacted`/`send_state_unknown`/
`manually_rejected` always refuse (a stale button tap can never resend); a confirmation also expires
after 10 minutes. **❌ Reject** calls the same `reject_listing` used by `./reject.sh` and can never
overwrite a settled/ambiguous send state. A `send_state_unknown` listing gets **🔍 Reconcile**
instead of Send — the same read-only, never-clicks-Send reconciliation as `./reconcile.sh`. Text
commands `/review <id>`, `/send <id>`, `/reject <id>`, `/reconcile <id>`, `/status` mirror the
buttons (`/send` still requires the same confirmation step).

## Local review dashboard

Run `./dashboard.sh`, then open `http://127.0.0.1:8765`. The deliberately simple local dashboard
reads the same SQLite database and exposes filters for review-required, unknown-send-state,
drafted, sent, filtered, Premium-failed, AI-failed, and all records. Its detail page shows the
original listing, extracted facts, decision/review reasons, age mismatch, hidden questions and
commands, AI/fallback trace, exact outgoing message, Bewerbermappe state, live/persisted send gates,
and event/contact history.

Opening a page is always read-only. Approve & Send has a separate confirmation page showing the
exact final message and requires an explicit checkbox/POST; after confirmation it calls the same
`contact_listing` pipeline as `./approve.sh`, so `DRY_RUN`/`AUTO_SEND`, browser gates, and SQLite
deduplication remain authoritative. Reject also requires explicit confirmation. Reconcile calls
only the existing read-only conversation inspector and cannot click Send. An unknown send state is
blocked from approval in both the web UI and CLI. Stop the dashboard with `Ctrl-C` in its terminal.

## Read-only audit export

Run `./audit_export.sh` to create a timestamped, sanitized review bundle under `audit_exports/`
plus a ZIP of the same bundle. Run `./audit_export.sh <DB_ID>` for a compact bundle containing only
one listing and its associated database events, contact attempts, and structured log entries. The
exporter opens SQLite using `mode=ro` and `PRAGMA query_only=ON`; it does not instantiate the normal
database wrapper, analyze/reprocess listings, open a browser, reconcile, or import any sending
path.

Each bundle contains status/counts, a flattened `listings.csv`, one A–J lifecycle Markdown file per
listing, chronological events, contact attempts, watcher state, the database schema, bounded
sanitized logs, safe decision/profile configuration, current runtime gates, a manifest, and a
README describing evidence gaps. It excludes `.env`, API keys, credentials, OAuth tokens, cookies,
browser profiles/storage, Bewerbermappe files and private document paths, and authenticated browser
screenshots/HTML. Audit exports are gitignored and created with owner-only permissions.

## Operations

Useful commands:

```bash
./setup.sh
./doctor.sh
./login.sh wg
./login.sh gmail
./dry_run.sh URL
./watch_once.sh
./send_once.sh 'WG_GESUCHT_URL'
./reconcile_send.sh 'WG_GESUCHT_URL'
./review.sh
./review.sh <DB_ID>
./approve.sh <DB_ID>
./reject.sh <DB_ID>
./reconcile.sh
./reconcile.sh <DB_ID>
./reprocess.sh <DB_ID>
./reprocess.sh <DB_ID> --fallback-only
./reset_run.sh
./reset_run.sh --baseline-current
./baseline_watcher.sh
./telegram_bot.sh
./dashboard.sh
./audit_export.sh
./audit_export.sh <DB_ID>
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
