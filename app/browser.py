from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .config_loader import ROOT, Settings, load_yaml
from .schemas import AnalysisOutcome, ContactResult

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, BrowserType, Locator, Page

logger = logging.getLogger("apartment_agent.browser")

CONTACT_SELECTORS = (
    "button:has-text('Nachricht senden')",
    "a:has-text('Nachricht senden')",
    "button:has-text('Kontaktieren')",
    "a:has-text('Kontaktieren')",
    "button:has-text('Anbieter kontaktieren')",
    "[data-testid*='contact']",
)
MESSAGE_SELECTORS = (
    "textarea[name='message']",
    "textarea[name*='message']",
    "textarea#message",
    "textarea[placeholder*='Nachricht']",
    "textarea[placeholder*='message' i]",
    "[contenteditable='true'][role='textbox']",
)
SUBJECT_SELECTORS = (
    "input[name='subject']",
    "input[placeholder*='Betreff']",
    "input[placeholder*='subject' i]",
)
NAME_SELECTORS = ("input[name*='name' i]", "input[autocomplete='name']")
EMAIL_SELECTORS = ("input[type='email']", "input[name*='mail' i]")
PHONE_SELECTORS = ("input[type='tel']", "input[name*='phone' i]", "input[name*='telefon' i]")
SEND_SELECTORS = (
    "button:has-text('Nachricht senden')",
    "button:has-text('Senden')",
    "button:has-text('Send message')",
    "button[type='submit']",
)


def _launch_persistent(
    browser_type: BrowserType, settings: Settings, *, headless: bool
) -> BrowserContext:
    channel = os.getenv("BROWSER_CHANNEL", "chrome").strip()
    return browser_type.launch_persistent_context(
        str(settings.browser_profile_path), headless=headless, channel=channel or None
    )


def _first_visible(page: Page, selectors: tuple[str, ...]) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=800):
                return locator
        except Exception:
            continue
    return None


def _goto_with_retry(page: Page, url: str, timeout_ms: int, attempts: int = 2) -> None:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                page.wait_for_timeout(750 * (attempt + 1))
    raise RuntimeError(f"page navigation failed after {attempts} attempts: {last_error}")


def _artifact_prefix(listing_id: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", listing_id or "listing")
    directory = ROOT / "logs" / "browser"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{stamp}_{safe_id}"


def login_wg(settings: Settings) -> None:
    from playwright.sync_api import sync_playwright

    settings.browser_profile_path.mkdir(parents=True, exist_ok=True)
    login_url = __import__("os").getenv(
        "WG_LOGIN_URL", "https://www.wg-gesucht.de/mein-wg-gesucht.html"
    )
    with sync_playwright() as playwright:
        context = _launch_persistent(playwright.chromium, settings, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        _goto_with_retry(page, login_url, settings.browser_timeout_seconds * 1000)
        print("Complete WG-Gesucht login/CAPTCHA/2FA in the browser, then press Enter here.")
        input()
        context.close()


def browser_profile_has_session(settings: Settings) -> bool:
    if not settings.browser_profile_path.is_dir():
        return False
    cookie_files = list(settings.browser_profile_path.rglob("Cookies"))
    return any(path.is_file() and path.stat().st_size > 0 for path in cookie_files)


def fetch_full_listing(outcome_url: str, settings: Settings) -> str:
    """Fetch visible listing text through the user's normal persistent browser session."""
    from playwright.sync_api import sync_playwright

    settings.browser_profile_path.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = _launch_persistent(
            playwright.chromium, settings, headless=settings.browser_headless
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(settings.browser_timeout_seconds * 1000)
        try:
            _goto_with_retry(page, outcome_url, settings.browser_timeout_seconds * 1000)
            page.wait_for_timeout(600)
            text = ""
            for selector in (
                "#main_column",
                "[data-testid='listing-details']",
                "[data-testid*='listing-description']",
                "main",
                "body",
            ):
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    candidate = locator.inner_text().strip()
                    if len(candidate) > 100:
                        text = candidate
                        break
            if not text:
                raise RuntimeError("listing text container was not found")
            lower = text.casefold()
            if any(
                marker in lower
                for marker in ("captcha", "ich bin kein roboter", "verify you are human")
            ):
                raise RuntimeError("CAPTCHA/human verification requires a headed login")
            return text.strip()
        finally:
            context.close()


def prepare_platform_contact(outcome: AnalysisOutcome, settings: Settings) -> ContactResult:
    if not outcome.message or not outcome.listing.url:
        return ContactResult(status="review_required", detail="listing URL or message is missing")
    if not outcome.validation.auto_send_allowed:
        return ContactResult(
            status="review_required", detail="deterministic validation did not pass"
        )
    from playwright.sync_api import sync_playwright

    artifact = _artifact_prefix(outcome.listing.listing_id)
    settings.browser_profile_path.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = _launch_persistent(
            playwright.chromium, settings, headless=settings.browser_headless
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(settings.browser_timeout_seconds * 1000)
        try:
            _goto_with_retry(page, outcome.listing.url, settings.browser_timeout_seconds * 1000)
            page.wait_for_timeout(800)
            page_text = page.locator("body").inner_text(timeout=5000).casefold()
            if any(
                marker in page_text
                for marker in ("captcha", "ich bin kein roboter", "verify you are human")
            ):
                return ContactResult(
                    status="review_required",
                    detail="CAPTCHA/human verification detected; run ./login.sh wg",
                )
            message_box = _first_visible(page, MESSAGE_SELECTORS)
            if message_box is None:
                button = _first_visible(page, CONTACT_SELECTORS)
                if button is not None:
                    button.click()
                    page.wait_for_timeout(800)
                    message_box = _first_visible(page, MESSAGE_SELECTORS)
            if message_box is None:
                if re.search(r"einloggen|anmelden|log\s?in", page_text):
                    detail = "WG-Gesucht login expired or missing; run ./login.sh wg"
                else:
                    detail = "message composer was not found; selectors/site layout need review"
                raise RuntimeError(detail)
            message_box.fill(outcome.message.body)
            subject = _first_visible(page, SUBJECT_SELECTORS)
            if subject is not None and outcome.message.subject:
                subject.fill(outcome.message.subject)
            # Public contact forms may require identity fields. Values come from config.yaml.
            applicant = load_yaml("config.yaml")["applicant"]
            for selectors, value in (
                (NAME_SELECTORS, applicant["name"]),
                (EMAIL_SELECTORS, applicant["email"]),
                (PHONE_SELECTORS, applicant["phone"]),
            ):
                field = _first_visible(page, selectors)
                if field is not None and not field.input_value():
                    field.fill(str(value))
            if outcome.attachment.should_attach and outcome.attachment.path:
                file_input = page.locator("input[type='file']").first
                if not file_input.count():
                    raise RuntimeError("allowed attachment requested but file input was not found")
                file_input.set_input_files(outcome.attachment.path)
            screenshot = artifact.with_suffix(".png")
            page.screenshot(path=str(screenshot), full_page=True)
            if not settings.sending_enabled:
                return ContactResult(
                    status="dry_run_ready",
                    screenshot_path=str(screenshot),
                    detail="composer filled; stopped before Send",
                )
            send = _first_visible(page, SEND_SELECTORS)
            if send is None or not send.is_enabled():
                raise RuntimeError("Send button unavailable after composer validation")
            send.click()
            page.wait_for_timeout(1200)
            after = page.locator("body").inner_text().casefold()
            if not any(
                marker in after
                for marker in ("nachricht gesendet", "message sent", "wurde gesendet")
            ):
                raise RuntimeError("Send was clicked but sent state could not be verified")
            return ContactResult(
                status="sent",
                screenshot_path=str(screenshot),
                detail="platform confirmed sent state",
            )
        except Exception as exc:
            try:
                page.screenshot(
                    path=str(artifact.with_name(artifact.name + "_failure.png")), full_page=True
                )
                artifact.with_name(artifact.name + "_failure.html").write_text(
                    page.content(), encoding="utf-8"
                )
            except Exception:
                logger.exception("failed to capture browser failure artifacts")
            return ContactResult(status="send_failed", detail=str(exc)[:500])
        finally:
            context.close()
