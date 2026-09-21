from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

import app.browser as browser_module
from app.browser import reconcile_wg_listing, verify_message_sent

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


def _write_listing_and_conversation(tmp_path: Path, conversation_body: str) -> Path:
    conversation_dir = tmp_path / "nachrichten"
    conversation_dir.mkdir()
    (conversation_dir / "conversation.html").write_text(
        f"<!doctype html><html><body><div class='messages'>{conversation_body}</div></body></html>",
        encoding="utf-8",
    )
    listing_html = tmp_path / "listing.html"
    listing_html.write_text(
        """<!doctype html><html><body>
        <a class="wgg-btn-primary" href="./nachrichten/conversation.html">conversation</a>
        <button id="send"
          onclick="window.__sendClicks = (window.__sendClicks || 0) + 1">Send</button>
        </body></html>""",
        encoding="utf-8",
    )
    return listing_html


def test_verify_message_sent_follows_conversation_link_like_reconcile(tmp_path, page) -> None:
    """The post-send verification path must navigate to WG-Gesucht Messages and check
    the actual conversation content, exactly like reconcile does -- the listing page
    alone (still showing a "view conversation" link) is not itself proof of a send."""
    listing_html = _write_listing_and_conversation(tmp_path, BODY)
    page.goto(listing_html.as_uri())

    evidence = verify_message_sent(
        page, BODY, None, "14096490", poll_seconds=1, poll_interval_ms=200
    )

    assert evidence.confirmed
    assert "followed_conversation_link" in evidence.signals


def test_verify_message_sent_never_clicks_anything(tmp_path, page) -> None:
    """Verification must never click Send again, however long it polls."""
    listing_html = _write_listing_and_conversation(tmp_path, "unrelated content only")
    page.goto(listing_html.as_uri())

    verify_message_sent(page, BODY, None, "14096490", poll_seconds=1, poll_interval_ms=200)

    assert page.evaluate("() => window.__sendClicks || 0") == 0


def test_verify_and_reconcile_use_the_same_shared_matching_function(monkeypatch, page) -> None:
    """Both the immediately-after-Send verification path and the independent,
    later reconcile path must route through the exact same conversation-matching
    function -- there must not be two separate implementations."""
    page.set_content("<main>Nothing relevant is shown here.</main>")
    calls = []
    original = browser_module._sent_signals_with_conversation_followup

    def spy(*args, **kwargs):
        calls.append("called")
        return original(*args, **kwargs)

    monkeypatch.setattr(browser_module, "_sent_signals_with_conversation_followup", spy)

    verify_message_sent(page, BODY, None, "14096490", poll_seconds=0.1, poll_interval_ms=50)
    reconcile_wg_listing(page, BODY, "14096490")

    assert len(calls) >= 2


def test_conversation_link_without_message_stays_unknown_not_confirmed(tmp_path, page) -> None:
    listing_html = _write_listing_and_conversation(tmp_path, "some unrelated conversation text")
    page.goto(listing_html.as_uri())

    evidence = verify_message_sent(
        page, BODY, None, "14096490", poll_seconds=1, poll_interval_ms=200
    )

    assert not evidence.confirmed
    assert page.evaluate("() => window.__sendClicks || 0") == 0


def test_bounded_retry_eventually_confirms_a_delayed_conversation(tmp_path, page) -> None:
    """The message may not appear instantly in Messages (a real conversation page can
    render its content asynchronously); a small bounded retry loop must still confirm
    it once it appears, without ever clicking Send again."""
    conversation_dir = tmp_path / "nachrichten"
    conversation_dir.mkdir()
    (conversation_dir / "conversation.html").write_text(
        f"""<!doctype html><html><body>
        <div class="messages" id="messages">not yet</div>
        <script>
          setTimeout(() => {{
            document.getElementById('messages').textContent = {BODY!r};
          }}, 700);
        </script>
        </body></html>""",
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

    evidence = verify_message_sent(
        page, BODY, None, "14096490", poll_seconds=3, poll_interval_ms=300
    )

    assert evidence.confirmed
