from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from app.browser import (
    _extract_wg_listing_text,
    activate_wg_plus_priority,
    attach_wg_bewerbermappe,
    dismiss_or_accept_policy_modal_if_present,
    inspect_wg_listing_page,
    prepare_platform_contact,
)
from app.schemas import (
    AnalysisOutcome,
    AttachmentDecision,
    ListingFacts,
    MessageDraft,
    RuleDecision,
    ValidationResult,
)
from app.universal_fallback import GERMAN_WG_TEMPLATE
from tests.conftest import viable_listing


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def _premium_entry(text: str, href: str = "#premium", onclick: str = "") -> str:
    return f"""
    <a href="{href}" data-campaign_type="wggplus_promotion"
       data-campaign_click_source="dav"
       data-campaign_click_position="contact_overtake_competitors_button"
       onclick="{onclick}">{text}</a>
    """


@pytest.mark.parametrize(
    "rotating_text",
    ["Mitbewerber überholen +", "Werbefrei surfen +", "Oben platzieren +"],
)
def test_rotating_promo_copy_is_not_used_as_selector(page, rotating_text: str) -> None:
    page.set_content(_premium_entry(rotating_text, href="/wgg-plus-shop"))
    result = activate_wg_plus_priority(page)
    assert result.state == "premium_entry_found"
    assert not result.verified
    assert "shop" in result.detail


def test_unknown_promo_copy_is_still_found_structurally(page) -> None:
    page.set_content(_premium_entry("Heute schneller ans Ziel +", href="/wgg-plus-shop"))
    result = activate_wg_plus_priority(page)
    assert result.state == "premium_entry_found"
    assert not result.verified


def test_wg_plus_control_present_but_priority_unavailable(page) -> None:
    page.set_content(
        _premium_entry("Whatever", onclick="menu.style.display='block'")
        + "<div id='menu' role='dialog' style='display:none'>Jetzt freischalten</div>"
    )
    assert activate_wg_plus_priority(page).state == "not_available"


def test_priority_activates_and_is_positively_verified(page) -> None:
    page.set_content(
        _premium_entry("Unknown copy", onclick="menu.style.display='block'")
        + """
        <div id="menu" role="dialog" style="display:none">
          <button data-action="message-priority"
            onclick="this.setAttribute('aria-pressed','true')">Priority</button>
        </div>
        """
    )
    result = activate_wg_plus_priority(page)
    assert result.state == "premium_priority_verified"
    assert result.verified


def test_priority_already_active(page) -> None:
    page.set_content("<div data-premium-boost-state='active'>Priority</div>")
    result = activate_wg_plus_priority(page)
    assert result.state == "premium_priority_verified"
    assert result.verified


def test_changed_menu_structure_still_activates(page) -> None:
    page.set_content(
        _premium_entry("Unknown", onclick="menu.style.display='block'")
        + """
        <section id="menu" aria-modal="true" style="display:none">
          <a href="#activate" data-feature="inbox-boost"
             onclick="this.dataset.state='active'">Inbox option</a>
        </section>
        """
    )
    assert activate_wg_plus_priority(page).state == "premium_priority_verified"


def test_priority_click_without_verification_fails(page) -> None:
    page.set_content(
        _premium_entry("Unknown", onclick="menu.style.display='block'")
        + """
        <div id="menu" role="dialog" style="display:none">
          <button data-action="message-priority">Priority</button>
        </div>
        """
    )
    result = activate_wg_plus_priority(page)
    assert result.state == "premium_priority_activated"
    assert not result.verified


def test_generic_wg_plus_account_entitlement_alone_is_not_verified_in_strict_mode(
    page, monkeypatch
) -> None:
    """Reproduces the review concern from the real production run: rotating
    WG-Gesucht+ marketing copy and a plausible account-wide entitlement (via the shop
    redirect heuristic) must never be treated as proof the priority boost is actually
    active on this specific composer."""
    monkeypatch.setattr("app.browser._account_priority_entitlement_is_active", lambda _page: True)
    page.set_content(
        _premium_entry("Werbefrei surfen +", onclick="menu.style.display='block'")
        + """
        <div id="menu" role="dialog" style="display:none">
          <button data-action="message-priority">Priority</button>
        </div>
        """
    )
    result = activate_wg_plus_priority(page)
    assert result.state == "premium_priority_available"
    assert not result.verified


def test_bewerbermappe_attaches_and_verifies(page) -> None:
    page.set_content(
        """
        <button data-testid="bewerbermappe-option"
          onclick="this.dataset.state='attached'">Meine Bewerbermappe</button>
        """
    )
    result = attach_wg_bewerbermappe(page)
    assert result.state == "attached"
    assert result.verified


def test_bewerbermappe_click_without_verification_fails(page) -> None:
    page.set_content("<button data-testid='bewerbermappe-option'>Meine Bewerbermappe</button>")
    result = attach_wg_bewerbermappe(page)
    assert result.state == "failed"
    assert not result.verified


def test_bewerbermappe_ui_unavailable(page) -> None:
    page.set_content("<main>No account documents here</main>")
    assert attach_wg_bewerbermappe(page).state == "not_available"


def test_listing_state_uses_action_destination_not_copy(page) -> None:
    page.set_content("<a href='/nachricht-senden/room.1234567.html'>Anything</a>")
    assert inspect_wg_listing_page(page).state == "new"
    page.set_content(
        "<a class='wgg-btn-primary' href='/nachrichten.html?ad=1234567'>Anything else</a>"
    )
    assert inspect_wg_listing_page(page).state == "already_contacted"
    page.set_content(
        "<a class='wgg-btn-primary' href='/nachricht.html?nachrichten-id=621373677'>"
        "Rotating copy</a>"
    )
    assert inspect_wg_listing_page(page).state == "already_contacted"


def test_global_unread_messages_badge_is_not_mistaken_for_this_listings_conversation(
    page,
) -> None:
    """Real production false positive: WG-Gesucht's site-wide nav bar has a hidden
    "unread messages" badge (`class="... wgg_primary ..."`, `href="/nachrichten.html"`)
    that becomes visible whenever the account has ANY unread message anywhere, entirely
    unrelated to the listing being viewed. A broad `[class*='primary']` substring
    selector previously matched this badge and made every listing look already
    contacted while an unrelated conversation had an unread reply. A real, never
    contacted listing must still report `new` when the badge happens to be visible.
    """
    page.set_content(
        """
        <a id="new_messages_counter_main_navigation"
           class="badge wgg_badge-small wgg_primary wgg_round position-relative"
           href="/nachrichten.html" style="display: block;"></a>
        <a class="wgg-btn wgg-btn-primary wgg-btn-sm"
           href="/nachricht-senden/wg-zimmer-in-Muenster-Geist.13910522.html">Nachricht senden</a>
        """
    )
    result = inspect_wg_listing_page(page)
    assert result.state == "new", result.detail


def test_real_wg_attachment_menu_shape_attaches_and_verifies(page) -> None:
    page.set_content(
        """
        <button type="button" data-target="#attachment_options_modal"
          onclick="menu.style.display='block'">Attachment</button>
        <div id="menu" style="display:none">
          <p id="share_application_package" onclick="
            attached.style.display='block'; this.parentElement.style.display='none'">
            Meine Bewerbermappe
          </p>
        </div>
        <div id="attached" class="pre_attached_application_package" style="display:none">
          Meine Bewerbermappe <button id="detach_application_package">Remove</button>
        </div>
        """
    )
    result = attach_wg_bewerbermappe(page)
    assert result.state == "attached"
    assert result.verified


def test_listing_text_includes_hidden_wg_description_tabs(page) -> None:
    page.set_content(
        """
        <main>
          <p>WG-Zimmer in Münster mit 490 Euro Gesamtmiete und vielen Details.</p>
          <div id="freitext_0">Das Zimmer ist hell und möbliert.</div>
          <div id="freitext_1" class="display-none">Die Lage ist zentral.</div>
          <div id="freitext_2" class="display-none">Wir kochen zusammen.</div>
          <div id="freitext_3" class="display-none">Beginne deine Nachricht mit Moin.</div>
        </main>
        """
    )
    text = _extract_wg_listing_text(page)
    assert "Die Lage ist zentral." in text
    assert "Wir kochen zusammen." in text
    assert "Beginne deine Nachricht mit Moin." in text


def test_listing_text_excludes_unrelated_similar_ads(page) -> None:
    page.set_content(
        """
        <main>
          <h1>Gemischte 3er WG</h1>
          <p>Gesucht wird: Geschlecht egal zwischen 25 und 35 Jahren.</p>
          <div id="freitext_0">Wir suchen eine neue Person für unsere entspannte WG.</div>
          <div id="similar_ads_swiper">
            <a class="similar_ads_title">Gemütliche Frauen-WG in bester Uni-Lage</a>
          </div>
        </main>
        """
    )
    text = _extract_wg_listing_text(page)
    assert "Gemischte 3er WG" in text
    assert "Frauen-WG" not in text


def _wg_outcome(url: str, *, message_source: str = "cloud_ai") -> AnalysisOutcome:
    return AnalysisOutcome(
        listing=viable_listing(url=url),
        facts=ListingFacts(
            advertiser_type="wg",
            housing_type="wg_room",
            warm_rent_eur=490,
            scam_risk="low",
            confidence=0.9,
        ),
        rule_decision=RuleDecision(decision="APPLY"),
        status="drafted",
        message=MessageDraft(
            body=GERMAN_WG_TEMPLATE,
            language="de",
            address_register="du",
        ),
        attachment=AttachmentDecision(
            allowed=True,
            should_attach=True,
            source="wg_account",
            requires_browser_verification=True,
        ),
        validation=ValidationResult(auto_send_allowed=True),
        message_source=message_source,
    )


def _write_wg_fixture(
    path: Path, *, premium_success: bool = True, dossier_success: bool = True
) -> None:
    premium = (
        _premium_entry("Unknown", onclick="premiumMenu.style.display='block'")
        + """
          <div id="premiumMenu" role="dialog" style="display:none">
            <button data-action="message-priority"
              onclick="this.setAttribute('aria-pressed','true')">Activate</button>
          </div>
        """
        if premium_success
        else _premium_entry("Unknown", href="/wgg-plus-shop")
    )
    dossier_click = "this.dataset.state='attached'" if dossier_success else ""
    path.write_text(
        f"""<!doctype html><html><body>
        {premium}
        <a href="/nachricht-senden/room.1234567.html"
          onclick="event.preventDefault(); composer.style.display='block'">Open</a>
        <form id="composer" style="display:none" onsubmit="event.preventDefault()">
          <textarea name="message"></textarea>
          <button type="button" data-testid="bewerbermappe-option"
            onclick="{dossier_click}">Meine Bewerbermappe</button>
          <button type="submit"
            onclick="sent.style.display='block'">Send</button>
        </form>
        <div id="sent" data-message-state="sent" style="display:none">Sent</div>
        </body></html>""",
        encoding="utf-8",
    )


def test_strict_mode_blocks_normal_fallback_when_priority_fails(settings, tmp_path) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html, premium_success=False)
    result = prepare_platform_contact(
        _wg_outcome(html.as_uri()),
        replace(settings, browser_headless=True, wg_premium_strict=True),
        send_permitted=False,
    )
    assert result.status == "premium_boost_failed"
    assert result.screenshot_path


def test_non_strict_mode_warns_but_still_reaches_dry_run_ready(settings, tmp_path) -> None:
    """WG+ priority is a bonus, not a hard requirement: with WG_PREMIUM_STRICT=false
    (the default), an unverifiable boost must not cost a good apartment."""
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html, premium_success=False)
    result = prepare_platform_contact(
        _wg_outcome(html.as_uri()),
        replace(settings, browser_headless=True, wg_premium_strict=False),
        send_permitted=False,
    )
    assert result.status == "dry_run_ready", result.detail
    assert "premium_boost_unverified" in result.detail
    assert "Bewerbermappe attached" in result.detail


def test_non_strict_mode_allows_actual_send_when_priority_unverified(settings, tmp_path) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html, premium_success=False)
    claims = 0

    def claim() -> bool:
        nonlocal claims
        claims += 1
        return True

    result = prepare_platform_contact(
        _wg_outcome(html.as_uri()),
        replace(
            settings,
            dry_run=False,
            auto_send=True,
            browser_headless=True,
            wg_premium_strict=False,
        ),
        send_permitted=True,
        claim_actual=claim,
    )
    assert result.status == "sent", result.detail
    assert claims == 1
    assert "premium_boost_unverified" in result.detail


def test_strict_mode_still_blocks_actual_send_when_priority_unverified(settings, tmp_path) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html, premium_success=False)
    claims = 0

    def claim() -> bool:
        nonlocal claims
        claims += 1
        return True

    result = prepare_platform_contact(
        _wg_outcome(html.as_uri()),
        replace(
            settings, dry_run=False, auto_send=True, browser_headless=True, wg_premium_strict=True
        ),
        send_permitted=True,
        claim_actual=claim,
    )
    assert result.status == "premium_boost_failed"
    assert claims == 0


def test_universal_fallback_faces_all_gates_and_dry_run_never_sends(settings, tmp_path) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html)
    result = prepare_platform_contact(
        _wg_outcome(html.as_uri(), message_source="universal_fallback"),
        replace(settings, dry_run=True, auto_send=False, browser_headless=True),
        send_permitted=False,
    )
    assert result.status == "dry_run_ready", result.detail
    assert "priority verified" in result.detail
    assert "Bewerbermappe attached" in result.detail
    assert "stopped before Send" in result.detail
    assert result.screenshot_path and Path(result.screenshot_path).is_file()


def test_auto_send_requires_every_gate(settings, tmp_path) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html)
    claims = 0

    def claim() -> bool:
        nonlocal claims
        claims += 1
        return True

    result = prepare_platform_contact(
        _wg_outcome(html.as_uri()),
        replace(settings, dry_run=False, auto_send=True, browser_headless=True),
        send_permitted=True,
        claim_actual=claim,
    )
    assert result.status == "sent", result.detail
    assert claims == 1


def test_auto_send_blocked_when_bewerbermappe_cannot_be_verified(settings, tmp_path) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html, dossier_success=False)
    claims = 0

    def claim() -> bool:
        nonlocal claims
        claims += 1
        return True

    result = prepare_platform_contact(
        _wg_outcome(html.as_uri()),
        replace(settings, dry_run=False, auto_send=True, browser_headless=True),
        send_permitted=True,
        claim_actual=claim,
    )
    assert result.status == "bewerbermappe_attachment_failed"


# ---- security/privacy/policy popup handling (task H) ------------------------------


def _cookie_consent_modal(accept_text: str = "Akzeptieren") -> str:
    return f"""
    <div id="onetrust-consent-sdk">
      <p>Wir verwenden Cookies für Sicherheit und Datenschutz.</p>
      <button onclick="this.closest('#onetrust-consent-sdk').remove();
        window.__acceptClicked = true">{accept_text}</button>
    </div>
    """


def test_policy_modal_present_is_detected_and_accepted(page) -> None:
    page.set_content(_cookie_consent_modal())
    handled = dismiss_or_accept_policy_modal_if_present(page)
    assert handled is True
    assert page.evaluate("() => window.__acceptClicked === true")


def test_policy_modal_absent_is_a_noop(page) -> None:
    page.set_content("<main>Nothing special here.</main>")
    handled = dismiss_or_accept_policy_modal_if_present(page)
    assert handled is False


def test_policy_modal_never_clicks_the_unrelated_premium_menu(page) -> None:
    """Regression guard: WG-Gesucht's own Premium priority popup uses the same
    role="dialog"/aria-modal accessible markup as a real consent modal would. The
    policy-popup detector must never probe buttons inside that unrelated, already-
    working menu."""
    page.set_content(
        _premium_entry("Unknown", onclick="menu.style.display='block'")
        + """
        <div id="menu" role="dialog" style="display:block">
          <button data-action="message-priority"
            onclick="window.__priorityClicked = true">Priority</button>
        </div>
        """
    )
    handled = dismiss_or_accept_policy_modal_if_present(page)
    assert handled is False
    assert page.evaluate("() => window.__priorityClicked || false") is False


def test_premium_click_popup_is_accepted_then_priority_still_verifies(page) -> None:
    page.set_content(
        _premium_entry("Unknown", onclick="popup.style.display='block'")
        + f"""
        {_cookie_consent_modal()}
        <div id="popup" role="dialog" style="display:none">
          <button data-action="message-priority"
            onclick="this.setAttribute('aria-pressed','true')">Activate</button>
        </div>
        """
    )
    result = activate_wg_plus_priority(page)
    assert result.state == "premium_priority_verified"
    assert result.verified
    assert page.evaluate("() => window.__acceptClicked === true")


def test_message_open_popup_is_accepted_then_composer_still_opens(settings, tmp_path) -> None:
    html = tmp_path / "room.1234567.html"
    html.write_text(
        f"""<!doctype html><html><body>
        {_cookie_consent_modal()}
        {_premium_entry("Unknown", href="/wgg-plus-shop")}
        <a href="/nachricht-senden/room.1234567.html"
          onclick="event.preventDefault(); composer.style.display='block'">Open</a>
        <form id="composer" style="display:none" onsubmit="event.preventDefault()">
          <textarea name="message"></textarea>
          <button type="button" data-testid="bewerbermappe-option"
            onclick="this.dataset.state='attached'">Meine Bewerbermappe</button>
          <button type="submit" onclick="sent.style.display='block'">Send</button>
        </form>
        <div id="sent" data-message-state="sent" style="display:none">Sent</div>
        </body></html>""",
        encoding="utf-8",
    )
    result = prepare_platform_contact(
        _wg_outcome(html.as_uri()),
        replace(
            settings,
            dry_run=True,
            auto_send=False,
            browser_headless=True,
            wg_premium_strict=False,
        ),
        send_permitted=False,
    )
    assert result.status == "dry_run_ready", result.detail
