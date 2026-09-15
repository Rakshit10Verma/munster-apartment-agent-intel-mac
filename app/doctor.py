from __future__ import annotations

import importlib.util
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .browser import browser_profile_has_session
from .config_loader import Settings, load_all
from .database import Database
from .prefilter import deterministic_prefilter
from .prompts import SYSTEM_PROMPT, combined_prompt
from .providers import ProviderError, route_cloud
from .schemas import SourceListing


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _provider_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    for name in settings.provider_order:
        key_name = f"{name.upper()}_API_KEY"
        model_name = f"{name.upper()}_MODEL"
        configured = bool(os.getenv(key_name, "").strip())
        model = os.getenv(model_name, "").strip()
        checks.append(
            Check(
                f"AI {name}",
                "OK" if configured and model else "MISSING",
                f"key {'present' if configured else 'blank'}; model {model or 'blank'}",
            )
        )
    return checks


def _browser_check(settings: Settings) -> Check:
    if importlib.util.find_spec("playwright") is None:
        return Check("Playwright browser", "MISSING", "run ./setup.sh")
    try:
        system_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        channel = os.getenv("BROWSER_CHANNEL", "chrome")
        if channel == "chrome":
            selected = system_chrome
        else:
            cache = Path.home() / "Library/Caches/ms-playwright"
            candidates = list(
                cache.glob("chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium")
            )
            selected = candidates[-1] if candidates else cache / "missing-chromium"
        return Check(
            "Playwright browser",
            "OK" if selected.is_file() else "MISSING",
            str(selected) if selected.is_file() else "run: .venv/bin/playwright install chromium",
        )
    except Exception as exc:
        return Check("Playwright browser", "ERROR", str(exc)[:200])


def _document_check(settings: Settings) -> Check:
    path = settings.bewerbermappe_path
    if path is None:
        return Check("Bewerbermappe", "MISSING", "set absolute BEWERBERMAPPE_PATH in .env")
    if not path.is_absolute():
        return Check("Bewerbermappe", "ERROR", "path is not absolute")
    if not path.is_file():
        return Check("Bewerbermappe", "MISSING", f"file not found: {path}")
    return Check("Bewerbermappe", "OK", f"{path.name} ({path.stat().st_size} bytes)")


def _database_check(settings: Settings) -> Check:
    try:
        database = Database(settings.database_path)
        with database.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return Check("SQLite", "OK", str(settings.database_path))
    except Exception as exc:
        return Check("SQLite", "ERROR", str(exc)[:200])


def _ai_smoke(settings: Settings, config: dict[str, Any], answers: dict[str, Any]) -> Check:
    if not any(
        os.getenv(f"{name.upper()}_API_KEY", "").strip() for name in settings.provider_order
    ):
        return Check("AI smoke", "SKIPPED", "no cloud API key configured")
    listing = SourceListing(
        platform="mock",
        listing_id="doctor-smoke",
        raw_text=(
            "WG-Zimmer in Münster, 490 Euro warm, mindestens 12 Monate. "
            "Wir kochen manchmal zusammen und fahren gern Fahrrad."
        ),
    )
    prefilter = deterministic_prefilter(listing, config)
    try:
        result, notes = route_cloud(
            SYSTEM_PROMPT,
            combined_prompt(listing, prefilter, config, answers),
            settings,
        )
        note = f" via {result.metadata.provider}/{result.metadata.model}"
        if notes:
            note += f" ({len(notes)} fallback note(s))"
        return Check("AI smoke", "OK", f"{result.metadata.latency_ms} ms{note}")
    except ProviderError as exc:
        return Check("AI smoke", "ERROR", str(exc)[:300])


def run_doctor(ai_smoke: bool = True) -> tuple[list[Check], bool]:
    config, answers, settings = load_all()
    checks = [
        Check("Python", "OK", platform.python_version()),
        Check(
            "Safety mode",
            "OK" if settings.dry_run and not settings.auto_send else "WARN",
            f"DRY_RUN={settings.dry_run}, AUTO_SEND={settings.auto_send}, "
            f"effective sending={settings.sending_enabled}",
        ),
        Check(
            "Production AI path",
            "OK",
            "cloud only; inherited LOCAL_FIRST/llama.cpp settings are ignored",
        ),
        *_provider_checks(settings),
        _document_check(settings),
        _browser_check(settings),
        Check(
            "WG session",
            "OK" if browser_profile_has_session(settings) else "MISSING",
            str(settings.browser_profile_path),
        ),
        Check(
            "Gmail OAuth",
            "OK"
            if settings.gmail_token_path.is_file()
            else ("MISSING" if settings.gmail_enabled else "DISABLED"),
            (
                str(settings.gmail_token_path)
                if settings.gmail_token_path.is_file()
                else "run ./login.sh gmail"
                if settings.gmail_enabled
                else "ENABLE_GMAIL=false"
            ),
        ),
        _database_check(settings),
    ]
    if ai_smoke:
        checks.append(_ai_smoke(settings, config, answers))
    required_errors = [check for check in checks if check.status == "ERROR"]
    providers_present = any(
        check.name.startswith("AI ") and check.status == "OK" for check in checks
    )
    usable = (
        not required_errors and providers_present and settings.dry_run and not settings.auto_send
    )
    return checks, usable


def doctor(ai_smoke: bool = True, console: Console | None = None) -> bool:
    checks, usable = run_doctor(ai_smoke=ai_smoke)
    table = Table(title="Münster Apartment Agent Doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Details")
    colors = {"OK": "green", "WARN": "yellow", "ERROR": "red", "MISSING": "yellow"}
    for check in checks:
        color = colors.get(check.status, "dim")
        table.add_row(check.name, f"[{color}]{check.status}[/{color}]", check.detail)
    (console or Console()).print(table)
    return usable
