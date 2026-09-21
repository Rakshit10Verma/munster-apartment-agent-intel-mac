from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urljoin

from .config_loader import ROOT, Settings, load_yaml
from .documents import validate_applicant_photo
from .prefilter import photo_required_for_initial_contact
from .schemas import (
    AnalysisOutcome,
    ContactResult,
    PremiumBoostResult,
    WGBewerbermappeResult,
    WGListingInspection,
)

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, BrowserType, Locator, Page

logger = logging.getLogger("apartment_agent.browser")

# Actions are selected by destination/state rather than rotating or localized visible copy.
WG_NEW_ACTION_SELECTORS = (
    "a[href*='/nachricht-senden/']",
    "form[action*='/nachricht-senden/'] button[type='submit']",
    "[data-testid*='contact-advertiser' i]",
    "[data-action*='open-message-composer' i]",
)
WG_CONVERSATION_ACTION_SELECTORS = (
    "a.wgg-btn-primary[href*='/nachricht.html?nachrichten-id=']",
    "a.wgg-btn-primary[href*='/nachrichten']",
    # Deliberately NOT `a[class*='primary'][href*='/nachrichten']`: `[class*=...]` is a
    # substring match over the whole class attribute, so it also caught the site-wide
    # "unread messages" nav badge (`class="... wgg_primary ..."`, `href="/nachrichten.html"`,
    # hidden unless the account has ANY unread message anywhere). That badge is unrelated
    # to this specific listing and caused a real false "already_contacted" positive that
    # blocked a legitimate, never-contacted listing. `.wgg-btn-primary` above is a proper
    # class-token selector and does not have this problem.
    "a[href*='/nachricht-anzeigen/']",
    "[data-testid*='view-conversation' i]",
    "[data-conversation-id][role='button']",
)
WG_PREMIUM_ENTRY_SELECTORS = (
    # Observed on the real WG listing DOM. The final token rotates with the marketing copy.
    "[data-campaign_type='wggplus_promotion']"
    "[data-campaign_click_source='dav'][data-campaign_click_position^='contact_']",
    "[data-testid*='premium' i][data-testid*='boost' i]",
    "[data-testid*='wgg-plus' i][data-testid*='contact' i]",
    "[data-component*='premium' i][data-action*='boost' i]",
    "[data-action*='message-priority' i]",
    "[aria-controls*='priority' i][aria-haspopup]",
)
WG_PRIORITY_ACTION_SELECTORS = (
    "[data-testid*='message-priority' i]:not([data-agent-premium-entry])",
    "[data-testid*='inbox-boost' i]:not([data-agent-premium-entry])",
    "[data-testid*='overtake' i]:not([data-agent-premium-entry])",
    "[data-action*='message-priority' i]:not([data-agent-premium-entry])",
    "[data-action*='inbox-boost' i]:not([data-agent-premium-entry])",
    "[data-feature*='message-priority' i]:not([data-agent-premium-entry])",
    "[data-feature*='inbox-boost' i]:not([data-agent-premium-entry])",
    "input[name*='priority' i]",
    "input[name*='boost' i]",
    "a[href*='message-priority' i]",
    "a[href*='inbox-boost' i]",
)
WG_PREMIUM_VERIFIED_SELECTORS = (
    "[data-premium-boost-state='active']",
    "[data-message-priority-state='active']",
    "[data-feature*='message-priority' i][data-state='active' i]",
    "[data-feature*='inbox-boost' i][data-state='active' i]",
    "[data-testid*='priority' i][data-state='active' i]",
    "[data-testid*='boost' i][data-state='active' i]",
    "[data-testid*='priority' i][aria-pressed='true']",
    "[data-testid*='boost' i][aria-checked='true']",
    "input[name*='priority' i]:checked",
    "input[name*='boost' i]:checked",
)
WG_DOSSIER_CONTROL_SELECTORS = (
    "#share_application_package",
    "#share_application_package_from_modal",
    "[data-testid*='bewerbermappe' i]",
    "[data-testid*='application-dossier' i]",
    "[data-testid*='application-document' i]",
    "[data-document-type='bewerbermappe']",
    "[data-action*='bewerbermappe' i]",
    "input[name*='bewerbermappe' i]",
    "input[value*='bewerbermappe' i]",
    "[aria-controls*='bewerbermappe' i]",
)
WG_DOSSIER_VERIFIED_SELECTORS = (
    ".pre_attached_application_package:has(#detach_application_package)",
    "[data-document-type='bewerbermappe'][data-state='attached' i]",
    "[data-testid*='bewerbermappe' i][data-state='attached' i]",
    "[data-testid*='bewerbermappe' i][data-selected='true']",
    "[data-testid*='attached-document' i][data-document-type='bewerbermappe']",
    "input[name*='bewerbermappe' i]:checked",
    "input[value*='bewerbermappe' i]:checked",
)
WG_PHOTO_ATTACHED_SELECTORS = (
    ".pre_attached_files",
    "#pre_attached_files",
    ".pre_attached_file",
    "[data-attachment-state='attached']",
    "[data-testid*='attached-file' i]",
    "#message_template .attachments",
    "#message_template .attachment",
)
MESSAGE_SELECTORS = (
    "textarea#message_input",
    "textarea[name='content']",
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
    "button[data-action*='send-message' i]",
    "button[data-testid*='send-message' i]",
    "button[type='submit']",
)
# WG-Gesucht intermittently shows a security/privacy/policy acceptance popup after
# navigation or after clicking the Premium/message actions -- sometimes, not always.
# Deliberately NOT a bare `[role='dialog']`/`[aria-modal='true']`: WG-Gesucht's own
# legitimate Premium priority popup menu uses that exact same accessible-dialog
# markup (see activate_wg_plus_priority's menu structure), so a bare selector would
# risk matching and probing buttons inside that unrelated, already-working popup.
# Every selector here instead requires an explicit cookie/consent/privacy/security/
# policy signal, which real WG UI controls (premium, message, dossier) never carry.
WG_POLICY_MODAL_CONTAINER_SELECTORS = (
    # Actual WG-Gesucht composer overlay seen in the 18/19 September failures.
    # Its generic Bootstrap modal/backdrop intercepts the attachment-menu click.
    "#sec_advice.modal",
    "#usercentrics-root",
    "#onetrust-consent-sdk",
    "#cookiebot",
    "[id*='cookie-consent' i]",
    "[id*='cookiebanner' i]",
    "[data-testid*='consent' i]",
    "[data-testid*='cookie' i]",
    "[data-testid*='security' i][data-testid*='modal' i]",
    "[data-testid*='privacy' i][data-testid*='modal' i]",
    "[data-testid*='policy' i][data-testid*='modal' i]",
    "[class*='cookie-consent' i]",
    "[class*='consent-banner' i]",
    "[aria-label*='cookie' i]",
    "[aria-label*='datenschutz' i]",
    "[aria-label*='privacy' i]",
    "[aria-label*='security' i][aria-label*='notice' i]",
)
WG_POLICY_MODAL_ACCEPT_TEXT_PATTERN = re.compile(
    r"akzeptieren|zustimmen|verstanden|einverstanden|bestätigen|bestaetigen|"
    r"sicherheitstipps gelesen|weiter\b|fortfahren|accept|agree|continue|confirm|got it|\bok\b",
    re.I,
)


def _launch_persistent(
    browser_type: BrowserType, settings: Settings, *, headless: bool
) -> BrowserContext:
    channel = os.getenv("BROWSER_CHANNEL", "chrome").strip()
    context = browser_type.launch_persistent_context(
        str(settings.browser_profile_path), headless=headless, channel=channel or None
    )
    state_path = settings.browser_profile_path / "storage-state.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            cookies = state.get("cookies", []) if isinstance(state, dict) else []
            if cookies:
                context.add_cookies(cookies)
        except Exception:
            logger.exception("saved WG browser state could not be restored")
    return context


def _first_visible(scope: Any, selectors: tuple[str, ...], timeout: int = 500) -> Locator | None:
    for selector in selectors:
        locator = scope.locator(selector)
        try:
            for index in range(min(locator.count(), 12)):
                candidate = locator.nth(index)
                if candidate.is_visible(timeout=timeout):
                    return candidate
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


def _capture_diagnostics(page: Page, artifact: Path, reason: str) -> str | None:
    screenshot = artifact.with_name(artifact.name + "_failure.png")
    try:
        page.screenshot(path=str(screenshot), full_page=True)
        artifact.with_name(artifact.name + "_failure.html").write_text(
            page.content(), encoding="utf-8"
        )
        artifact.with_name(artifact.name + "_failure.json").write_text(
            json.dumps(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "url": page.url,
                    "title": page.title(),
                    "reason": reason[:500],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return str(screenshot)
    except Exception:
        logger.exception("failed to capture browser failure artifacts")
        return None


def _page_requires_user_action(page: Page) -> str | None:
    url = page.url.casefold()
    if "modal=sign_in" in url or "/login" in url or "/anmelden" in url:
        return "WG-Gesucht login expired or missing; run ./login.sh wg"
    try:
        body = page.locator("body").inner_text(timeout=3000).casefold()
    except Exception:
        return None
    if any(
        marker in body for marker in ("captcha", "ich bin kein roboter", "verify you are human")
    ):
        return "CAPTCHA/human verification detected; run ./login.sh wg"
    return None


def dismiss_or_accept_policy_modal_if_present(page: Page) -> bool:
    """Detect and accept WG-Gesucht's intermittent security/privacy/policy popup.

    Called right after navigation and right after clicking the Premium/message
    actions, since the popup only sometimes appears at those points. A complete
    no-op when no modal is present (the common case); never raises, and only ever
    clicks a button that is actually inside a detected modal/dialog container, so it
    cannot accidentally click an unrelated page element.
    """
    try:
        modal = _first_visible(page, WG_POLICY_MODAL_CONTAINER_SELECTORS, timeout=600)
        if modal is None:
            return False
        button = None
        candidates = modal.locator(
            "button, a[role='button'], [role='button'], input[type='button']"
        )
        for index in range(min(candidates.count(), 10)):
            candidate = candidates.nth(index)
            try:
                if not candidate.is_visible(timeout=150):
                    continue
                text = candidate.inner_text(timeout=150)
            except Exception:
                continue
            if WG_POLICY_MODAL_ACCEPT_TEXT_PATTERN.search(text or ""):
                button = candidate
                break
        if button is None:
            return False
        button.click(timeout=1000)
        page.wait_for_timeout(300)
        logger.info("WG-Gesucht policy/security popup detected and accepted")
        return True
    except Exception as exc:
        logger.warning(
            "policy/security popup handling failed; continuing without it",
            extra={"fields": {"error": str(exc)[:300]}},
        )
        return False


def _listing_id_from_url(url: str) -> str:
    match = re.search(r"\.(\d{5,})\.html(?:[?#]|$)", url)
    return match.group(1) if match else ""


def inspect_wg_listing_page(page: Page, expected_listing_id: str = "") -> WGListingInspection:
    user_action = _page_requires_user_action(page)
    if user_action:
        return WGListingInspection(state="requires_user_action", detail=user_action)
    actual_id = _listing_id_from_url(page.url)
    if expected_listing_id and actual_id and expected_listing_id != actual_id:
        return WGListingInspection(
            state="unknown",
            listing_id=actual_id,
            detail=f"listing identity mismatch: expected {expected_listing_id}, got {actual_id}",
        )
    conversation = _first_visible(page, WG_CONVERSATION_ACTION_SELECTORS)
    if conversation is not None:
        return WGListingInspection(
            state="already_contacted",
            listing_id=actual_id,
            detail="existing conversation action detected from DOM destination/state",
        )
    new_action = _first_visible(page, WG_NEW_ACTION_SELECTORS)
    if new_action is not None:
        return WGListingInspection(
            state="new",
            listing_id=actual_id,
            detail="new-message action detected from DOM destination/state",
        )
    try:
        body = page.locator("body").inner_text(timeout=3000).casefold()
    except Exception:
        body = ""
    unavailable_markers = (
        "anzeige ist nicht mehr verfügbar",
        "angebot ist nicht mehr verfügbar",
        "anzeige wurde deaktiviert",
        "listing is no longer available",
    )
    if any(marker in body for marker in unavailable_markers):
        return WGListingInspection(
            state="unavailable", listing_id=actual_id, detail="listing is no longer available"
        )
    return WGListingInspection(
        state="unknown",
        listing_id=actual_id,
        detail="neither a new-message nor an existing-conversation action was found",
    )


SENT_STRUCTURAL_MARKER_SELECTORS = (
    "[data-message-state='sent']",
    "[data-testid*='message-sent' i]",
    "a[href*='/nachrichten'][data-conversation-id]",
)
CONVERSATION_URL_PATTERN = re.compile(r"/nachricht\.html\?nachrichten-id=|/nachrichten(?:/|\?|$)")
RECENT_TIMESTAMP_PATTERN = re.compile(r"\bvor\s+\d+\s+(?:sekunde|minute)n?\b|\bjust now\b", re.I)


def _normalize_fingerprint(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def message_fingerprint(body: str) -> str:
    """A stable hash of a message body, independent of incidental whitespace/line-break
    differences, used to remember exactly what we attempted to send."""
    return hashlib.sha256(_normalize_fingerprint(body).encode("utf-8")).hexdigest()


CHUNK_WORDS = 10


def _fingerprint_chunks(body: str) -> tuple[str, str]:
    """First/last substantial word chunks of a normalized body, used to find the
    message again in visible page text even if WG-Gesucht reformats whitespace."""
    words = _normalize_fingerprint(body).split()
    if not words:
        return "", ""
    chunk_len = min(CHUNK_WORDS, len(words))
    return " ".join(words[:chunk_len]), " ".join(words[-chunk_len:])


@dataclass(frozen=True)
class SentStateEvidence:
    confirmed: bool
    signals: list[str] = field(default_factory=list)


def _collect_sent_signals(
    page: Page, expected_body: str, message_box: Locator | None, listing_id: str
) -> list[str]:
    """Gather independent, structural evidence that a message was actually sent.

    Deliberately does not depend on any single exact DOM marker or German marketing
    copy: WG-Gesucht can change wording/markup at any time, so real confirmation comes
    from combining several weak signals instead of trusting one brittle one.
    """
    signals: list[str] = []
    first_chunk, last_chunk = _fingerprint_chunks(expected_body)
    try:
        page_text = _normalize_fingerprint(page.locator("body").inner_text(timeout=1000))
    except Exception:
        page_text = ""
    if first_chunk and first_chunk in page_text:
        signals.append("message_fingerprint_start_matched")
    if last_chunk and last_chunk in page_text:
        signals.append("message_fingerprint_end_matched")
    if CONVERSATION_URL_PATTERN.search(page.url.casefold()):
        signals.append("url_is_conversation_route")
    if _first_visible(page, SENT_STRUCTURAL_MARKER_SELECTORS, timeout=150) is not None:
        signals.append("structural_sent_marker")
    try:
        inspection = inspect_wg_listing_page(page, listing_id)
        if inspection.state == "already_contacted":
            signals.append("listing_state_is_already_contacted")
    except Exception:
        pass
    if message_box is not None:
        try:
            if not _composer_value(message_box):
                signals.append("composer_is_empty")
        except Exception:
            pass
    if _first_visible(page, WG_DOSSIER_VERIFIED_SELECTORS, timeout=150) is not None:
        signals.append("bewerbermappe_shown_attached")
    if RECENT_TIMESTAMP_PATTERN.search(page_text):
        signals.append("recent_timestamp_marker")
    return signals


def _is_send_confirmed(signals: list[str]) -> bool:
    fingerprint_confirmed = (
        "message_fingerprint_start_matched" in signals
        or "message_fingerprint_end_matched" in signals
    )
    conversation_confirmed = (
        "url_is_conversation_route" in signals or "listing_state_is_already_contacted" in signals
    )
    return (
        "structural_sent_marker" in signals
        or (fingerprint_confirmed and conversation_confirmed)
        or (fingerprint_confirmed and "composer_is_empty" in signals)
    )


def _conversation_link_href(page: Page) -> str | None:
    link = _first_visible(page, WG_CONVERSATION_ACTION_SELECTORS, timeout=250)
    if link is None:
        return None
    return link.get_attribute("href") or None


def _sent_signals_with_conversation_followup(
    page: Page,
    expected_body: str,
    message_box: Locator | None,
    listing_id: str,
    inspection: WGListingInspection | None = None,
) -> list[str]:
    """Collect sent-state signals from the current page and, if not yet confirmed and
    an existing-conversation action is visible, follow it (read-only navigation, never
    a click) to the WG-Gesucht Messages/conversation page and collect signals there
    too -- the real sent message text lives on that page, not on the listing page
    itself. Shared by post-send verification (`verify_message_sent`, called
    immediately after a real Send click) and read-only reconciliation
    (`reconcile_wg_listing`, used later/independently) so both use exactly the same
    conversation-matching logic; a listing merely showing a "view conversation" link
    is not by itself proof of a send (a dry-run composer visit can expose that same
    link), so the conversation page's own content must be inspected.
    """
    signals = _collect_sent_signals(page, expected_body, message_box, listing_id)
    if _is_send_confirmed(signals):
        return signals
    if inspection is None:
        inspection = inspect_wg_listing_page(page, listing_id)
    if inspection.state == "already_contacted":
        conversation_href = _conversation_link_href(page)
        if conversation_href:
            try:
                page.goto(
                    urljoin(page.url, conversation_href),
                    wait_until="domcontentloaded",
                    timeout=15000,
                )
                page.wait_for_timeout(500)
                conversation_signals = _collect_sent_signals(page, expected_body, None, listing_id)
                signals = list(
                    dict.fromkeys([*signals, *conversation_signals, "followed_conversation_link"])
                )
            except Exception:
                signals = list(dict.fromkeys([*signals, "conversation_link_navigation_failed"]))
    return signals


def verify_message_sent(
    page: Page,
    expected_body: str,
    message_box: Locator | None,
    listing_id: str,
    *,
    poll_seconds: float = 25.0,
    poll_interval_ms: int = 1000,
) -> SentStateEvidence:
    """Poll for up to `poll_seconds` after a Send click, combining multiple signals
    instead of one exact marker, and following the conversation link to WG-Gesucht
    Messages (see `_sent_signals_with_conversation_followup`) if the current page
    alone is not enough to confirm. Never raises and never clicks anything further:
    the caller must treat a non-confirmed result as `send_state_unknown`, never as a
    confident failure, and must never click Send again."""
    deadline = time.monotonic() + poll_seconds
    while True:
        signals = _sent_signals_with_conversation_followup(
            page, expected_body, message_box, listing_id
        )
        confirmed = _is_send_confirmed(signals)
        if confirmed or time.monotonic() >= deadline:
            return SentStateEvidence(confirmed=confirmed, signals=signals)
        page.wait_for_timeout(poll_interval_ms)


@dataclass(frozen=True)
class ReconciliationOutcome:
    result: Literal["sent", "not_sent", "send_state_unknown"]
    signals: list[str] = field(default_factory=list)
    detail: str = ""


def reconcile_wg_listing(page: Page, expected_body: str, listing_id: str) -> ReconciliationOutcome:
    """Read-only reconciliation: inspects the current page, and the conversation page
    it links to if any, for evidence that our application is already present. NEVER
    clicks anything (only `page.goto`, never `.click()`), so it is always safe to run
    regardless of AUTO_SEND/DRY_RUN and cannot cause a duplicate send.
    """
    inspection = inspect_wg_listing_page(page, listing_id)
    signals = _sent_signals_with_conversation_followup(
        page, expected_body, None, listing_id, inspection
    )
    if _is_send_confirmed(signals):
        return ReconciliationOutcome(
            result="sent",
            signals=signals,
            detail="application found in the WG-Gesucht conversation for this listing",
        )
    fingerprint_seen = any(signal.startswith("message_fingerprint") for signal in signals)
    conversation_seen = any(
        signal in signals
        for signal in ("url_is_conversation_route", "listing_state_is_already_contacted")
    )
    if inspection.state == "new" and not fingerprint_seen and not conversation_seen:
        return ReconciliationOutcome(
            result="not_sent",
            signals=signals,
            detail="listing still shows a new-message action; no prior application was found",
        )
    return ReconciliationOutcome(
        result="send_state_unknown",
        signals=signals,
        detail="could not positively confirm or rule out a prior send from the current page state",
    )


def reconcile_wg_url(
    url: str, settings: Settings, expected_body: str, listing_id: str = ""
) -> ReconciliationOutcome:
    """Open a WG-Gesucht listing/conversation read-only and reconcile whether our
    application is already present. Never clicks Send."""
    from playwright.sync_api import sync_playwright

    settings.browser_profile_path.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = _launch_persistent(
            playwright.chromium, settings, headless=settings.browser_headless
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(settings.browser_timeout_seconds * 1000)
        try:
            _goto_with_retry(page, url, settings.browser_timeout_seconds * 1000)
            page.wait_for_timeout(600)
            return reconcile_wg_listing(
                page, expected_body, listing_id or _listing_id_from_url(url)
            )
        finally:
            context.close()


def _control_is_active(control: Locator) -> bool:
    try:
        if control.is_checked():
            return True
    except Exception:
        pass
    values = " ".join(
        (control.get_attribute(name) or "").casefold()
        for name in ("aria-pressed", "aria-checked", "data-state", "data-selected", "class")
    )
    return bool(
        re.search(r"\b(true|active|activated|selected|attached|checked)\b", values)
        and "inactive" not in values
    )


def _premium_is_verified(page: Page) -> bool:
    if _first_visible(page, WG_PREMIUM_VERIFIED_SELECTORS, timeout=150) is not None:
        return True
    candidates = page.locator(
        "[data-feature*='priority' i], [data-feature*='boost' i], "
        "[data-testid*='priority' i], [data-testid*='boost' i]"
    )
    for index in range(min(candidates.count(), 20)):
        candidate = candidates.nth(index)
        try:
            if candidate.is_visible(timeout=100) and _control_is_active(candidate):
                return True
        except Exception:
            continue
    confirmation = page.get_by_text(
        re.compile(
            r"(?:priorit|nachrichten?\s+ganz\s+oben|mitbewerber).{0,45}"
            r"(?:aktiv|aktiviert|erfolgreich)",
            re.I,
        )
    )
    try:
        return any(confirmation.nth(i).is_visible(timeout=100) for i in range(confirmation.count()))
    except Exception:
        return False


def _account_priority_entitlement_is_active(page: Page) -> bool:
    """Verify WG+ entitlement using the authenticated shop redirect observed in production.

    For an active WG+ account, WG applies message priority account-wide: the composer has no
    upsell toggle and `/wgg-plus-shop` redirects to the Bewerbermappe account area. Non-members
    retain `#wgg_plus_toggle`, whose own site JavaScript redirects to the shop on send.
    """
    if "wg-gesucht.de/nachricht-senden/" not in page.url.casefold():
        return False
    if page.locator("#wgg_plus_toggle").count():
        return False
    if not page.locator("#messenger_form").count() or not page.locator("#message_input").count():
        return False
    try:
        response = page.context.request.get(
            urljoin(page.url, "/wgg-plus-shop"),
            timeout=10_000,
            max_redirects=5,
        )
        final_url = response.url.casefold()
        return response.ok and "mein-wg-gesucht-applicant-portfolio" in final_url
    except Exception:
        return False


def _premium_entry_by_structure(page: Page) -> Locator | None:
    entry = _first_visible(page, WG_PREMIUM_ENTRY_SELECTORS, timeout=250)
    if entry is not None:
        return entry
    primary = _first_visible(page, WG_NEW_ACTION_SELECTORS + WG_CONVERSATION_ACTION_SELECTORS)
    if primary is None:
        return None
    try:
        ancestors = primary.locator("xpath=ancestor::div[count(.//a | .//button) <= 8]")
        for index in range(min(ancestors.count(), 5)):
            candidate = _first_visible(
                ancestors.nth(index),
                (
                    "a[href*='wgg-plus' i]",
                    "[class*='premium' i][role='button']",
                    "[class*='plus' i][role='button']",
                    "button:has(img[src*='plus' i])",
                    "a:has(img[src*='plus' i])",
                ),
                timeout=150,
            )
            if candidate is not None:
                return candidate
    except Exception:
        return None
    return None


def _priority_action(page: Page) -> Locator | None:
    roots = page.locator(
        "[role='dialog'], [aria-modal='true'], .modal.show, .modal.in, [role='menu']"
    )
    for index in range(roots.count()):
        root = roots.nth(index)
        try:
            if not root.is_visible(timeout=150):
                continue
        except Exception:
            continue
        action = _first_visible(root, WG_PRIORITY_ACTION_SELECTORS, timeout=150)
        if action is not None:
            return action
        buttons = root.locator("button, a, [role='button'], label")
        for button_index in range(min(buttons.count(), 30)):
            button = buttons.nth(button_index)
            try:
                text = button.inner_text(timeout=150).casefold()
                if button.is_visible(timeout=150) and re.search(
                    r"priorit|nachricht.{0,12}oben|mitbewerber.{0,12}überhol|inbox.{0,12}boost",
                    text,
                ):
                    return button
            except Exception:
                continue
    return _first_visible(page, WG_PRIORITY_ACTION_SELECTORS, timeout=150)


def activate_wg_plus_priority(page: Page) -> PremiumBoostResult:
    """Activate and positively verify the actual WG+ message-priority feature.

    The production record distinguishes how far the flow actually got instead of
    collapsing everything into a single "activated" state: an entry being present in
    the DOM, an account-wide entitlement merely being plausible, an action having been
    clicked, and the priority state being independently confirmed are all different
    levels of evidence. Only `premium_priority_verified` (verified=True) counts as
    success in strict mode; rotating WG-Gesucht+ marketing copy never counts.
    """
    user_action = _page_requires_user_action(page)
    if user_action:
        return PremiumBoostResult(state="requires_user_action", detail=user_action)
    if _premium_is_verified(page):
        return PremiumBoostResult(
            state="premium_priority_verified",
            verified=True,
            detail="priority state structurally verified as active",
        )
    entry = _premium_entry_by_structure(page)
    if entry is None:
        return PremiumBoostResult(
            state="not_available", detail="structural WG+ listing control was not available"
        )
    entry.evaluate("element => element.setAttribute('data-agent-premium-entry', 'true')")
    href = (entry.get_attribute("href") or "").casefold()
    if "wgg-plus-shop" in href:
        return PremiumBoostResult(
            state="premium_entry_found",
            detail="WG+ control leads to the subscription shop, not a priority action",
        )
    try:
        if entry.is_disabled():
            return PremiumBoostResult(
                state="premium_entry_found", detail="WG+ listing control is disabled"
            )
    except Exception:
        pass
    try:
        entry.click()
        page.wait_for_timeout(400)
        dismiss_or_accept_policy_modal_if_present(page)
    except Exception as exc:
        return PremiumBoostResult(state="failed", detail=f"WG+ entry click failed: {exc}"[:500])
    user_action = _page_requires_user_action(page)
    if user_action:
        return PremiumBoostResult(state="requires_user_action", detail=user_action)
    if "wgg-plus-shop" in page.url.casefold():
        return PremiumBoostResult(
            state="premium_entry_found", detail="WG+ entry opened the subscription shop"
        )
    if _premium_is_verified(page):
        return PremiumBoostResult(
            state="premium_priority_verified",
            verified=True,
            detail="priority activation structurally verified",
        )
    if _account_priority_entitlement_is_active(page):
        # The shop-redirect heuristic only shows the account CAN have priority, not that
        # this specific composer/listing has it active right now. Do not treat generic
        # WG-Gesucht+ account presence as proof; strict mode still blocks on this alone.
        return PremiumBoostResult(
            state="premium_priority_available",
            verified=False,
            detail=(
                "account-wide WG+ entitlement looks plausible via the shop redirect, but no "
                "direct per-message priority state was structurally confirmed on this "
                "composer; treated as unverified in strict mode"
            ),
        )
    action = _priority_action(page)
    if action is None:
        try:
            page_text = page.locator("body").inner_text(timeout=1500).casefold()
        except Exception:
            page_text = ""
        if any(word in page_text for word in ("zahlung", "payment", "abonnieren", "freischalten")):
            return PremiumBoostResult(
                state="not_available", detail="priority feature requires purchase/subscription"
            )
        return PremiumBoostResult(
            state="premium_entry_found",
            detail="WG+ opened, but no actual priority action was found",
        )
    if _control_is_active(action):
        return PremiumBoostResult(
            state="premium_priority_verified",
            verified=True,
            detail="priority action is already active",
        )
    try:
        action.click()
        page.wait_for_timeout(500)
        dismiss_or_accept_policy_modal_if_present(page)
    except Exception as exc:
        return PremiumBoostResult(
            state="failed", detail=f"priority action click failed: {exc}"[:500]
        )
    if _premium_is_verified(page) or _control_is_active(action):
        return PremiumBoostResult(
            state="premium_priority_verified",
            verified=True,
            detail="priority activation positively verified",
        )
    return PremiumBoostResult(
        state="premium_priority_activated",
        detail="priority action was clicked, but the active state could not be verified",
    )


def _dossier_is_verified(page: Page) -> bool:
    # Only an attached-document DOM marker / checked account-package input is
    # evidence. A click, generic active class, or successful Playwright action is
    # insufficient proof that WG-Gesucht accepted the Bewerbermappe.
    return _first_visible(page, WG_DOSSIER_VERIFIED_SELECTORS, timeout=150) is not None


def attach_wg_bewerbermappe(page: Page) -> WGBewerbermappeResult:
    """Select the WG-account Bewerbermappe; retry only this pre-Send step.

    A click is not proof of attachment. Each attempt rechecks the security overlay,
    reopens the menu, and reacquires the control because WG-Gesucht can rerender it.
    """
    failures: list[str] = []
    for attempt in range(1, 4):
        dismiss_or_accept_policy_modal_if_present(page)
        user_action = _page_requires_user_action(page)
        if user_action:
            return WGBewerbermappeResult(state="requires_user_action", detail=user_action)
        if _dossier_is_verified(page):
            return WGBewerbermappeResult(
                state="already_attached",
                verified=True,
                detail=f"WG Bewerbermappe visibly attached before attempt {attempt}",
            )
        control = _first_visible(page, WG_DOSSIER_CONTROL_SELECTORS, timeout=250)
        if control is None:
            attachment_menu = _first_visible(
                page,
                (
                    "[data-target='#attachment_options_modal']",
                    "[data-bs-target='#attachment_options_modal']",
                ),
                timeout=250,
            )
            if attachment_menu is not None:
                try:
                    attachment_menu.click(timeout=2000)
                    page.wait_for_timeout(200)
                    dismiss_or_accept_policy_modal_if_present(page)
                    control = _first_visible(page, WG_DOSSIER_CONTROL_SELECTORS, timeout=250)
                except Exception as exc:
                    failures.append(
                        f"attempt {attempt}: attachment menu click failed: {str(exc)[:180]}"
                    )
                    continue
        if control is None:
            labels = page.get_by_text(re.compile(r"^\s*(?:meine\s+)?bewerbermappe\s*$", re.I))
            for index in range(min(labels.count(), 12)):
                candidate = labels.nth(index)
                try:
                    if candidate.is_visible(timeout=150):
                        control = candidate
                        break
                except Exception:
                    continue
        if control is None:
            failures.append(f"attempt {attempt}: Meine Bewerbermappe control not found")
            continue
        try:
            tag = control.evaluate("element => element.tagName.toLowerCase()")
            input_type = (control.get_attribute("type") or "").casefold()
            if tag == "input" and input_type in {"checkbox", "radio"}:
                control.check(timeout=2000)
            else:
                control.click(timeout=2000)
            page.wait_for_timeout(350)
        except Exception as exc:
            failures.append(f"attempt {attempt}: selection failed: {str(exc)[:180]}")
            continue
        if _dossier_is_verified(page):
            return WGBewerbermappeResult(
                state="attached",
                verified=True,
                detail=f"WG Bewerbermappe visible in composer after attempt {attempt}",
            )
        failures.append(
            f"attempt {attempt}: selection clicked but no attached-document DOM evidence"
        )
    unavailable = all("control not found" in failure for failure in failures)
    return WGBewerbermappeResult(
        state="not_available" if unavailable else "failed",
        detail="; ".join(failures)[:1000],
    )


def _photo_is_visibly_attached(page: Page, filename: str) -> bool:
    """A selected input or successful upload call is not an attached-file signal."""
    for selector in WG_PHOTO_ATTACHED_SELECTORS:
        try:
            matches = page.locator(selector).filter(has_text=filename)
            for index in range(min(matches.count(), 12)):
                if matches.nth(index).is_visible(timeout=150):
                    return True
        except Exception:
            continue
    return False


def attach_wg_applicant_photo(page: Page, photo_path: Path) -> WGBewerbermappeResult:
    """Use WG's Foto/Datei control, then require a visible filename in the composer.

    Reacquire the file input on each bounded pre-Send attempt because the attachment
    modal can rerender. An upload that only reaches WG's library is not sufficient.
    """
    failures: list[str] = []
    for attempt in range(1, 3):
        dismiss_or_accept_policy_modal_if_present(page)
        if _photo_is_visibly_attached(page, photo_path.name):
            return WGBewerbermappeResult(
                state="already_attached",
                verified=True,
                detail=f"photo visibly attached before attempt {attempt}",
            )
        try:
            menu = _first_visible(
                page,
                (
                    "[data-target='#attachment_options_modal']",
                    "[data-bs-target='#attachment_options_modal']",
                ),
                timeout=250,
            )
            if menu is None:
                failures.append(f"attempt {attempt}: attachment menu not found")
                continue
            menu.click(timeout=2000)
            dismiss_or_accept_policy_modal_if_present(page)
            file_option = _first_visible(
                page,
                (
                    "#attachment_options_modal .attach_file",
                    ".conversation-attachment-option.attach_file",
                ),
                timeout=250,
            )
            if file_option is None:
                failures.append(f"attempt {attempt}: Foto/Datei option not found")
                continue
            file_option.click(timeout=2000)
            dismiss_or_accept_policy_modal_if_present(page)
            file_input = page.locator("#file_input")
            if not file_input.count():
                failures.append(f"attempt {attempt}: WG photo file input not found")
                continue
            file_input.set_input_files(str(photo_path), timeout=3000)
            page.wait_for_timeout(700)
            if _photo_is_visibly_attached(page, photo_path.name):
                return WGBewerbermappeResult(
                    state="attached",
                    verified=True,
                    detail=f"photo filename visible as attached after attempt {attempt}",
                )
            # A library entry may need explicit selection before modal confirmation.
            library_entry = page.locator("#file_storage_wrapper").get_by_text(
                photo_path.name, exact=True
            )
            if library_entry.count() and library_entry.first.is_visible(timeout=200):
                library_entry.first.click(timeout=2000)
            confirm = page.locator("#attachments_modal button[data-dismiss='modal']").filter(
                has_text=re.compile(r"Bestätigen|Confirm", re.I)
            )
            if confirm.count() and confirm.first.is_visible(timeout=200):
                confirm.first.click(timeout=2000)
            page.wait_for_timeout(500)
            if _photo_is_visibly_attached(page, photo_path.name):
                return WGBewerbermappeResult(
                    state="attached",
                    verified=True,
                    detail=f"photo filename visible as attached after attempt {attempt}",
                )
            failures.append(
                f"attempt {attempt}: upload attempted but no attached-photo DOM evidence"
            )
        except Exception as exc:
            failures.append(f"attempt {attempt}: photo attachment failed: {str(exc)[:180]}")
    return WGBewerbermappeResult(state="failed", detail="; ".join(failures)[:1000])


def login_wg(settings: Settings) -> None:
    from playwright.sync_api import sync_playwright

    settings.browser_profile_path.mkdir(parents=True, exist_ok=True)
    login_url = os.getenv("WG_LOGIN_URL", "https://www.wg-gesucht.de/mein-wg-gesucht.html")
    with sync_playwright() as playwright:
        context = _launch_persistent(playwright.chromium, settings, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        _goto_with_retry(page, login_url, settings.browser_timeout_seconds * 1000)
        while True:
            print("Complete WG-Gesucht login/CAPTCHA/2FA in the browser, then press Enter here.")
            input()
            verifier = context.new_page()
            _goto_with_retry(verifier, login_url, settings.browser_timeout_seconds * 1000)
            verifier.wait_for_timeout(2000)
            if _page_requires_user_action(verifier) is None:
                state_path = settings.browser_profile_path / "storage-state.json"
                context.storage_state(path=str(state_path))
                state_path.chmod(0o600)
                verifier.close()
                print("WG-Gesucht login verified and saved.")
                break
            verifier.close()
            print("Login is not active in this browser profile yet; the window remains open.")
        context.close()


def browser_profile_has_session(settings: Settings) -> bool:
    if not settings.browser_profile_path.is_dir():
        return False
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            context = _launch_persistent(playwright.chromium, settings, headless=True)
            page = context.pages[0] if context.pages else context.new_page()
            _goto_with_retry(
                page,
                os.getenv("WG_LOGIN_URL", "https://www.wg-gesucht.de/mein-wg-gesucht.html"),
                settings.browser_timeout_seconds * 1000,
            )
            page.wait_for_timeout(2000)
            authenticated = _page_requires_user_action(page) is None
            context.close()
            return authenticated
    except Exception:
        logger.exception("live WG session verification failed")
        return False


def fetch_wg_listing_snapshot(
    outcome_url: str, settings: Settings
) -> tuple[str, WGListingInspection]:
    """Fetch visible listing text and contact/conversation state in one browser visit."""
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
            user_action = _page_requires_user_action(page)
            if user_action:
                raise RuntimeError(user_action)
            text = _extract_wg_listing_text(page)
            if not text:
                raise RuntimeError("listing text container was not found")
            inspection = inspect_wg_listing_page(page, _listing_id_from_url(outcome_url))
            return text.strip(), inspection
        finally:
            context.close()


def fetch_full_listing(outcome_url: str, settings: Settings) -> str:
    text, _inspection = fetch_wg_listing_snapshot(outcome_url, settings)
    return text


def _extract_wg_listing_text(page: Page) -> str:
    """Return visible facts plus every WG description tab, including hidden tabs."""
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
            recommendation_selector = "#similar_ads_swiper, [id^='similar_ads_'], .similar_ads"
            for recommendation in locator.locator(recommendation_selector).all_inner_texts():
                if recommendation.strip():
                    candidate = candidate.replace(recommendation.strip(), "")
            if len(candidate) > 100:
                text = candidate
                break

    # WG-Gesucht renders Zimmer/Lage/WG-Leben/Sonstiges in separate freitext
    # nodes and hides three of them with CSS. inner_text() omits those nodes,
    # so collect their textContent explicitly before deterministic analysis.
    descriptions: list[str] = []
    for raw in page.locator("[id^='freitext_']").all_text_contents():
        candidate = re.sub(r"\s+", " ", raw).strip()
        if candidate and candidate.casefold() not in text.casefold():
            descriptions.append(candidate)
    if descriptions:
        text = f"{text}\n\n" + "\n\n".join(descriptions)
    return text.strip()


def _composer_value(message_box: Locator) -> str:
    if message_box.get_attribute("contenteditable") == "true":
        return (message_box.inner_text() or "").strip()
    return message_box.input_value().strip()


def _fill_identity(page: Page) -> None:
    applicant = load_yaml("config.yaml")["applicant"]
    for selectors, value in (
        (NAME_SELECTORS, applicant["name"]),
        (EMAIL_SELECTORS, applicant["email"]),
        (PHONE_SELECTORS, applicant["phone"]),
    ):
        field = _first_visible(page, selectors)
        if field is not None and not field.input_value():
            field.fill(str(value))


def _prepare_wg_contact(
    outcome: AnalysisOutcome,
    settings: Settings,
    send_permitted: bool,
    claim_actual: Callable[[], bool] | None,
    record_send_clicked: Callable[[str, str, str, str, str], None] | None = None,
) -> ContactResult:
    from playwright.sync_api import sync_playwright

    photo_required = (
        outcome.facts.photo_required_with_first_message
        or photo_required_for_initial_contact(outcome.listing.raw_text)
    )
    photo_path = settings.applicant_photo_path if photo_required else None
    if photo_required:
        photo_error = validate_applicant_photo(photo_path, load_yaml("config.yaml"))
        if photo_error:
            return ContactResult(status="review_required", detail=photo_error)
    artifact = _artifact_prefix(outcome.listing.listing_id)
    settings.browser_profile_path.mkdir(parents=True, exist_ok=True)
    send_clicked = False
    with sync_playwright() as playwright:
        context = _launch_persistent(
            playwright.chromium, settings, headless=settings.browser_headless
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(settings.browser_timeout_seconds * 1000)
        try:
            _goto_with_retry(page, outcome.listing.url, settings.browser_timeout_seconds * 1000)
            page.wait_for_timeout(700)
            dismiss_or_accept_policy_modal_if_present(page)
            inspection = inspect_wg_listing_page(page, outcome.listing.listing_id)
            if inspection.state == "already_contacted":
                return ContactResult(status="already_contacted", detail=inspection.detail)
            if inspection.state != "new":
                screenshot = _capture_diagnostics(page, artifact, inspection.detail)
                return ContactResult(
                    status="review_required",
                    detail=inspection.detail,
                    screenshot_path=screenshot,
                )
            premium = (
                activate_wg_plus_priority(page)
                if settings.wg_use_premium_boost
                else PremiumBoostResult(
                    state="not_available", detail="WG premium boost disabled by configuration"
                )
            )
            premium_warning: str | None = None
            if not premium.verified:
                if settings.wg_premium_strict:
                    screenshot = _capture_diagnostics(page, artifact, premium.detail)
                    return ContactResult(
                        status="premium_boost_failed",
                        detail=premium.detail,
                        screenshot_path=screenshot,
                    )
                # WG+ priority is a bonus, not a hard requirement: an unverifiable boost
                # must never cost a good apartment by itself. Continue normally and just
                # record it. If the premium interaction genuinely broke the page, the
                # composer lookup right below will fail on its own and surface that.
                premium_warning = f"premium_boost_unverified ({premium.state}): {premium.detail}"
                logger.warning(
                    "WG+ priority could not be verified; continuing without it "
                    "(WG_PREMIUM_STRICT=false)",
                    extra={
                        "fields": {
                            "listing_id": outcome.listing.listing_id,
                            "premium_state": premium.state,
                        }
                    },
                )
            message_box = _first_visible(page, MESSAGE_SELECTORS)
            if message_box is None:
                contact_action = _first_visible(page, WG_NEW_ACTION_SELECTORS)
                if contact_action is None:
                    raise RuntimeError("new-message action disappeared before composer open")
                contact_action.click()
                page.wait_for_timeout(700)
                dismiss_or_accept_policy_modal_if_present(page)
            user_action = _page_requires_user_action(page)
            if user_action:
                screenshot = _capture_diagnostics(page, artifact, user_action)
                return ContactResult(
                    status="review_required", detail=user_action, screenshot_path=screenshot
                )
            message_box = _first_visible(page, MESSAGE_SELECTORS)
            if message_box is None:
                raise RuntimeError("message composer was not found after new-message action")
            assert outcome.message is not None
            message_box.fill(outcome.message.body)
            subject = _first_visible(page, SUBJECT_SELECTORS)
            if subject is not None and outcome.message.subject:
                subject.fill(outcome.message.subject)
            _fill_identity(page)
            dossier = attach_wg_bewerbermappe(page)
            if not dossier.verified:
                screenshot = _capture_diagnostics(page, artifact, dossier.detail)
                return ContactResult(
                    status="bewerbermappe_attachment_failed",
                    detail=dossier.detail,
                    screenshot_path=screenshot,
                )
            photo = None
            if photo_required:
                assert photo_path is not None
                photo = attach_wg_applicant_photo(page, photo_path)
                if not photo.verified:
                    screenshot = _capture_diagnostics(page, artifact, photo.detail)
                    return ContactResult(
                        status="review_required",
                        detail=(
                            "required applicant photo could not be visibly attached before Send: "
                            + photo.detail
                        ),
                        screenshot_path=screenshot,
                    )
            intended_message = outcome.message.body.strip()
            composer_matches = _composer_value(message_box) == intended_message
            final_errors: list[str] = []
            if outcome.rule_decision.decision != "APPLY":
                final_errors.append("listing is not eligible")
            if not outcome.validation.auto_send_allowed:
                final_errors.append("deterministic message validation did not pass")
            if outcome.facts.scam_risk != "low" or outcome.facts.critical_ambiguities:
                final_errors.append("scam/document legitimacy gate did not pass")
            if not premium.verified and settings.wg_premium_strict:
                final_errors.append("actual WG+ priority was not verified")
            if not dossier.verified:
                final_errors.append("WG Bewerbermappe attachment was not verified")
            if photo_required and (
                photo is None
                or not photo.verified
                or not _photo_is_visibly_attached(page, photo_path.name)
            ):
                final_errors.append("required applicant photo attachment was not verified")
            if not composer_matches:
                final_errors.append("composer content does not match intended message")
            user_action = _page_requires_user_action(page)
            if user_action:
                final_errors.append(user_action)
            ready_screenshot = artifact.with_suffix(".png")
            page.screenshot(path=str(ready_screenshot), full_page=True)
            if not send_permitted:
                priority_bit = "priority verified" if premium.verified else premium_warning
                detail = (
                    f"{priority_bit}; composer filled; WG Bewerbermappe attached; "
                    + ("required photo attached; " if photo_required else "")
                    + "stopped before Send"
                )
                if final_errors:
                    detail += "; blocked: " + "; ".join(final_errors)
                if not settings.browser_headless and sys.stdin.isatty():
                    input(
                        "DRY_RUN is ready in Chrome. Verify it visually, "
                        "then press Enter to close. "
                    )
                return ContactResult(
                    status="dry_run_ready", screenshot_path=str(ready_screenshot), detail=detail
                )
            if final_errors:
                failure = "; ".join(final_errors)
                failure_screenshot = _capture_diagnostics(page, artifact, failure)
                return ContactResult(
                    status="review_required",
                    detail=f"final safety gate blocked: {failure}",
                    screenshot_path=failure_screenshot,
                )
            if claim_actual is None or not claim_actual():
                return ContactResult(
                    status="already_contacted",
                    detail="duplicate actual contact blocked by SQLite final gate",
                )
            send_scope: Any = message_box.locator("xpath=ancestor::form[1]")
            if not send_scope.count():
                send_scope = page
            send = _first_visible(send_scope, SEND_SELECTORS)
            if send is None or not send.is_enabled():
                raise RuntimeError("Send button unavailable after every final gate passed")
            # From here on the message may already be on its way: never call this path
            # "send_failed" again, since that would wrongly permit an automatic retry
            # and risk a duplicate application. Persist what we attempted immediately,
            # before spending any time waiting for confirmation.
            send.click()
            send_clicked = True
            fingerprint = message_fingerprint(intended_message)
            if record_send_clicked is not None:
                try:
                    attachment_state = dossier.state + (";photo:verified" if photo_required else "")
                    record_send_clicked(
                        fingerprint, intended_message, premium.state, attachment_state, page.url
                    )
                except Exception:
                    logger.exception(
                        "failed to persist send-attempt record immediately after Send click"
                    )
            evidence = verify_message_sent(
                page,
                intended_message,
                message_box,
                outcome.listing.listing_id,
                poll_seconds=float(settings.browser_timeout_seconds),
            )
            if evidence.confirmed:
                sent_detail = "sent state positively verified via: " + ", ".join(evidence.signals)
                if photo_required:
                    sent_detail += "; required photo visibly attached before Send"
                if premium_warning:
                    sent_detail += f"; {premium_warning}"
                return ContactResult(
                    status="sent", screenshot_path=str(ready_screenshot), detail=sent_detail
                )
            unknown_screenshot = _capture_diagnostics(
                page, artifact, "send clicked but sent state could not be positively confirmed"
            )
            return ContactResult(
                status="send_state_unknown",
                screenshot_path=unknown_screenshot or str(ready_screenshot),
                detail=(
                    "Send was clicked but the sent state could not be positively confirmed. "
                    "Do not retry automatically; run ./reconcile_send.sh for this listing. "
                    "Signals observed: " + (", ".join(evidence.signals) or "none")
                ),
            )
        except Exception as exc:
            failure_screenshot = _capture_diagnostics(page, artifact, str(exc))
            if send_clicked:
                return ContactResult(
                    status="send_state_unknown",
                    detail=(
                        "Send was clicked but an error interrupted sent-state verification; "
                        "do not retry automatically, run ./reconcile_send.sh instead: "
                        f"{str(exc)[:400]}"
                    ),
                    screenshot_path=failure_screenshot,
                )
            return ContactResult(
                status="send_failed", detail=str(exc)[:500], screenshot_path=failure_screenshot
            )
        finally:
            context.close()


def _prepare_generic_platform_contact(
    outcome: AnalysisOutcome,
    settings: Settings,
    send_permitted: bool,
    claim_actual: Callable[[], bool] | None,
) -> ContactResult:
    from playwright.sync_api import sync_playwright

    artifact = _artifact_prefix(outcome.listing.listing_id)
    with sync_playwright() as playwright:
        context = _launch_persistent(
            playwright.chromium, settings, headless=settings.browser_headless
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_default_timeout(settings.browser_timeout_seconds * 1000)
        try:
            _goto_with_retry(page, outcome.listing.url, settings.browser_timeout_seconds * 1000)
            message_box = _first_visible(page, MESSAGE_SELECTORS)
            if message_box is None:
                raise RuntimeError("message composer was not found")
            assert outcome.message is not None
            message_box.fill(outcome.message.body)
            if outcome.attachment.should_attach and outcome.attachment.path:
                file_input = page.locator("input[type='file']").first
                if not file_input.count():
                    raise RuntimeError(
                        "allowed local attachment requested but file input was not found"
                    )
                file_input.set_input_files(outcome.attachment.path)
            ready_screenshot = artifact.with_suffix(".png")
            page.screenshot(path=str(ready_screenshot), full_page=True)
            if not send_permitted:
                return ContactResult(
                    status="dry_run_ready",
                    screenshot_path=str(ready_screenshot),
                    detail="composer filled; stopped before Send",
                )
            if claim_actual is None or not claim_actual():
                return ContactResult(
                    status="already_contacted", detail="duplicate actual contact blocked by SQLite"
                )
            send = _first_visible(page, SEND_SELECTORS)
            if send is None or not send.is_enabled():
                raise RuntimeError("Send button unavailable")
            send.click()
            return ContactResult(status="sent", screenshot_path=str(ready_screenshot))
        except Exception as exc:
            failure_screenshot = _capture_diagnostics(page, artifact, str(exc))
            return ContactResult(
                status="send_failed", detail=str(exc)[:500], screenshot_path=failure_screenshot
            )
        finally:
            context.close()


def prepare_platform_contact(
    outcome: AnalysisOutcome,
    settings: Settings,
    *,
    send_permitted: bool,
    claim_actual: Callable[[], bool] | None = None,
    record_send_clicked: Callable[[str, str, str, str, str], None] | None = None,
) -> ContactResult:
    """`send_permitted` is the caller's already-resolved answer to "may this specific
    trigger send for real right now" (see Settings.send_permitted) -- the one point
    where DRY_RUN/AUTO_SEND/trigger-identity are decided; everything below this only
    ever asks the plain boolean, so there is exactly one source of truth."""
    if not outcome.message or not outcome.listing.url:
        return ContactResult(status="review_required", detail="listing URL or message is missing")
    if not outcome.validation.auto_send_allowed:
        return ContactResult(
            status="review_required", detail="deterministic validation did not pass"
        )
    if outcome.listing.platform == "wg_gesucht":
        return _prepare_wg_contact(
            outcome, settings, send_permitted, claim_actual, record_send_clicked
        )
    return _prepare_generic_platform_contact(outcome, settings, send_permitted, claim_actual)
