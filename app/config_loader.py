from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .schemas import SendTrigger

ROOT = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def resolve_path(value: str, default: str) -> Path:
    path = Path(value or default).expanduser()
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Settings:
    provider_order: tuple[str, ...]
    ai_timeout_seconds: int
    anthropic_timeout_seconds: int
    openai_timeout_seconds: int
    gemini_timeout_seconds: int
    ai_max_retries: int
    ai_provider_cooldown_seconds: int
    dry_run: bool
    auto_send: bool
    bewerbermappe_path: Path | None
    wg_use_premium_boost: bool
    wg_premium_strict: bool
    browser_headless: bool
    browser_profile_path: Path
    browser_timeout_seconds: int
    database_path: Path
    input_directory: Path
    poll_interval_seconds: int
    gmail_enabled: bool
    gmail_credentials_path: Path
    gmail_token_path: Path
    gmail_alert_query: str
    log_level: str
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_allowed_user_id: int | None
    telegram_chat_id: str
    applicant_photo_path: Path | None = None
    freellm_base_url: str = ""
    freellm_api_key: str = ""
    freellm_model: str = ""
    freellm_timeout_seconds: int = 30

    def send_permitted(self, trigger: SendTrigger) -> bool:
        """Whether `trigger` may perform a REAL send right now.

        DRY_RUN is absolute: it overrides every trigger, unconditionally. Below that,
        AUTO_SEND only gates the daemon's own autonomous trigger ("auto") -- it does
        NOT gate an explicit human approval (manual_cli/telegram/dashboard), since
        AUTO_SEND=false means "no autonomous daemon send", not "disable human-approved
        sending". Every other safety gate (dedup, Bewerbermappe, required-fact
        validation, scam checks, send verification) is unaffected by this and still
        applies identically regardless of trigger -- this only decides who may
        initiate a send attempt in the first place.
        """
        if self.dry_run:
            return False
        if trigger == "auto":
            return self.auto_send
        return True

    def provider_timeout_seconds(self, provider: str) -> int:
        return {
            "anthropic": self.anthropic_timeout_seconds,
            "openai": self.openai_timeout_seconds,
            "gemini": self.gemini_timeout_seconds,
            "freellm": self.freellm_timeout_seconds,
        }.get(provider, self.ai_timeout_seconds)


def load_yaml(name: str) -> dict[str, Any]:
    with (ROOT / name).open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ValueError(f"{name} must contain a YAML mapping")
    return loaded


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env", override=False)
    raw_order = (
        os.getenv("AI_PROVIDER_ORDER")
        or os.getenv("CLOUD_FALLBACK_ORDER", "anthropic,openai,gemini")
        or "anthropic,openai,gemini"
    )
    order = tuple(item.strip().lower() for item in raw_order.split(",") if item.strip())
    unknown = sorted(set(order) - {"anthropic", "openai", "gemini", "freellm"})
    if unknown:
        raise ValueError(f"unsupported AI providers: {', '.join(unknown)}")
    document = os.getenv("BEWERBERMAPPE_PATH", "").strip()
    photo = os.getenv("APPLICANT_PHOTO_PATH", "").strip()
    default_ai_timeout = env_int("AI_TIMEOUT_SECONDS", 30)
    openai_timeout = env_int("OPENAI_TIMEOUT_SECONDS", default_ai_timeout)
    if openai_timeout < 20:
        raise ValueError("OPENAI_TIMEOUT_SECONDS must be at least 20 seconds")
    return Settings(
        provider_order=order,
        ai_timeout_seconds=default_ai_timeout,
        anthropic_timeout_seconds=env_int("ANTHROPIC_TIMEOUT_SECONDS", default_ai_timeout),
        openai_timeout_seconds=openai_timeout,
        gemini_timeout_seconds=env_int("GEMINI_TIMEOUT_SECONDS", default_ai_timeout),
        ai_max_retries=env_int("AI_MAX_RETRIES", 1),
        ai_provider_cooldown_seconds=env_int("AI_PROVIDER_COOLDOWN_SECONDS", 300),
        dry_run=env_bool("DRY_RUN", True),
        auto_send=env_bool("AUTO_SEND", False),
        bewerbermappe_path=resolve_path(document, document) if document else None,
        applicant_photo_path=resolve_path(photo, photo) if photo else None,
        wg_use_premium_boost=env_bool("WG_USE_PREMIUM_BOOST", True),
        wg_premium_strict=env_bool("WG_PREMIUM_STRICT", False),
        browser_headless=env_bool("BROWSER_HEADLESS", False),
        browser_profile_path=resolve_path(
            os.getenv("BROWSER_PROFILE_PATH", ""), "browser-profile/wg-gesucht"
        ),
        browser_timeout_seconds=env_int("BROWSER_TIMEOUT_SECONDS", 30),
        database_path=resolve_path(os.getenv("DATABASE_PATH", ""), "data/apartment_agent.sqlite"),
        input_directory=resolve_path(os.getenv("INPUT_DIRECTORY", ""), "data/inbox"),
        poll_interval_seconds=env_int("POLL_INTERVAL_SECONDS", 120),
        gmail_enabled=env_bool("ENABLE_GMAIL", False),
        gmail_credentials_path=resolve_path(
            os.getenv("GMAIL_CREDENTIALS_PATH", ""), "credentials.json"
        ),
        gmail_token_path=resolve_path(os.getenv("GMAIL_TOKEN_PATH", ""), "token.json"),
        gmail_alert_query=os.getenv(
            "GMAIL_ALERT_QUERY", "newer_than:2d (from:wg-gesucht.de OR subject:(WG-Gesucht))"
        ),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        telegram_enabled=env_bool("TELEGRAM_ENABLED", False),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_allowed_user_id=(
            int(raw_id) if (raw_id := os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()) else None
        ),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        freellm_base_url=os.getenv("FREELLM_BASE_URL", "http://127.0.0.1:3001/v1").strip(),
        freellm_api_key=os.getenv("FREELLM_API_KEY", "").strip(),
        freellm_model=os.getenv("FREELLM_MODEL", "").strip(),
        freellm_timeout_seconds=env_int("FREELLM_TIMEOUT_SECONDS", default_ai_timeout),
    )


def load_all() -> tuple[dict[str, Any], dict[str, Any], Settings]:
    return load_yaml("config.yaml"), load_yaml("answer_bank.yaml"), load_settings()