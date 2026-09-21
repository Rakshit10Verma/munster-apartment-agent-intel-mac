from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from app.browser import (
    _collect_sent_signals,
    _is_send_confirmed,
    message_fingerprint,
    verify_message_sent,
)
from app.contact import contact_listing
from app.database import Database
from tests.test_browser_dry_run import _premium_entry, _wg_outcome

BODY = (
    "Hallo zusammen, ich interessiere mich sehr für euer Zimmer und freue mich schon "
    "sehr auf eine Antwort von euch, vielen Dank im Voraus."
)


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def _write_wg_fixture_ambiguous_send(path: Path) -> None:
    """A composer whose Send button gives NO observable feedback at all: no
    structural marker, no navigation, no clearing. Used to exercise the ambiguous
    `send_state_unknown` path deterministically."""
    premium = (
        _premium_entry("Unknown", onclick="premiumMenu.style.display='block'")
        + """
          <div id="premiumMenu" role="dialog" style="display:none">
            <button data-action="message-priority"
              onclick="this.setAttribute('aria-pressed','true')">Activate</button>
          </div>
        """
    )
    path.write_text(
        f"""<!doctype html><html><body>
        {premium}
        <a href="/nachricht-senden/room.1234567.html"
          onclick="event.preventDefault(); composer.style.display='block'">Open</a>
        <form id="composer" style="display:none" onsubmit="event.preventDefault()">
          <textarea name="message"></textarea>
          <button type="button" data-testid="bewerbermappe-option"
            onclick="this.dataset.state='attached'">Meine Bewerbermappe</button>
          <button type="submit">Send</button>
        </form>
        </body></html>""",
        encoding="utf-8",
    )


# ---- pure logic: _is_send_confirmed --------------------------------------------


def test_structural_marker_alone_confirms_send() -> None:
    assert _is_send_confirmed(["structural_sent_marker"])


def test_fingerprint_plus_conversation_route_confirms_send() -> None:
    assert _is_send_confirmed(
        ["message_fingerprint_start_matched", "listing_state_is_already_contacted"]
    )


def test_fingerprint_plus_empty_composer_confirms_send() -> None:
    assert _is_send_confirmed(["message_fingerprint_end_matched", "composer_is_empty"])


def test_fingerprint_alone_does_not_confirm_send() -> None:
    assert not _is_send_confirmed(["message_fingerprint_start_matched"])


def test_no_signals_does_not_confirm_send() -> None:
    assert not _is_send_confirmed([])


def test_bewerbermappe_signal_alone_does_not_confirm_send() -> None:
    """Bewerbermappe being attached proves the document gate passed, but is never by
    itself proof that the message was actually sent."""
    assert not _is_send_confirmed(["bewerbermappe_shown_attached"])


# ---- fingerprint stability -------------------------------------------------------


def test_fingerprint_is_stable_across_whitespace_differences() -> None:
    assert message_fingerprint("Hallo   Welt\n\ndas ist ein Test.") == message_fingerprint(
        "Hallo Welt das ist ein Test."
    )


def test_fingerprint_differs_for_different_content() -> None:
    assert message_fingerprint("Hallo Welt") != message_fingerprint("Hallo Mond")


# ---- DOM signal extraction --------------------------------------------------------


def test_message_found_in_conversation_confirms_sent(page) -> None:
    page.set_content(
        f"""
        <a class='wgg-btn-primary' href='/nachricht.html?nachrichten-id=1'>conversation</a>
        <div class="messages">{BODY}</div>
        """
    )
    signals = _collect_sent_signals(page, BODY, None, "1234567")
    assert "message_fingerprint_start_matched" in signals
    assert "listing_state_is_already_contacted" in signals
    assert _is_send_confirmed(signals)


def test_whitespace_and_formatting_differences_still_match(page) -> None:
    reformatted = BODY.replace(" ", "\n")
    page.set_content(
        f"""
        <a class='wgg-btn-primary' href='/nachricht.html?nachrichten-id=1'>conversation</a>
        <div class="messages">{reformatted}</div>
        """
    )
    signals = _collect_sent_signals(page, BODY, None, "1234567")
    assert any(signal.startswith("message_fingerprint") for signal in signals)
    assert _is_send_confirmed(signals)


def test_message_and_bewerbermappe_together_confirm_sent(page) -> None:
    page.set_content(
        f"""
        <a class='wgg-btn-primary' href='/nachricht.html?nachrichten-id=1'>conversation</a>
        <div class="messages">{BODY}</div>
        <div class="pre_attached_application_package">
          Meine Bewerbermappe <span id="detach_application_package"></span>
        </div>
        """
    )
    signals = _collect_sent_signals(page, BODY, None, "1234567")
    assert "bewerbermappe_shown_attached" in signals
    assert _is_send_confirmed(signals)


def test_empty_composer_plus_conversation_message_confirms_sent(page) -> None:
    page.set_content(
        f"""
        <a class='wgg-btn-primary' href='/nachricht.html?nachrichten-id=1'>conversation</a>
        <div class="messages">{BODY}</div>
        <textarea id="message_input"></textarea>
        """
    )
    message_box = page.locator("#message_input")
    signals = _collect_sent_signals(page, BODY, message_box, "1234567")
    assert "composer_is_empty" in signals
    assert _is_send_confirmed(signals)


def test_no_evidence_after_send_is_not_confirmed(page) -> None:
    page.set_content("<main>Nothing relevant is shown here.</main>")
    evidence = verify_message_sent(
        page, BODY, None, "1234567", poll_seconds=1, poll_interval_ms=200
    )
    assert not evidence.confirmed
    assert evidence.signals == []


# ---- full pipeline: ambiguous send must not be treated as a hard failure --------


def test_send_clicked_without_confirmation_is_unknown_not_failed(settings, tmp_path) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture_ambiguous_send(html)
    outcome = _wg_outcome(html.as_uri())
    database = Database(settings.database_path)
    outcome.database_id, _ = database.discover(outcome.listing)
    outcome.database_id = database.save_outcome(outcome)

    result = contact_listing(
        outcome,
        replace(settings, dry_run=False, auto_send=True, browser_timeout_seconds=1),
        database,
        "auto",
    )

    assert result.status == "send_state_unknown"
    assert "reconcile_send.sh" in result.detail
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status, message_fingerprint, send_clicked_at, premium_state, "
            "attachment_state FROM contact_attempts WHERE listing_db_id=?",
            (outcome.database_id,),
        ).fetchone()
    assert row["status"] == "send_state_unknown"
    # persisted immediately after the click, independent of verification outcome
    assert row["message_fingerprint"]
    assert row["send_clicked_at"]
    assert row["premium_state"]
    assert row["attachment_state"]


def test_send_state_unknown_listing_cannot_auto_retry(settings, tmp_path) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture_ambiguous_send(html)
    outcome = _wg_outcome(html.as_uri())
    database = Database(settings.database_path)
    outcome.database_id, _ = database.discover(outcome.listing)
    outcome.database_id = database.save_outcome(outcome)
    actual_settings = replace(settings, dry_run=False, auto_send=True, browser_timeout_seconds=1)

    first = contact_listing(outcome, actual_settings, database, "auto")
    assert first.status == "send_state_unknown"

    second = contact_listing(outcome, actual_settings, database, "auto")
    assert second.status == "already_contacted"
    assert "duplicate" in second.detail


def test_duplicate_send_prevention_after_process_crash(settings, tmp_path) -> None:
    """Simulates a prior process that claimed the actual-send slot and crashed before
    doing anything else (e.g. before the browser even opened). A fresh attempt must
    still be blocked rather than proceeding to click Send again."""
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture_ambiguous_send(html)
    outcome = _wg_outcome(html.as_uri())
    database = Database(settings.database_path)
    outcome.database_id, _ = database.discover(outcome.listing)
    outcome.database_id = database.save_outcome(outcome)

    assert database.claim_actual_contact(outcome.database_id, "platform")

    result = contact_listing(
        outcome, replace(settings, dry_run=False, auto_send=True), database, "auto"
    )
    assert result.status == "already_contacted"
    assert "duplicate" in result.detail
