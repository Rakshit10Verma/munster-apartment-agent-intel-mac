from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

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
    ai_max_retries: int
    ai_provider_cooldown_seconds: int
    dry_run: bool
    auto_send: bool
    bewerbermappe_path: Path | None
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

    @property
    def sending_enabled(self) -> bool:
        return self.auto_send and not self.dry_run


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
    unknown = sorted(set(order) - {"anthropic", "openai", "gemini"})
    if unknown:
        raise ValueError(f"unsupported AI providers: {', '.join(unknown)}")
    document = os.getenv("BEWERBERMAPPE_PATH", "").strip()
    return Settings(
        provider_order=order,
        ai_timeout_seconds=env_int("AI_TIMEOUT_SECONDS", 35),
        ai_max_retries=env_int("AI_MAX_RETRIES", 1),
        ai_provider_cooldown_seconds=env_int("AI_PROVIDER_COOLDOWN_SECONDS", 300),
        dry_run=env_bool("DRY_RUN", True),
        auto_send=env_bool("AUTO_SEND", False),
        bewerbermappe_path=resolve_path(document, document) if document else None,
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
    )


def load_all() -> tuple[dict[str, Any], dict[str, Any], Settings]:
    return load_yaml("config.yaml"), load_yaml("answer_bank.yaml"), load_settings()
