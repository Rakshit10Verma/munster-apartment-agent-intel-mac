from __future__ import annotations

from dataclasses import replace

import pytest

from app import telegram_bot
from app.database import Database
from app.schemas import ContactResult
from tests.test_review import BODY, _save_review_required

AUTHORIZED_USER = 111
AUTHORIZED_CHAT = 222


@pytest.fixture
def tg_settings(settings):
    return replace(
        settings,
        telegram_enabled=True,
        telegram_bot_token="test-token-should-never-leak",
        telegram_allowed_user_id=AUTHORIZED_USER,
        telegram_chat_id=str(AUTHORIZED_CHAT),
    )


@pytest.fixture
def sent_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.telegram_bot.send_message",
        lambda *a, **k: (calls.append((a, k)), "999")[1],
    )
    return calls


@pytest.fixture
def edit_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.telegram_bot.edit_message",
        lambda *a, **k: (calls.append((a, k)), True)[1],
    )
    return calls


@pytest.fixture
def answer_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.telegram_bot.answer_callback_query", lambda *a, **k: calls.append((a, k))
    )
    return calls


def _callback_query(*, user_id, chat_id, data, message_id=42):
    return {
        "id": "cbid1",
        "from": {"id": user_id},
        "message": {"chat": {"id": chat_id}, "message_id": message_id},
        "data": data,
    }


def _message(*, user_id, chat_id, text):
    return {"from": {"id": user_id}, "chat": {"id": chat_id}, "text": text}


# ---- offset resume (restart must not skip or replay updates) -----------------------


def test_resume_offset_uses_stored_value_directly() -> None:
    """The poll loop persists update_id + 1 as the next offset; resuming must use
    that value as-is. Regression test for a bug where resuming added 1 again,
    silently skipping one buffered update on every bot restart."""
    assert telegram_bot._resume_offset("47") == 47
    assert telegram_bot._resume_offset(None) is None


def test_offset_round_trips_through_database(settings) -> None:
    database = Database(settings.database_path)
    database.set_telegram_state("update_offset", "1000")
    assert telegram_bot._resume_offset(database.get_telegram_state("update_offset")) == 1000


# ---- authorization ----------------------------------------------------------------


def test_unauthorized_user_cannot_mutate(settings, tg_settings, edit_spy, answer_spy) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    update = {
        "callback_query": _callback_query(
            user_id=999, chat_id=AUTHORIZED_CHAT, data=f"reject:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert database.get_listing(listing_db_id)["status"] == "review_required"
    assert not edit_spy
    assert not answer_spy


def test_wrong_chat_cannot_mutate(settings, tg_settings, edit_spy, answer_spy) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=555, data=f"reject:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert database.get_listing(listing_db_id)["status"] == "review_required"
    assert not edit_spy


def test_malformed_callback_is_acknowledged_and_ignored(
    settings, tg_settings, edit_spy, answer_spy
) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data="'; DROP TABLE listings; --"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert database.get_listing(listing_db_id)["status"] == "review_required"
    assert not edit_spy
    assert answer_spy  # spinner cleared, nothing executed


def test_unrecognized_action_in_valid_shape_is_ignored(
    settings, tg_settings, edit_spy, answer_spy
) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER,
            chat_id=AUTHORIZED_CHAT,
            data=f"delete_everything:{listing_db_id}",
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert not edit_spy


# ---- two-step send confirmation ----------------------------------------------------


def test_first_send_tap_never_sends(
    settings, tg_settings, edit_spy, answer_spy, monkeypatch
) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)

    def fail(*_a, **_k):
        raise AssertionError("approve_listing must not be called on the first tap")

    monkeypatch.setattr("app.telegram_bot.approve_listing", fail)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"send_request:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert database.get_listing(listing_db_id)["status"] == "review_required"
    pending = database.pop_pending_telegram_send(listing_db_id)
    assert pending is not None
    assert "Send application for DB" in edit_spy[0][0][3]


def test_confirm_send_calls_existing_send_path(
    settings, tg_settings, edit_spy, monkeypatch
) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    database.set_pending_telegram_send(listing_db_id, str(AUTHORIZED_CHAT))
    captured = {}

    def fake_approve(database_, settings_, listing_db_id_, *, confirmed, trigger, contact_fn):
        captured["listing_db_id"] = listing_db_id_
        captured["confirmed"] = confirmed
        captured["trigger"] = trigger
        from app.review import ReviewActionResult

        # The real approve_listing()/contact_listing() persist the outcome to
        # listings.status; the handler now re-reads the DB fresh, so the fake must
        # mirror that side effect for the re-read to reflect the intended outcome.
        with database_.connect() as connection:
            connection.execute("UPDATE listings SET status='sent' WHERE id=?", (listing_db_id_,))
        return ReviewActionResult(True, "sent", "ok", contact_result=ContactResult(status="sent"))

    monkeypatch.setattr("app.telegram_bot.approve_listing", fake_approve)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"send_confirm:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert captured["listing_db_id"] == listing_db_id
    assert captured["confirmed"] is True
    assert captured["trigger"] == "telegram"
    assert "confirmed sent" in edit_spy[-1][0][3]


def test_cancel_sends_nothing(settings, tg_settings, edit_spy, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    database.set_pending_telegram_send(listing_db_id, str(AUTHORIZED_CHAT))

    def fail(*_a, **_k):
        raise AssertionError("approve_listing must not be called on cancel")

    monkeypatch.setattr("app.telegram_bot.approve_listing", fail)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"send_cancel:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert database.pop_pending_telegram_send(listing_db_id) is None
    assert database.get_listing(listing_db_id)["status"] == "review_required"


def test_expired_confirmation_is_refused(settings, tg_settings, edit_spy, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    database.set_pending_telegram_send(listing_db_id, str(AUTHORIZED_CHAT))
    with database.connect() as connection:
        connection.execute(
            "UPDATE telegram_pending_sends SET created_at='2000-01-01T00:00:00+00:00' "
            "WHERE listing_db_id=?",
            (listing_db_id,),
        )

    def fail(*_a, **_k):
        raise AssertionError("approve_listing must not be called for an expired confirmation")

    monkeypatch.setattr("app.telegram_bot.approve_listing", fail)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"send_confirm:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert "expired" in edit_spy[-1][0][3]


def test_duplicate_confirm_callbacks_cannot_double_send(
    settings, tg_settings, edit_spy, monkeypatch
) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    database.set_pending_telegram_send(listing_db_id, str(AUTHORIZED_CHAT))
    call_count = {"n": 0}

    def fake_approve(database_, settings_, listing_db_id_, *, confirmed, trigger, contact_fn):
        call_count["n"] += 1
        from app.review import ReviewActionResult

        with database_.connect() as connection:
            connection.execute("UPDATE listings SET status='sent' WHERE id=?", (listing_db_id_,))
        return ReviewActionResult(True, "sent", "ok", contact_result=ContactResult(status="sent"))

    monkeypatch.setattr("app.telegram_bot.approve_listing", fake_approve)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"send_confirm:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)
    telegram_bot.process_update(database, tg_settings, update)

    assert call_count["n"] == 1
    assert "No pending send confirmation" in edit_spy[-1][0][3]


def test_stale_button_rechecks_current_db_state_before_sending(
    settings, tg_settings, edit_spy, monkeypatch
) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    database.set_pending_telegram_send(listing_db_id, str(AUTHORIZED_CHAT))
    # Simulate the listing having been sent through some other path since the button
    # was shown (e.g. a stale notification days old).
    with database.connect() as connection:
        connection.execute("UPDATE listings SET status='sent' WHERE id=?", (listing_db_id,))

    def fail(*_a, **_k):
        raise AssertionError("approve_listing must not be called for an already-sent listing")

    monkeypatch.setattr("app.telegram_bot.approve_listing", fail)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"send_confirm:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert "already sent" in edit_spy[-1][0][3]


@pytest.mark.parametrize(
    "status", ["sent", "already_contacted", "send_state_unknown", "manually_rejected"]
)
def test_unsafe_statuses_refuse_send_request(settings, tg_settings, edit_spy, status) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database, status="review_required")
    with database.connect() as connection:
        connection.execute("UPDATE listings SET status=? WHERE id=?", (status, listing_db_id))
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"send_request:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert database.pop_pending_telegram_send(listing_db_id) is None
    assert "Cannot send" in edit_spy[-1][0][3]


def test_already_contacted_confirm_result_shows_no_send_or_reject_button(
    settings, tg_settings, edit_spy, monkeypatch
) -> None:
    """Reproduces the reported bug exactly: a Send confirmation resolves to
    already_contacted (an existing WG-Gesucht conversation was detected before
    sending), and the resulting Telegram message must not offer Send or Reject
    again -- and must use human-readable wording, not the raw DOM detection string."""
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    database.set_pending_telegram_send(listing_db_id, str(AUTHORIZED_CHAT))

    def fake_approve(database_, settings_, listing_db_id_, *, confirmed, trigger, contact_fn):
        from app.review import ReviewActionResult

        with database_.connect() as connection:
            connection.execute(
                "UPDATE listings SET status='already_contacted' WHERE id=?", (listing_db_id_,)
            )
        return ReviewActionResult(
            False,
            "already_contacted",
            "existing conversation detected",
            contact_result=ContactResult(
                status="already_contacted",
                detail="existing conversation action detected from DOM destination/state",
            ),
        )

    monkeypatch.setattr("app.telegram_bot.approve_listing", fake_approve)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"send_confirm:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    final_call = edit_spy[-1][0]
    final_text = final_call[3]
    final_keyboard = final_call[4]
    assert "existing conversation action detected from DOM" not in final_text
    assert "already contacted" in final_text.casefold()
    labels = [b["text"] for row in final_keyboard["inline_keyboard"] for b in row]
    assert labels == ["🔗 Open Listing"]
    assert database.get_listing(listing_db_id)["status"] == "already_contacted"


def test_stale_send_callback_after_already_contacted_is_refused(
    settings, tg_settings, edit_spy, monkeypatch
) -> None:
    """After the listing has settled to already_contacted, a leftover/stale Send tap
    (e.g. from an old, un-refreshed Telegram message) must still be safely refused --
    the gate is the live DB status, never what buttons a stale message happens to
    still display."""
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    with database.connect() as connection:
        connection.execute(
            "UPDATE listings SET status='already_contacted' WHERE id=?", (listing_db_id,)
        )

    def fail(*_a, **_k):
        raise AssertionError("approve_listing must not be called for an already_contacted listing")

    monkeypatch.setattr("app.telegram_bot.approve_listing", fail)
    stale_send_request = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"send_request:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, stale_send_request)

    assert "Cannot send" in edit_spy[-1][0][3]
    assert database.pop_pending_telegram_send(listing_db_id) is None
    # Even if a pending confirmation somehow already existed (e.g. a race with an
    # older tap), a stale Confirm callback must also be refused.
    database.set_pending_telegram_send(listing_db_id, str(AUTHORIZED_CHAT))
    stale_confirm = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"send_confirm:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, stale_confirm)

    assert "Refusing to send" in edit_spy[-1][0][3]
    assert database.get_listing(listing_db_id)["status"] == "already_contacted"


def test_send_state_unknown_never_gets_a_normal_send_action() -> None:
    from app.telegram_notify import send_state_unknown_keyboard

    keyboard = send_state_unknown_keyboard(1, "https://example.test")
    callback_data = [
        button["callback_data"]
        for row in keyboard["inline_keyboard"]
        for button in row
        if "callback_data" in button
    ]
    assert not any(
        data.startswith("send_request") or data.startswith("send_confirm") for data in callback_data
    )
    assert any(data.startswith("reconcile") for data in callback_data)


# ---- reject / reconcile reuse existing shared logic ---------------------------------


def test_reject_calls_existing_rejection_logic(
    settings, tg_settings, edit_spy, monkeypatch
) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    captured = {}

    def fake_reject(database_, listing_db_id_, *, confirmed):
        captured["listing_db_id"] = listing_db_id_
        captured["confirmed"] = confirmed
        from app.review import ReviewActionResult

        return ReviewActionResult(True, "manually_rejected", "listing marked manually_rejected")

    monkeypatch.setattr("app.telegram_bot.reject_listing", fake_reject)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"reject:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert captured == {"listing_db_id": listing_db_id, "confirmed": True}
    assert "rejected" in edit_spy[-1][0][3]


def test_reject_cannot_corrupt_sent_history(settings, tg_settings, edit_spy) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database, status="review_required")
    with database.connect() as connection:
        connection.execute("UPDATE listings SET status='sent' WHERE id=?", (listing_db_id,))
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"reject:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert database.get_listing(listing_db_id)["status"] == "sent"
    assert "Cannot reject" in edit_spy[-1][0][3]


def test_reconcile_never_sends(settings, tg_settings, edit_spy, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database, status="send_state_unknown")

    def fake_reconcile_fn(url, settings_, expected_body, listing_id):
        from app.browser import ReconciliationOutcome

        return ReconciliationOutcome(result="not_sent", detail="no application found")

    def fail_contact(*_a, **_k):
        raise AssertionError("reconcile must never send")

    monkeypatch.setattr("app.telegram_bot.reconcile_wg_url", fake_reconcile_fn)
    monkeypatch.setattr("app.telegram_bot.contact_listing", fail_contact)
    monkeypatch.setattr("app.telegram_bot.approve_listing", fail_contact)
    with database.connect() as connection:
        connection.execute(
            "UPDATE listings SET message_body=?, canonical_url=? WHERE id=?",
            (BODY, "https://www.wg-gesucht.de/x.html", listing_db_id),
        )
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"reconcile:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert "No prior application" in edit_spy[-1][0][3]


# ---- Telegram outage / security isolation ------------------------------------------


def test_telegram_outage_does_not_crash_processing(monkeypatch, tg_settings) -> None:
    def boom(*_a, **_k):
        raise RuntimeError(
            f"connection failed to https://api.telegram.org/bot{tg_settings.telegram_bot_token}/x"
        )

    monkeypatch.setattr("httpx.post", boom)

    from app.telegram_notify import send_message

    result = send_message(tg_settings, tg_settings.telegram_chat_id, "hello")

    assert result is None  # failed gracefully, no exception raised


def test_bot_token_never_appears_in_logs(monkeypatch, tg_settings, caplog) -> None:
    def boom(*_a, **_k):
        raise RuntimeError(
            f"connect timeout for https://api.telegram.org/bot{tg_settings.telegram_bot_token}/sendMessage"
        )

    monkeypatch.setattr("httpx.post", boom)

    from app.telegram_notify import send_message

    with caplog.at_level("WARNING"):
        send_message(tg_settings, tg_settings.telegram_chat_id, "hello")

    log_text = caplog.text
    assert tg_settings.telegram_bot_token not in log_text


def test_text_command_send_still_requires_confirmation(
    settings, tg_settings, sent_spy, monkeypatch
) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)

    def fail(*_a, **_k):
        raise AssertionError("/send must open a confirmation, not send directly")

    monkeypatch.setattr("app.telegram_bot.approve_listing", fail)
    update = {
        "message": _message(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, text=f"/send {listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    assert database.pop_pending_telegram_send(listing_db_id) is not None


def test_dry_run_result_is_reported_without_pretending_sent(
    settings, tg_settings, edit_spy, monkeypatch
) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    database.set_pending_telegram_send(listing_db_id, str(AUTHORIZED_CHAT))

    def fake_approve(database_, settings_, listing_db_id_, *, confirmed, trigger, contact_fn):
        from app.review import ReviewActionResult

        with database_.connect() as connection:
            connection.execute(
                "UPDATE listings SET status='dry_run_ready' WHERE id=?", (listing_db_id_,)
            )
        return ReviewActionResult(
            True,
            "dry_run_ready",
            "ok",
            contact_result=ContactResult(status="dry_run_ready", detail="stopped before Send"),
        )

    monkeypatch.setattr("app.telegram_bot.approve_listing", fake_approve)
    update = {
        "callback_query": _callback_query(
            user_id=AUTHORIZED_USER, chat_id=AUTHORIZED_CHAT, data=f"send_confirm:{listing_db_id}"
        )
    }

    telegram_bot.process_update(database, tg_settings, update)

    last_text = edit_spy[-1][0][3]
    assert "confirmed sent" not in last_text
    assert "DRY_RUN" in last_text
