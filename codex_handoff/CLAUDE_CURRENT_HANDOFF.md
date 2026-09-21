# Münster Apartment Agent — Current Claude Handoff

Last updated: 2026-09-15

## Purpose of this handoff

You are taking over this repository as the implementation owner. The original project requirements and architectural intent are documented in:

- `codex_handoff/AGENTS.md`
- `codex_handoff/CODEX_MASTER_HANDOFF.md`
- `README.md`

This document describes the repository's **current** implementation, later corrections, verified behavior, live-run findings, and outstanding limitations. When this current-state handoff conflicts with an older handoff description, this document wins for facts about what is now implemented. Explicit user requirements always win.

Before changing anything, read all three files above and this file completely, then inspect `git status`, `git diff`, and the relevant source/tests. The worktree intentionally contains substantial uncommitted implementation work. Do not reset, discard, or overwrite it.

## Safety boundary

The application contacts real WG-Gesucht pages, so preserve these invariants:

- Real sending is disabled by default.
- `.env` currently has `DRY_RUN=true` and `AUTO_SEND=false`.
- Never send unless the user explicitly changes `AUTO_SEND=true` and invokes the intended production path.
- Never ask the user to paste API keys into chat or source code.
- Do not print, copy, or commit `.env`, browser storage state, OAuth material, or private Bewerbermappe paths/content.
- The user handles only API keys in `.env`, any absolute local Bewerbermappe path, login/OAuth/CAPTCHA/2FA, and explicitly enabling a real send.
- WG-Gesucht uses the account Bewerbermappe. The message body must not claim that the Bewerbermappe is attached.
- Gmail is currently disabled.

Safe operational configuration currently includes:

```dotenv
AUTO_SEND=false
DRY_RUN=true
AI_PROVIDER_ORDER=anthropic,openai,gemini
AI_TIMEOUT_SECONDS=30
ANTHROPIC_TIMEOUT_SECONDS=30
OPENAI_TIMEOUT_SECONDS=30
GEMINI_TIMEOUT_SECONDS=30
AI_PROVIDER_COOLDOWN_SECONDS=300
WG_USE_PREMIUM_BOOST=true
WG_PREMIUM_STRICT=true
BROWSER_HEADLESS=false
ENABLE_GMAIL=false
```

Do not copy secret values into documentation. `.env` is ignored by git.

## Repository state

The last committed baseline observed was:

```text
67d202c feat: finish cloud-first apartment agent
```

The following current worktree changes are intentional project work and must be preserved:

```text
 M .env.example
 M README.md
 M app/analyzer.py
 M app/browser.py
 M app/cli.py
 M app/config_loader.py
 M app/contact.py
 M app/daemon.py
 M app/database.py
 M app/doctor.py
 M app/documents.py
 M app/prefilter.py
 M app/prompts.py
 M app/providers.py
 M app/rules.py
 M app/schemas.py
 M app/validator.py
 M config.yaml
 M tests/conftest.py
 M tests/test_browser_dry_run.py
 M tests/test_documents.py
 M tests/test_pipeline_database.py
 M tests/test_prefilter_rules.py
 M tests/test_providers.py
 M tests/test_validator.py
?? app/message_policy.py
?? app/universal_fallback.py
?? send_once.sh
?? tests/fixtures/wg_14096490.yaml
?? tests/test_age_register_length_regression.py
?? tests/test_message_policy.py
?? tests/test_send_once.py
```

Re-run `git status --short` because the list may have changed after this handoff was generated. Do not assume an untracked file is disposable.

## Verified status

The latest completed full verification after the current changes was:

- `pytest`: **190 passed in 92.96s**
- Ruff formatting check: passed
- Ruff lint: passed
- mypy for `app`: passed
- shell syntax checks: passed
- `git diff --check`: passed
- doctor previously passed using the cloud-only path, configured provider keys, Chrome, saved WG session, strict WG Premium checks, and SQLite

The daemon was not running at the last check.

SQLite records one actual contact for listing 14096490 as confirmed sent on 2026-09-15. Keep the safety flags disabled unless the user explicitly requests another controlled real-send operation.

## Architecture and important files

### Pipeline orchestration

`app/analyzer.py`

`process_listing` performs the production decision pipeline:

1. deterministic prefilter and hard skips;
2. existing-conversation protection;
3. cloud AI provider routing;
4. constrained universal fallback only if every provider fails;
5. fact reconciliation and rules;
6. attachment decision;
7. deterministic message policy;
8. deterministic validator;
9. database persistence and preparation for browser contact.

### CLI

`app/cli.py`

Commands include `doctor`, `analyze`, `run`, `login`, `status`, and `export`. The one-shot send wrapper uses a hidden `--require-sent` behavior so it exits unsuccessfully unless browser verification confirms a sent state.

### Deterministic extraction and filters

`app/prefilter.py`

It normalizes/extracts rent, housing type, availability dates, duration, women-only restrictions, WBS, age constraints, religious/confessional/fraternity restrictions, scam/payment signals, hidden questions, and exact commands.

Important: hard filters execute before any AI call. A universal fallback can never bypass them.

### AI providers

`app/providers.py`

- Production hot path is cloud-only.
- Local llama.cpp/Gemma is intentionally disabled because it was too slow and produced poor German.
- Default configurable fallback order is Anthropic → OpenAI → Gemini.
- Timeouts are configurable globally and per provider.
- Preserve retry/fallback/cooldown behavior.
- OpenAI timeout must not be below the observed doctor latency of roughly 16.2 seconds; the present default is 30 seconds.

### Universal fallback

`app/universal_fallback.py`

The approved universal template fallback is implemented. It runs only when:

- the listing already passed every deterministic hard filter;
- all configured cloud providers failed or timed out;
- the source is an ordinary WG-Gesucht listing with enough reliable context;
- scam/document risk is low;
- there are no hidden or unclassified questions;
- there are no exact commands;
- there are no special application or subject instructions;
- there is no unresolved hard-condition or eligibility ambiguity.

The fallback selects German/English and du/Sie deterministically where clear, then proceeds through the same validator, Premium, Bewerbermappe, final DOM, contact-claim, send, and verification gates as an AI-generated message. It is persisted as:

```text
message_source=universal_fallback
```

If a question, command, special requirement, material ambiguity, or scam/document concern exists, provider exhaustion yields REVIEW rather than fallback.

### Message policy

`app/message_policy.py`

This applies deterministic register, age-note, and closing policy after generation. It is security/safety-relevant and should not be weakened casually.

### Validator

`app/validator.py`

It enforces rules, hidden-question IDs and answers, exact commands, scams, critical ambiguity, factual consistency, language/register, document policy, length policy, and the required viewing/closing block.

### Browser automation

`app/browser.py`

The Playwright implementation:

- loads the saved WG session;
- extracts the primary listing and every hidden description tab matching `[id^=freitext_]`;
- explicitly excludes `#similar_ads_swiper` so recommended listings cannot inject unrelated constraints such as “Frauen-WG”;
- distinguishes new, already-contacted, and unavailable states;
- verifies the WG Plus/Premium entitlement;
- opens and fills the message composer;
- attaches the account Bewerbermappe;
- performs final DOM gates;
- stops before Send in dry-run;
- clicks Send only when all production gates permit it;
- verifies the post-send state.

### Contact idempotency

`app/contact.py` and `app/database.py`

Immediately before an actual Send click, SQLite acquires a unique actual-contact claim. Database migrations include `message_source`, and actual/dry-run contact records are distinct. This is a final duplicate-send defense and must remain immediately adjacent to the actual sending path.

### Documents

`app/documents.py`

- WG-Gesucht uses the user's account Bewerbermappe.
- A local PDF is for email only and is optional while Gmail is disabled.
- The WG message text must not say the Bewerbermappe is attached.

### Daemon

`app/daemon.py`

The daemon behavior was intentionally not changed by the one-shot send command.

## Current product rules

### Deterministic hard skips

At minimum, the pre-AI path rejects:

- women-only listings for this applicant;
- WBS-required listings;
- a single room above €550 warm;
- duration below six months;
- mandatory religious/confessional membership or fraternity requirements;
- high scam risk or prohibited payment/document behavior;
- genuinely strict incompatible age requirements;
- other unambiguous mandatory eligibility failures.

### Age constraints

This policy was corrected after a live listing exposed over-filtering:

- Generic ranges such as `25–35`, `between 25 and 35`, approximate wording, and `am liebsten` are soft preferences, not automatic hard skips.
- Extracted age data includes minimum, maximum, strength, and mismatch state.
- A soft mismatch should add a brief, natural note reassuring the WG about maturity, calmness, reliability, and orderliness.
- Only explicit mandatory forms such as `Nur ab 30, keine Ausnahmen`, `Mindestalter 30 zwingend`, or `Bitte nur Personen ab 30 anschreiben` should hard-skip an incompatible applicant.

### Register and applicant profile

- A WG/current-flatmate/student/private-WG context uses `du/ihr` and the WG applicant profile.
- A genuine landlord, Hausverwaltung, or company uses formal `Sie` and the formal profile.
- Do not trust an AI/account label alone when the page context clearly indicates a WG.
- A fixed prior bug incorrectly interpreted text like `Frau zwischen ...` as a named female landlord. Keep the context-based classifier regression coverage.

### Message length

For WG messages:

- optimize for naturalness and listing relevance rather than an exact count;
- typical preferred range: 120–180 words;
- below 100 words: block;
- 180–195 words is acceptable when the content earns its length through a real hook, hidden-question answer, age/fit clarification, and the mandatory viewing block;
- above 210 words: warn and consider removing redundant or generic content;
- above 220 words: normally block, with a ceiling of 240 when a hidden question or soft age mismatch provides a genuine reason;
- the approved universal template has a specific exception up to 300 words.

The validator also blocks repeated personality-trait lists and polished cover-letter phrases in informal WG messages. Prefer natural spoken German and do not shorten a message merely to hit a number if that makes it abrupt or generic. For genuinely formal messages, the configured target remains 90–140 words. Any explicit listing-specific shorter command still takes precedence.

### Informal WG tone

The user prefers the simple, warm tone of the initial test/dry-run messages. Do not manufacture casualness by injecting the same phrases into every draft. In particular, avoid repeated canned wording such as `Ich hab gesehen`, `klingt für mich echt gut`, and `bin da ziemlich unkompliziert`. Use a direct greeting, one or two real listing-specific details, a compact introduction, and relevant interests. Concrete facts should show fit without an applicant-style sales pitch. The deterministic soft-age sentence now says simply that Rakshit is younger than the preferred range but thinks it could still fit personally; it must not claim that he “bringt die nötige Reife mit.”

### One-time fees and takeovers

A one-time fee, furniture takeover, or accessories takeover is acceptable when its combined deterministically known total is strictly below €500. It must not create medium scam risk or block the universal fallback solely for that reason. A total of €500 or more, an unknown total, a suspicious payment method, or payment demanded before viewing remains REVIEW or SKIP under the existing scam rules. Cloud-AI fee-only concern is normalized only when the deterministic parser positively established a safe sub-€500 total; independent AI payment-method or scam concerns remain intact.

### Required German WG viewing and closing block

For ordinary German WG applications, preserve this wording exactly:

```text
Da ich aktuell noch in Berlin wohne, wäre ein erstes Kennenlernen per Video-Call für mich natürlich am einfachsten. Wenn eine Besichtigung vor Ort lieber ist, sagt mir einfach ein bisschen vorher Bescheid. Dann kann ich meine Tickets planen und in den nächsten Tagen nach Münster kommen.

Falls noch etwas offen ist, schreibt mir einfach gerne. Ich würde mich freuen, mehr über die Wohnung und das Zusammenleben zu erfahren :)

Liebe Grüße
Rakshit
```

Only adapt this to `Sie` for a genuinely formal landlord/company context. Do not casually paraphrase the block.

### Favorite-food hidden question

When a listing asks for the applicant's favorite food, answer naturally with:

```text
Hähnchenpasta mit Sahnesoße
```

A natural rendering is: `Beim Lieblingsessen bin ich aktuell ganz klar bei Hähnchenpasta mit Sahnesoße :)`

Do not add the previously observed awkward claim that the applicant will cook it at move-in; the validator rejects that wording.

## WG Premium and composer details

WG's rotating promotional UI is detected structurally rather than by visible marketing text. Relevant structural evidence includes:

- `data-campaign_type="wggplus_promotion"`
- source `dav`
- a position beginning with `contact_`

The current real account appears to apply priority account-wide. There may be no per-message `#wgg_plus_toggle`; the authenticated shop redirects to the Bewerbermappe/account area. `activate_wg_plus_priority` positively verifies the account entitlement and reports it as already active. With `WG_PREMIUM_STRICT=true`, lack of positive verification remains a blocker.

Observed current composer selectors/state:

- form: `#messenger_form`
- textarea: `#message_input` with name `content`
- submit button inside the form
- attachment trigger: `[data-target='#attachment_options_modal']`
- account package action: `#share_application_package`
- attached confirmation: `.pre_attached_application_package:has(#detach_application_package)`

Selectors can change; if they do, update fixture/browser tests and retain conservative failure behavior.

## Existing-conversation protection

A new message link is typically `/nachricht-senden`. An existing conversation is exposed by a primary link resembling:

```text
a.wgg-btn-primary[href*='/nachricht.html?nachrichten-id=']
```

The pipeline checks this before AI and again in browser automation. WG may expose an existing conversation after a dry-run composer visit even when no Send click occurred. Treat the site's state conservatively; do not bypass the duplicate-contact guard merely because local SQLite has no actual-send record.

## One-shot production command

`send_once.sh` implements the first controlled real-send path:

```bash
./send_once.sh 'https://www.wg-gesucht.de/...'
```

It:

- accepts exactly one explicit HTTPS WG-Gesucht listing URL;
- refuses unless `AUTO_SEND=true` is present in `.env`;
- refuses while the daemon is running;
- scopes `DRY_RUN=false` to its child process rather than editing daemon behavior;
- runs the exact single-listing production pipeline;
- exits nonzero unless the post-send state is verified;
- relies on the same filters, provider/fallback, validator, Premium, Bewerbermappe, final gates, SQLite claim, and browser sent-state verification as normal production.

Do not weaken these preconditions. The user must explicitly enable `AUTO_SEND=true` for the controlled test and should restore it to false afterward.

## Live-run history and lessons

### Listing 14101923

The first universal-fallback dry-run reached the composer, but the user noticed the listing was women-only. The real page contained `Frauen-WG` and `Frau zwischen 21 und 28`. Nothing was sent. The deterministic filter and database outcome were corrected to `filtered_skip`. This incident drove the requirement that all description tabs be extracted and women-only checks run before AI/fallback.

### Listing 12212661

Availability was `01.03.2027–31.05.2027`. Cloud AI detected incompatibility, and deterministic fixed date-range/duration extraction was subsequently added so this no longer depends on AI.

### Listing 13679223

No immediate hard filter, but multiple-room/shared-grocery ambiguity required REVIEW. This is an example where the universal fallback must remain unavailable.

### Listing 14102429

A real dry-run completed the browser preparation path:

- WG Plus verified;
- composer opened and filled;
- account Bewerbermappe visibly attached;
- automation stopped before Send;
- screenshot saved below `logs/browser`.

On revisit, WG exposed an existing conversation despite no Send click. Preserve the conservative existing-conversation guard.

### Listing 13208552

It contained a favorite-drink question plus conflicting Ablöse/document details. AI returned REVIEW. The deterministic policy rendered the required exact closing.

### Listing 14096490

This is the main age/register/length regression fixture: `tests/fixtures/wg_14096490.yaml`.

Observed deterministic facts after fixes:

```text
state: new
women_only: false
advertiser: wg
housing: wg_room
buergschaft required: false
age: 25–35, soft, applicant mismatch acknowledged
hidden questions: 1 (favorite food)
decision: APPLY
hard skips: none
warning: soft age mismatch only
```

The initial implementation misclassified register and length. Regression tests now require du/ihr, the soft-age note, a natural favorite-food answer, and no erroneous register/length block.

After the earlier provider-failure run, a later controlled production flow contacted this listing. SQLite records it as `sent` with confirmation at `2026-09-15T19:42:24.624262+00:00`. The sent cloud-AI message contained a duplicated age explanation and the disliked phrase `bringe die nötige Reife ... mit`; the current deterministic message policy removes provider-authored age-mismatch sentences and inserts one shorter, more natural sentence instead. Never attempt to contact this listing again.

## Database snapshot from the last inspection

The observed outcome totals were:

```text
ai_failed=1
already_contacted=1
discovered=1
drafted=3
filtered_skip=3
premium_boost_failed=1
review_required=4
sent=1
```

Recent rows included listings 14021501 (Premium boost failed), 14096490 (sent), 13208552 (review), 14102429 (already contacted according to site state), 13679223 (review), 12212661 (filtered), and 14101923 (filtered). Query current SQLite state rather than relying on these historical counts.

## Verification commands

From the repository root:

```bash
. .venv/bin/activate
python -m pytest -q
python -m ruff format --check app tests
python -m ruff check app tests
python -m mypy app
bash -n setup.sh start.sh stop.sh status.sh doctor.sh login.sh dry_run.sh send_once.sh
git diff --check
```

Operational checks:

```bash
./doctor.sh --skip-ai
./doctor.sh
./status.sh
```

Safe listing dry-run:

```bash
./dry_run.sh 'https://www.wg-gesucht.de/...'
```

Daemon lifecycle:

```bash
./start.sh
./status.sh
./stop.sh
```

Controlled single production send, only after the user explicitly enables it:

```bash
./send_once.sh 'https://www.wg-gesucht.de/...'
```

## Immediate takeover procedure

1. Read `codex_handoff/AGENTS.md`, `codex_handoff/CODEX_MASTER_HANDOFF.md`, this file, and `README.md` completely.
2. Inspect `git status --short`, `git diff --stat`, and the full relevant diffs before editing.
3. Confirm `.env` safety flags without printing its secret values.
4. Run the full verification commands above to establish the current baseline.
5. Preserve all safety gates and the corrected live-page extraction behavior.
6. Continue from the user's next concrete request. Do not independently initiate a real send, daemon, login, or account mutation.

## Remaining risks and limitations

- WG-Gesucht can change DOM structure and selectors without notice.
- The saved session can expire or trigger CAPTCHA/2FA; only the user can complete those challenges.
- Cloud providers can all be unavailable. The universal fallback deliberately refuses listings with questions or ambiguity, so some good listings will correctly stop at REVIEW rather than risk a bad application.
- A dry-run composer visit may cause WG to show an existing conversation state even without an actual Send click.
- Gmail remains disabled and has not been taken through a live OAuth/contact flow in the current configuration.
- A controlled actual send is recorded for listing 14096490. The contact row has a confirmation timestamp but no `send_clicked_at` value, so retain the listing/site/SQLite duplicate guards and do not infer that it is safe to resend.
- The worktree has not been committed after the current implementation. Preserve it and do not perform destructive git operations.
