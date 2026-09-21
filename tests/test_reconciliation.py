from __future__ import annotations

from dataclasses import replace

import pytest
from playwright.sync_api import sync_playwright

from app.browser import reconcile_wg_listing
from app.cli import _reconcile
from app.database import Database
from tests.conftest import viable_listing

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


# ---- browser-level reconciliation (read-only) ------------------------------------


def test_reconcile_finds_message_and_reports_sent(page) -> None:
    page.set_content(
        f"""
        <a class='wgg-btn-primary' href='/nachricht.html?nachrichten-id=1'>conversation</a>
        <div class="messages">{BODY}</div>
        """
    )
    outcome = reconcile_wg_listing(page, BODY, "14096490")
    assert outcome.result == "sent"


def test_reconcile_finds_new_action_and_reports_not_sent(page) -> None:
    page.set_content("<a href='/nachricht-senden/room.14096490.html'>Contact</a>")
    outcome = reconcile_wg_listing(page, BODY, "14096490")
    assert outcome.result == "not_sent"


def test_reconcile_ambiguous_page_stays_unknown(page) -> None:
    page.set_content("<main>Neither a new-message nor a conversation action here.</main>")
    outcome = reconcile_wg_listing(page, BODY, "14096490")
    assert outcome.result == "send_state_unknown"


def test_reconcile_never_clicks_anything(page) -> None:
    """Reconciliation must be strictly read-only, even if a Send-like button exists."""
    page.set_content(
        """
        <a href='/nachricht-senden/room.14096490.html'>Contact</a>
        <button onclick="window.__clicked = (window.__clicked || 0) + 1">Send</button>
        """
    )
    reconcile_wg_listing(page, BODY, "14096490")
    assert page.evaluate("() => window.__clicked || 0") == 0


def test_reconcile_follows_conversation_link_to_find_message(tmp_path, page) -> None:
    """The real WG-Gesucht listing page only links to the conversation; the actual
    sent message text lives on that separate page. A conversation link alone is not
    proof of a send (a dry-run visit can expose it too), so reconciliation must follow
    the link (via navigation, never a click) and look for the message there."""
    conversation_dir = tmp_path / "nachrichten"
    conversation_dir.mkdir()
    conversation_html = conversation_dir / "conversation.html"
    conversation_html.write_text(
        f"<!doctype html><html><body><div class='messages'>{BODY}</div></body></html>",
        encoding="utf-8",
    )
    listing_html = tmp_path / "listing.html"
    listing_html.write_text(
        """<!doctype html><html><body>
        <a class="wgg-btn-primary" href="./nachrichten/conversation.html">conversation</a>
        </body></html>""",
        encoding="utf-8",
    )
    page.add_init_script("window.__clicked = 0")
    page.goto(listing_html.as_uri())

    outcome = reconcile_wg_listing(page, BODY, "14096490")

    assert outcome.result == "sent"
    assert "followed_conversation_link" in outcome.signals
    assert page.evaluate("() => window.__clicked || 0") == 0


def test_reconcile_conversation_link_without_message_stays_unknown(tmp_path, page) -> None:
    """A conversation exists, but the message text cannot be found on either page:
    this must stay ambiguous rather than being guessed as sent or not_sent."""
    conversation_dir = tmp_path / "nachrichten"
    conversation_dir.mkdir()
    conversation_html = conversation_dir / "conversation.html"
    conversation_html.write_text(
        "<!doctype html><html><body><div>Some unrelated conversation content.</div></body></html>",
        encoding="utf-8",
    )
    listing_html = tmp_path / "listing.html"
    listing_html.write_text(
        """<!doctype html><html><body>
        <a class="wgg-btn-primary" href="./nachrichten/conversation.html">conversation</a>
        </body></html>""",
        encoding="utf-8",
    )
    page.goto(listing_html.as_uri())

    outcome = reconcile_wg_listing(page, BODY, "14096490")

    assert outcome.result == "send_state_unknown"
    assert "followed_conversation_link" in outcome.signals


# ---- database-level reconciliation plumbing --------------------------------------


def test_reconcile_contact_updates_existing_actual_row(settings) -> None:
    database = Database(settings.database_path)
    listing = viable_listing(platform="wg_gesucht", listing_id="14096490")
    listing_db_id, _ = database.discover(listing)
    assert database.claim_actual_contact(listing_db_id, "platform")
    database.record_send_clicked(
        listing_db_id,
        "fp123",
        BODY,
        "premium_priority_verified",
        "attached",
        "https://example.test",
    )

    database.reconcile_contact(listing_db_id, "sent", "found in conversation")

    attempt = database.get_actual_contact_attempt(listing_db_id)
    assert attempt is not None
    assert attempt["status"] == "sent"
    assert attempt["message_fingerprint"] == "fp123"
    assert attempt["confirmed_at"]
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status FROM listings WHERE id=?", (listing_db_id,)
        ).fetchone()
    assert row["status"] == "sent"


def test_reconcile_contact_creates_row_when_none_existed(settings) -> None:
    database = Database(settings.database_path)
    listing = viable_listing(platform="wg_gesucht", listing_id="14096490")
    listing_db_id, _ = database.discover(listing)

    database.reconcile_contact(listing_db_id, "not_sent", "no application found")

    attempt = database.get_actual_contact_attempt(listing_db_id)
    assert attempt is not None
    assert attempt["status"] == "not_sent"


def test_find_listing_by_url_or_id(settings) -> None:
    database = Database(settings.database_path)
    listing = viable_listing(
        platform="wg_gesucht",
        listing_id="14096490",
        url="https://www.wg-gesucht.de/wg-zimmer.14096490.html",
    )
    database.discover(listing)

    by_url = database.find_listing_by_url_or_id(listing.url)
    assert by_url is not None
    assert by_url["listing_id"] == "14096490"

    by_id = database.find_listing_by_url_or_id("https://unrelated.example/", "14096490")
    assert by_id is not None
    assert by_id["listing_id"] == "14096490"

    assert database.find_listing_by_url_or_id("https://unrelated.example/") is None


# ---- CLI-level reconciliation end to end ------------------------------------------


def test_cli_reconcile_marks_listing_sent_from_live_page(settings, tmp_path) -> None:
    html = tmp_path / "room.14096490.html"
    html.write_text(
        f"""<!doctype html><html><body>
        <a class='wgg-btn-primary' href='/nachricht.html?nachrichten-id=1'>conversation</a>
        <div class="messages">{BODY}</div>
        </body></html>""",
        encoding="utf-8",
    )
    database = Database(settings.database_path)
    listing = viable_listing(platform="wg_gesucht", listing_id="14096490", url=html.as_uri())
    listing_db_id, _ = database.discover(listing)
    with database.connect() as connection:
        connection.execute("UPDATE listings SET message_body=? WHERE id=?", (BODY, listing_db_id))

    code = _reconcile(html.as_uri(), replace(settings, browser_headless=True), database)

    assert code == 0
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status FROM listings WHERE id=?", (listing_db_id,)
        ).fetchone()
    assert row["status"] == "sent"


def test_cli_reconcile_reports_not_sent_from_live_page(settings, tmp_path) -> None:
    html = tmp_path / "room.14096490.html"
    html.write_text(
        """<!doctype html><html><body>
        <a href='/nachricht-senden/room.14096490.html'>Contact</a>
        </body></html>""",
        encoding="utf-8",
    )
    database = Database(settings.database_path)
    listing = viable_listing(platform="wg_gesucht", listing_id="14096490", url=html.as_uri())
    listing_db_id, _ = database.discover(listing)
    with database.connect() as connection:
        connection.execute("UPDATE listings SET message_body=? WHERE id=?", (BODY, listing_db_id))

    code = _reconcile(html.as_uri(), replace(settings, browser_headless=True), database)

    assert code == 0
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status FROM listings WHERE id=?", (listing_db_id,)
        ).fetchone()
    assert row["status"] == "not_sent"


def test_cli_reconcile_without_prior_record_refuses(settings) -> None:
    database = Database(settings.database_path)
    code = _reconcile(
        "https://www.wg-gesucht.de/wg-zimmer.99999999.html",
        replace(settings, browser_headless=True),
        database,
    )
    assert code == 2
