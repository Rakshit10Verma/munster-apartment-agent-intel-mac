from __future__ import annotations

from dataclasses import replace

import pytest

from app.database import Database
from app.telegram_notify import (
    check_watcher_health,
    keyboard_for_status,
    notify_listing_if_changed,
    record_discovery_outcome,
    render_listing_card,
    review_keyboard,
    send_state_unknown_keyboard,
)
from tests.test_review import _save_review_required


def _labels(keyboard: dict) -> list[str]:
    return [button["text"] for row in keyboard["inline_keyboard"] for button in row]


def _callback_data(keyboard: dict) -> list[str]:
    return [
        button["callback_data"]
        for row in keyboard["inline_keyboard"]
        for button in row
        if "callback_data" in button
    ]


@pytest.fixture
def tg_settings(settings):
    return replace(
        settings,
        telegram_enabled=True,
        telegram_bot_token="test-token",
        telegram_allowed_user_id=111,
        telegram_chat_id="222",
    )


def test_disabled_telegram_never_calls_the_api(settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)

    def fail(*_a, **_k):
        raise AssertionError("must never call the Telegram API when disabled")

    monkeypatch.setattr("app.telegram_notify.httpx.post", fail)

    notify_listing_if_changed(database, settings, listing_db_id)  # settings.telegram_enabled=False

    assert database.get_telegram_notification(listing_db_id) is None


def test_review_required_creates_one_notification(settings, tg_settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    calls = []
    monkeypatch.setattr(
        "app.telegram_notify.send_message", lambda *a, **k: (calls.append(a), "1")[1]
    )

    notify_listing_if_changed(database, tg_settings, listing_db_id)

    assert len(calls) == 1
    notification = database.get_telegram_notification(listing_db_id)
    assert notification is not None
    assert notification["status"] == "review_required"


def test_repeated_cycles_do_not_duplicate_notification(settings, tg_settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    calls = []
    monkeypatch.setattr(
        "app.telegram_notify.send_message", lambda *a, **k: (calls.append(a), "1")[1]
    )

    for _ in range(50):
        notify_listing_if_changed(database, tg_settings, listing_db_id)

    assert len(calls) == 1


def test_state_change_sends_a_new_notification(settings, tg_settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    calls = []
    monkeypatch.setattr(
        "app.telegram_notify.send_message", lambda *a, **k: (calls.append(a), "1")[1]
    )

    notify_listing_if_changed(database, tg_settings, listing_db_id)
    with database.connect() as connection:
        connection.execute("UPDATE listings SET status='sent' WHERE id=?", (listing_db_id,))
    notify_listing_if_changed(database, tg_settings, listing_db_id)

    assert len(calls) == 2
    assert database.get_telegram_notification(listing_db_id)["status"] == "sent"


def test_filtered_skip_never_notifies(settings, tg_settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database, status="filtered_skip")
    calls = []
    monkeypatch.setattr(
        "app.telegram_notify.send_message", lambda *a, **k: (calls.append(a), "1")[1]
    )

    notify_listing_if_changed(database, tg_settings, listing_db_id)

    assert not calls
    assert database.get_telegram_notification(listing_db_id) is None


def test_open_listing_points_to_stored_url() -> None:
    keyboard = review_keyboard(7, "https://www.wg-gesucht.de/wg-zimmer.777.html")
    open_button = keyboard["inline_keyboard"][0][0]
    assert open_button["url"] == "https://www.wg-gesucht.de/wg-zimmer.777.html"
    assert "callback_data" not in open_button  # a URL button never triggers our own code


def test_notification_never_dumps_full_debug_trace(settings, tg_settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(
        database, unresolved_required_facts=["Immatrikulationsbescheinigung"]
    )
    from app.review import load_listing_record
    from app.telegram_notify import format_listing_notification

    row = database.get_listing(listing_db_id)
    record = load_listing_record(row)

    text = format_listing_notification(row, record)

    assert "needs human review" not in text  # the raw validation.errors debug string
    assert "Immatrikulationsbescheinigung" not in text  # raw debug detail is kept out of Telegram
    assert record.review_reason_human in text  # the concise human-readable reason is kept


def test_send_state_unknown_has_no_send_button() -> None:
    keyboard = send_state_unknown_keyboard(3, "https://example.test")
    labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
    assert not any("Send" in label and "Confirm" not in label for label in labels)
    assert any("Reconcile" in label for label in labels)


# ---- state-aware button rendering (regression for DB 71: already_contacted must not
# offer Send/Reject, and no impossible action may survive a state change) -----------


def test_already_contacted_has_no_send_button() -> None:
    keyboard = keyboard_for_status("already_contacted", 71, "https://example.test")
    assert "send_request:71" not in _callback_data(keyboard)
    assert not any(data.startswith("reject:") for data in _callback_data(keyboard))
    assert not any(data.startswith("reconcile:") for data in _callback_data(keyboard))
    assert _labels(keyboard) == ["🔗 Open Listing"]


def test_sent_has_no_send_button() -> None:
    keyboard = keyboard_for_status("sent", 71, "https://example.test")
    assert "send_request:71" not in _callback_data(keyboard)
    assert _labels(keyboard) == ["🔗 Open Listing"]


def test_send_state_unknown_only_exposes_reconcile_and_open() -> None:
    keyboard = keyboard_for_status("send_state_unknown", 71, "https://example.test")
    assert _callback_data(keyboard) == ["reconcile:71", "dismiss:71"]
    assert not any(data.startswith("send") for data in _callback_data(keyboard))


@pytest.mark.parametrize(
    "status", ["manually_rejected", "filtered_skip", "dry_run_ready", "not_sent"]
)
def test_settled_or_unapprovable_statuses_never_show_send(status: str) -> None:
    """These are not in APPROVABLE_STATUSES (review.py's own approval gate), so a
    Send button would just be refused if tapped -- Telegram must not show one."""
    keyboard = keyboard_for_status(status, 71, "https://example.test")
    assert not any(data.startswith("send") for data in _callback_data(keyboard))


@pytest.mark.parametrize(
    "status", ["review_required", "drafted", "ai_failed", "premium_boost_failed"]
)
def test_approvable_statuses_still_show_send_and_reject(status: str) -> None:
    keyboard = keyboard_for_status(status, 71, "https://example.test")
    assert "send_request:71" in _callback_data(keyboard)
    assert "reject:71" in _callback_data(keyboard)


def test_already_contacted_headline_is_human_readable_not_raw_dom_reason(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    with database.connect() as connection:
        connection.execute(
            "UPDATE listings SET status='already_contacted' WHERE id=?", (listing_db_id,)
        )

    rendered = render_listing_card(database, listing_db_id)

    assert rendered is not None
    text, keyboard, status = rendered
    assert status == "already_contacted"
    assert "existing conversation action detected from DOM" not in text
    assert "already contacted" in text.casefold()
    assert "no duplicate" in text.casefold()
    assert _labels(keyboard) == ["🔗 Open Listing"]


def test_render_listing_card_refreshes_buttons_after_state_change(settings) -> None:
    """The exact scenario reported: a listing starts approvable (Send/Reject shown),
    then transitions to already_contacted -- re-rendering must drop those buttons
    immediately, based on nothing but the current DB row."""
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)

    before = render_listing_card(database, listing_db_id)
    assert before is not None
    _text, before_keyboard, before_status = before
    assert before_status == "review_required"
    assert "send_request" in _callback_data(before_keyboard)[0]

    with database.connect() as connection:
        connection.execute(
            "UPDATE listings SET status='already_contacted' WHERE id=?", (listing_db_id,)
        )

    after = render_listing_card(database, listing_db_id)
    assert after is not None
    _text, after_keyboard, after_status = after
    assert after_status == "already_contacted"
    assert not _callback_data(after_keyboard)
    assert _labels(after_keyboard) == ["🔗 Open Listing"]


def test_render_listing_card_returns_none_for_missing_listing(settings) -> None:
    database = Database(settings.database_path)
    assert render_listing_card(database, 999999) is None


# ---- outage isolation / dedup for daemon-level events -------------------------------


def test_discovery_failure_notifies_after_threshold_not_immediately(
    settings, tg_settings, monkeypatch
) -> None:
    database = Database(settings.database_path)
    calls = []
    monkeypatch.setattr(
        "app.telegram_notify.send_message", lambda *a, **k: (calls.append(a), "1")[1]
    )

    record_discovery_outcome(database, tg_settings, "wg_search_watcher", failed=True)
    record_discovery_outcome(database, tg_settings, "wg_search_watcher", failed=True)
    assert not calls
    record_discovery_outcome(database, tg_settings, "wg_search_watcher", failed=True)
    assert len(calls) == 1


def test_discovery_success_resets_failure_count(settings, tg_settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    calls = []
    monkeypatch.setattr(
        "app.telegram_notify.send_message", lambda *a, **k: (calls.append(a), "1")[1]
    )

    record_discovery_outcome(database, tg_settings, "wg_search_watcher", failed=True)
    record_discovery_outcome(database, tg_settings, "wg_search_watcher", failed=True)
    record_discovery_outcome(database, tg_settings, "wg_search_watcher", failed=False)
    record_discovery_outcome(database, tg_settings, "wg_search_watcher", failed=True)
    record_discovery_outcome(database, tg_settings, "wg_search_watcher", failed=True)

    assert not calls  # never reached the threshold again after the reset


def test_check_watcher_health_never_raises_without_wg_watch_configured(
    settings, tg_settings
) -> None:
    database = Database(settings.database_path)
    check_watcher_health(database, tg_settings)  # must not raise even with no searches configured
