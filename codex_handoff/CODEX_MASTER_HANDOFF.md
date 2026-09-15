# CODEX MASTER HANDOFF — Münster Apartment Agent

## Goal
Finish this repo into an autonomous Münster housing-search and first-contact agent:
discover -> parse -> hard-filter -> scam/doc-risk check -> hidden-question detection -> personalized message -> deterministic validation -> contact/send -> logging/deduplication.

The user should only have to insert API keys, set the Bewerbermappe path, complete one-time logins/OAuth/CAPTCHA when required, and later enable auto-send.

## Current state
The repo contains early Python/YAML/provider code and an obsolete local-LLM experiment.
Local Gemma on the user's 2019 Intel Mac took several minutes and generated poor German. Do not spend time optimizing local inference for production.

A previous bad message was incorrectly marked auto-send-safe. The new validator must reject similar output.

## Platform priority
1. WG-Gesucht: main source, speed critical, internal message, attach Bewerbermappe only after legitimacy check.
2. AStA Münster Wohnbörse: email if given, otherwise supported site contact.
3. na dann: email if given, otherwise internal contact if available.
4. Kleinanzeigen: platform message.
5. Keep adapters modular for future university portals.

Never bypass human verification/anti-bot controls.

## Applicant
Name: Rakshit Verma
Age: 21, turning 22 in October
Phone: 01743915037
Email: rakshit.verma1410@gmail.com
Education: starting MSc Information Systems at University of Münster; previous BSc Data Science in Berlin; lived in Berlin ~3.5 years.
Employment: Werkstudent at Landesbausparkasse in Potsdam, fully remote, stable income.
Languages: German fluent, English fluent, Hindi fluent/native, French basic.
Move-in: preferred 2026-10-01; last week of September also possible.
Intended stay: ~2 years / full master's.
Viewing: always offer video call and say he can come to Münster on short notice.

Never mention nationality/origin in normal applications unless a form explicitly requires it.
Never abbreviate Landesbausparkasse as "LBS".
Never call him "Remote-Mitarbeiter"; he is a Werkstudent.

## Housing rules
Accept WG rooms, studios, student rooms, whole flats, and Zwischenmiete >= 6 months.

Normal single room/studio budget: max €550 warm.

Whole-flat exception:
Do not reject a whole flat merely because total rent > €550.
Example acceptable opportunity: 3-room flat around €1,100 warm if realistically shareable.
Evaluate usable rooms, realistic residents, effective per-person rent, ability to add tenants, Hauptmieter liability, deposit and contract risks.

Hard SKIP:
- women-only / men excluded
- WBS required
- normal room/studio > €550 warm
- duration < 6 months
- mandatory minimum age >= 30
- religion/confession membership required
- religious Studentenverbindung/fraternity/association obligations required
- clear/high scam risk
- any mandatory requirement applicant cannot satisfy

Age preference is not a hard age requirement.

Warnings:
- no Anmeldung
- Ablöse > €400
- high deposit
- Bürgschaft
- Indexmiete
- Hauptmieter liability
- odd fees/payment demands
- contradictory information

## Scam/document policy
Before sensitive documents:
- run legitimacy/scam checks
- high risk => skip
- medium/ambiguous => no sensitive attachment and no auto-send until resolved

Signals include landlord abroad + keys by post, payment before viewing, no viewing, implausibly cheap rent, copied/stock-looking material, contradictory details, unusual urgency/payment method.

Bewerbermappe may contain:
- SCHUFA
- Mietschuldenfreiheitsbescheinigung
- salary slip
- University of Münster acceptance letter

Use env var `BEWERBERMAPPE_PATH`.
WG-Gesucht: attach only after legitimacy gate.
Email: attach only when listing explicitly requests documents; otherwise say documents are available on request.
Never commit applicant documents.

## Confirmed answer bank
Use semantically and naturally; never invent.

Card games: Skyjo, Flip 7; sometimes Uno; does not play Doppelkopf.
Food: Italian, Korean, Indian; go-to Hähnchenpasta mit Sahnesoße; sometimes cooks properly, otherwise meal prep/quick meals; eats everything.
Drink: coconut water.
Music: 90s Bollywood, Kanye West, Eminem. If asked for one exact favorite song, say no fixed single favorite and name known preferences.
Fictional character: does not watch many films/series and does not really identify with a fictional character; prefers simply being himself.
Sports: cycling, bouldering, gym ~4x/week, table tennis, chess, card games, open to new sports.
Weekend: relax, hobbies, personal projects.
Ideal WG activities: cards together, eating together.
Study: mostly library.
Home office: 7-8 hours with ~30-40 min food break.
WG atmosphere: relaxed/cool, fun people, social but respects personal space, generally quiet.
Cleaning: WG plan; otherwise ~weekly.
Dislikes: messy kitchen/bathroom/common areas.
Friends: ~1-2 visits/week.
Alcohol: occasional gatherings/parties, not regular.
Party frequency: about monthly or every 2-3 months / occasions.
Morning/night: morning person; likes first sunlight/morning breeze.
Partner: lives in Berlin, rarely visits.
Overnight guests: generally none.
Parties at home: no.
Travel: vacations, usually ~a week.
Sustainability: reduce waste/water, use paper carefully.
Favorite season: summer.
Politics: prefer not to answer.
Sensitive identity/personal questions: prefer not to answer.
Unknown subjective facts: never invent. Use a truthful graceful non-answer if possible; otherwise block auto-send.

## Hidden questions/commands
Must extract separate arrays with stable ids:
- hidden_questions
- hidden_commands

Examples: favorite food/card game/song, Doppelkopf, required first word, exact subject, answer three questions, length instructions.

Before send:
required question ids == answered question ids
required command ids == applied command ids
and exact commands must be verifiable in final subject/body.
Missing required item => AUTO_SEND=false.

## Message quality
Listing German => German; English => English.
Student WG => natural du/ihr.
Formal/private landlord => Sie.
Human, concise, age-appropriate, 1-2 truly listing-specific hooks.
WG usually 100-170 words; formal 90-140.

Use when natural:
- Rakshit, 21
- starting MSc Information Systems at University of Münster
- Werkstudent at Landesbausparkasse, fully remote
- long-term stay
- video + short-notice Münster viewing

Reject:
- generic cover-letter opening ("ich bewerbe mich sehr gerne...", "Mit großem Interesse...")
- "Remote-Mitarbeiter"
- "LBS"
- nationality/origin
- invented preferences
- awkward restatement of hidden question
- bullet points in outgoing body
- over-polished HR tone
- messages with no listing-specific hook

Known bad example that MUST fail validator:
"welches Kartenspiel du am liebsten spielst, ist Skyjo oder Flip 7."
Better natural integration:
"Bei Kartenspielen bin ich übrigens ganz klar bei Skyjo oder Flip 7."

## AI architecture
Production hot path: cloud only.
Default configurable order:
`AI_PROVIDER_ORDER=anthropic,openai,gemini`

Keys:
- ANTHROPIC_API_KEY
- OPENAI_API_KEY
- GEMINI_API_KEY

Keep model ids configurable in `.env`, not hardcoded into business logic.
At implementation time use currently supported stable models from official SDKs.

Fallback:
missing key / quota / payment / timeout / schema failure -> next provider.
Retry same provider only for reasonable transient failures.

Optimize latency/cost:
1. deterministic prefilter first
2. ONE cloud AI call per viable listing where practical
3. response contains structured listing analysis AND final message
4. log provider/model/latency/token usage where available, never secrets

Structured AI result should include:
facts, ambiguities, scam risk+reasons, hidden questions, hidden commands, personalization hooks,
decision recommendation, final subject, final body, answered ids, applied command ids,
unresolved required facts, confidence.

Deterministic code is final authority for hard filters and send permission.

## Validator
Block auto-send if:
- hard skip
- high scam risk
- missing hidden answer/command
- unresolved required personal fact
- nationality/origin appears without explicit form requirement
- "LBS" appears
- "Remote-Mitarbeiter" appears
- required subject/keyword missing
- language/register mismatch
- empty/too-short/absurdly-long message
- document policy violation
- critical contradiction/ambiguity
- provider failure

Also flag generic opener, lack of listing-specific hook, repeated question wording, obviously reusable copy-paste text.

## Discovery
Implement common source adapter interface.

WG-Gesucht:
Prefer saved-search email alert via Gmail API initially, but benchmark latency. If safe browser discovery is faster/reliable, keep as alternative.
Deduplicate by listing id/url.
No aggressive scraping or anti-bot bypass.

AStA/na dann:
low-frequency periodic checks are fine.

## Browser automation
Use Playwright.
- persistent browser profile
- one-time headed login
- headless/headed configurable
- robust selectors
- screenshots/HTML dump on failure
- retry/backoff
- detect login expiry / CAPTCHA
- never bypass verification

WG flow:
open listing -> verify listing -> get full text/fields -> analyze -> fill message ->
attach Bewerbermappe if allowed -> in DRY_RUN stop before Send + screenshot ->
in AUTO_SEND click only if every gate passes -> verify sent state -> log.

## Email
Use Gmail API/OAuth if practical.
Listing email => send through Gmail.
Honor subject requirements.
Attach only when explicitly requested.
Log message/thread id and prevent duplicates.

## Persistence
Mandatory SQLite.
Unique key on platform+listing id/url.
Suggested statuses:
discovered, filtered_skip, ai_failed, review_required, drafted, dry_run_ready, sent, send_failed, replied, closed.

Store timestamps, source/url/id, normalized facts, decision reasons, warnings,
provider/model/latency, final message, attachment decision, send result, errors.

Provide CSV export. Google Sheets optional, not a core dependency.

## Nontechnical UX
User should operate via simple scripts:
- ./setup.sh
- ./login.sh
- ./dry_run.sh
- ./start.sh
- ./stop.sh
- ./status.sh
- ./doctor.sh

`doctor` should check environment, keys presence (not values), browser, WG session,
Gmail token if enabled, Bewerbermappe path, DB, AI smoke test, and DRY_RUN/AUTO_SEND state.

## Secrets
Create `.env.example`; never commit `.env`.
Expected vars include:
AI_PROVIDER_ORDER=anthropic,openai,gemini
provider keys/model ids
DRY_RUN=true
AUTO_SEND=false
BEWERBERMAPPE_PATH=
BROWSER_HEADLESS=
GMAIL_CREDENTIALS_PATH=
DATABASE_PATH=
polling/provider timeouts

## Tests
Unit tests for every hard skip/warning.
Provider-contract tests: valid result, schema failure, timeout, quota, fallback, all unavailable.
Validator tests for:
Remote-Mitarbeiter, LBS, nationality mention, missing hidden answer,
bad card-game sentence, missing exact keyword, wrong register/language, generic opener, unresolved question.

Regression fixtures:
1. Catholic Studentenverbindung => SKIP
2. WG with €150 Ablöse => APPLY
3. whole-flat/Hauptmiete opportunity => not automatic skip
4. casual WG + hidden question => APPLY
5. formal landlord => Sie, Landesbausparkasse, no origin
6. €600 normal WG room => SKIP
7. WBS => SKIP
8. women-only => SKIP
9. 3-month Zwischenmiete => SKIP

E2E:
mock source -> AI -> validator -> mocked sender -> DB
real provider smoke test when keys exist
Playwright live dry-run if login exists
Never send a test application to an unrelated real person.

## Acceptance criteria
Do not declare finished until, as far as environment access permits:
- pytest passes
- chosen formatter/linter/type checks pass
- doctor is useful
- sample cloud processing completes in seconds, not minutes
- provider fallback proven by mocks
- known bad local-model message is blocked
- dry-run shows natural personalized message
- hidden questions/commands verified deterministically
- duplicate listing cannot be contacted twice
- sensitive-document policy enforced
- Playwright preserves session and reaches/fills composer in dry-run when login is available
- actual sending is gated by AUTO_SEND=true
- daemon logs failures instead of crashing
- README has a plain-English "How Rakshit uses this" section
- no secrets committed

## How to take over the repo
1. inspect tree, git status, README, YAML, app code and tests
2. initialize git if needed and create a safe baseline commit
3. preserve useful YAML data
4. remove obsolete Ollama/llama.cpp production assumptions
5. implement cloud-first single-call contract
6. implement validator + regression tests first
7. then persistence/discovery/browser/email
8. run tests continuously
9. leave simple scripts/docs
10. do not ask user to manually copy code into files; edit the workspace yourself
