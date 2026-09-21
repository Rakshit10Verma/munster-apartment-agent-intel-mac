# Autonomous Hardening Setup

This repo is intended to be developed with gstack plus an optional local FreeLLMAPI router.

## 1. Install gstack

From a terminal on the development Mac:

```bash
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
cd ~/.claude/skills/gstack
./setup
```

Then from this repository:

```bash
(cd ~/.claude/skills/gstack && ./setup --team)
~/.claude/skills/gstack/bin/gstack-team-init required
```

Review the generated .claude/ files before committing them.

## 2. Install FreeLLMAPI locally

Recommended local-first setup:

```bash
curl -fsSL https://freellmapi.co/install.sh | bash
```

Or use the project's documented Docker/manual installation if preferred.

Open its local dashboard, add only provider keys/free tiers you are legitimately entitled to use, and copy the unified local API key.

Do not use stolen/shared keys, trial farming or rate-limit bypass schemes.

## 3. Configure Claude Code to use FreeLLMAPI where useful

FreeLLMAPI exposes an Anthropic-compatible surface for Claude Code. Use its current generated setup command rather than hand-editing credentials:

```bash
npx freellmapi setup-claude --url http://localhost:3001 --api-key '<your-unified-key>'
```

Prefer the free pool for mechanical/low-risk development work. Use a stronger model when architecture, production debugging or browser diagnosis needs it.

## 4. Configure the housing agent

Copy .env.example to .env if needed. Never commit .env.

During live hardening, intentional production sending requires:

```dotenv
DRY_RUN=false
AUTO_SEND=true
TELEGRAM_ENABLED=true
BROWSER_HEADLESS=false
```

Keep WG_PREMIUM_STRICT=false unless strict Premium verification is intentionally desired.

After FreeLLMAPI support in this branch is merged, add:

```dotenv
FREELLM_BASE_URL=http://127.0.0.1:3001/v1
FREELLM_API_KEY=<unified local key>
FREELLM_MODEL=<selected model/route>
FREELLM_TIMEOUT_SECONDS=30
AI_PROVIDER_ORDER=freellm,gemini,openai,anthropic
```

Provider order can be changed without altering the send-integrity rules.

## 5. Login/session setup

```bash
./setup.sh
./doctor.sh
./login.sh wg
```

Run Telegram separately:

```bash
./telegram_bot.sh
```

Then start the watcher:

```bash
./start.sh
./status.sh
```

Do not reset production contact history merely to obtain a clean dashboard.

## 6. Engineering loop

For a substantial unfinished area:

```text
/office-hours
/autoplan
implement
/review
/qa
```

The final proof for browser/Telegram bugs is a real observed product flow as described in FINISH_LINE.md, not only pytest.

## 7. First hardening milestone

Before attempting broader autonomous refactors, fix the current Telegram review dead-end:
- separate hard technical blockers from soft missing facts/preferences;
- allow explicit Confirm Send to override soft blockers without inventing facts;
- provide Telegram answer/Send anyway paths;
- make optional WG-experience questions non-blocking end-to-end;
- preserve duplicate/send_state_unknown protections;
- verify with real WG + Telegram observation.
