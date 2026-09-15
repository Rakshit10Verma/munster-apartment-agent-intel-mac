# AGENTS.md

You are the implementation owner for the Münster Apartment Agent in this repository.

Read `CODEX_MASTER_HANDOFF.md` completely before editing.

Rules:
- Inspect the existing repo first. Reuse useful config/data; refactor broken architecture.
- Do not stop at planning/scaffolding. Implement, run tests, fix failures, and leave a usable tool.
- Run routine commands/tests yourself instead of asking the user.
- Human-only actions may be requested only for: entering secrets in `.env`, one-time website/OAuth login, CAPTCHA/2FA, and choosing the Bewerbermappe file.
- Never hardcode or print secrets. Keep `.env` gitignored.
- Default to `DRY_RUN=true` and `AUTO_SEND=false`.
- Never bypass CAPTCHA, anti-bot controls, 2FA, rate limits, or access controls.
- Local llama.cpp/Gemma must NOT be in the production hot path. It was too slow and produced poor German.
- Production AI routing is configurable, default `anthropic,openai,gemini`.
- Use deterministic code before AI where possible.
- Prefer ONE cloud AI call per viable listing that returns both structured analysis and final message.
- Deterministic validators, not model confidence alone, decide whether sending is allowed.
- WG-Gesucht is the speed-critical primary platform.
- Add real tests, logging, deduplication, graceful failure handling, and nontechnical run scripts.
