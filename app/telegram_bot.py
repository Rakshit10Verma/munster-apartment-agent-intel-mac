from __future__ import annotations

import logging
import re
import signal
import time
from datetime import UTC, datetime
from typing import Any

from .browser import reconcile_wg_url
from .config_loader import Settings, load_all
from .contact import contact_listing
from .database import Database
from .review import (
    APPROVABLE_STATUSES,
    approval_blockers,
    approve_listing,
    current_status_reason,
    load_listing_record,
    missing_fact_question,
    reconcile_listing,
    reject_listing,
)
from .telegram_notify import (
    answer_callback_query,
    confirm_send_keyboard,
    edit_message,
    get_updates,
    render_listing_card,
    send_message,
)

logger = logging.getLogger("apartment_agent.telegram_bot")

# A confirmation older than this is refused rather than acted on: never trust a stale
# Telegram button tap, always re-read the current DB state before doing anything real.
PENDING_SEND_EXPIRY_SECONDS = 600

_CALLBACK_PATTERN = re.compile(
    r"^(send_request|send_confirm|send_cancel|reject|reconcile|dismiss):(\d+)$"
)
_TEXT_COMMAND_PATTERN = re.compile(r"^/(review|send|reject|reconcile)(?:@\w+)?\s+(\d+)\s*$")

# Human-readable reasons for the small set of statuses that commonly reach here.
# Anything else not in APPROVABLE_STATUSES still gets a safe, generic refusal (see
# _unsendable_reason) -- this dict is only for a nicer message, never the gate itself.
_UNSENDABLE_REASONS: dict[str, str] = {
    "sent": "this listing is already sent.",
    "already_contacted": "WG-Gesucht already shows a conversation for this listing.",
    "send_state_unknown": "a previous send attempt is unresolved -- reconcile it first.",
    "manually_rejected": "this listing was manually rejected.",
    "not_sent": "reconciliation found nothing was ever sent; use /review to reprocess if needed.",
}


def _unsendable_reason(status: str) -> str | None:
    """Allowlist, not a denylist: only a status in APPROVABLE_STATUSES (the exact set
    approve_listing's own gate uses) is ever considered sendable. Matches
    telegram_notify.keyboard_for_status so a Send button is never shown for -- or
    accepted from -- a status the real approval path would refuse anyway."""
    if status in APPROVABLE_STATUSES:
        return None
    return _UNSENDABLE_REASONS.get(status, f"listing status is '{status}', which cannot be sent.")


def _is_authorized(settings: Settings, user_id: Any, chat_id: Any) -> bool:
    """Only the configured user, in the configured chat, may mutate anything. Any
    other user/chat is silently ignored -- never authorized by bot username, command
    text, or display name alone."""
    if not settings.telegram_allowed_user_id or not settings.telegram_chat_id:
        return False
    try:
        return int(user_id) == settings.telegram_allowed_user_id and str(chat_id) == str(
            settings.telegram_chat_id
        )
    except (TypeError, ValueError):
        return False


def _handle_send_request(
    database: Database, settings: Settings, chat_id: Any, listing_db_id: int, message_id: str
) -> None:
    row = database.get_listing(listing_db_id)
    if row is None:
        edit_message(settings, chat_id, message_id, f"DB {listing_db_id} was not found.")
        return
    reason = _unsendable_reason(str(row["status"]))
    if reason:
        edit_message(settings, chat_id, message_id, f"Cannot send DB {listing_db_id}: {reason}")
        return
    record = load_listing_record(row)
    blockers = approval_blockers(record, database.get_actual_contact_attempt(listing_db_id))
    if blockers:
        edit_message(
            settings,
            chat_id,
            message_id,
            f"⚠️ DB {listing_db_id} needs review before sending.\n\n"
            f"{current_status_reason(record)}\n\nOpen /review {listing_db_id} for details.",
        )
        return
    # First tap NEVER sends: it only records a short-lived pending confirmation and
    # asks for an explicit second tap.
    database.set_pending_telegram_send(listing_db_id, str(chat_id))
    edit_message(
        settings,
        chat_id,
        message_id,
        (
            f"Retry application for DB {listing_db_id}? The first attempt did not click Send."
            if row["status"] == "bewerbermappe_attachment_failed"
            else f"Send application for DB {listing_db_id}?"
        ),
        confirm_send_keyboard(listing_db_id),
    )


def _handle_send_confirm(
    database: Database, settings: Settings, chat_id: Any, listing_db_id: int, message_id: str
) -> None:
    pending = database.pop_pending_telegram_send(listing_db_id)
    if pending is None or str(pending["chat_id"]) != str(chat_id):
        edit_message(
            settings,
            chat_id,
            message_id,
            f"No pending send confirmation for DB {listing_db_id}. This tap sent nothing; "
            f"the confirmation was already used or expired. Re-open with /review "
            f"{listing_db_id} to start a new two-step confirmation.",
        )
        return
    try:
        age_seconds = (
            datetime.now(UTC) - datetime.fromisoformat(str(pending["created_at"]))
        ).total_seconds()
    except ValueError:
        age_seconds = 0.0
    if age_seconds > PENDING_SEND_EXPIRY_SECONDS:
        edit_message(
            settings,
            chat_id,
            message_id,
            f"Confirmation for DB {listing_db_id} expired. Re-open with /review {listing_db_id}.",
        )
        return
    # Never trust the notification's own idea of state: re-read the listing and every
    # safety gate fresh, immediately before doing anything real.
    row = database.get_listing(listing_db_id)
    if row is None:
        edit_message(settings, chat_id, message_id, f"DB {listing_db_id} was not found.")
        return
    reason = _unsendable_reason(str(row["status"]))
    if reason:
        edit_message(
            settings, chat_id, message_id, f"Refusing to send DB {listing_db_id}: {reason}"
        )
        return
    record = load_listing_record(row)
    blockers = approval_blockers(record, database.get_actual_contact_attempt(listing_db_id))
    if blockers:
        edit_message(
            settings,
            chat_id,
            message_id,
            f"⚠️ DB {listing_db_id} needs your input.\n\n"
            f"Question: {missing_fact_question(record)}\n\n"
            f"Open /review {listing_db_id} for technical details.",
        )
        return
    edit_message(settings, chat_id, message_id, f"⏳ Sending and verifying DB {listing_db_id}...")
    # Same shared pipeline approve.sh and AUTO_SEND use -- every live gate (Premium,
    # Bewerbermappe, composer match, duplicate-send) is re-checked in the browser
    # regardless of how this call was triggered.
    approve_listing(
        database,
        settings,
        listing_db_id,
        confirmed=True,
        trigger="telegram",
        contact_fn=contact_listing,
    )
    # Always re-read the DB fresh for the final text/keyboard -- never build them from
    # the approve_listing() return value directly, so the message and its buttons can
    # never drift from what actually got persisted (this is exactly the bug class this
    # function guards against: a stale/incorrect Send button surviving a state change).
    rendered = render_listing_card(database, listing_db_id)
    if rendered is None:
        edit_message(settings, chat_id, message_id, f"DB {listing_db_id} was not found.")
        return
    text, keyboard, status = rendered
    if status not in {
        "sent",
        "already_contacted",
        "send_state_unknown",
        "bewerbermappe_attachment_failed",
        "manually_rejected",
        "filtered_skip",
        "dry_run_ready",
    }:
        # A genuine contact-attempt failure (e.g. a live Bewerbermappe/Premium/composer
        # gate). Point at /review for the exact technical reason rather than showing
        # raw debug detail here.
        text = f"⚠️ DB {listing_db_id} was not sent.\n\nSee /review {listing_db_id} for details."
    edit_message(settings, chat_id, message_id, text, keyboard)
    if status in {"sent", "already_contacted", "send_state_unknown"}:
        database.record_telegram_notification(listing_db_id, status, str(chat_id), message_id)


def _handle_send_cancel(
    database: Database, settings: Settings, chat_id: Any, listing_db_id: int, message_id: str
) -> None:
    database.clear_pending_telegram_send(listing_db_id)
    rendered = render_listing_card(database, listing_db_id)
    if rendered is None:
        edit_message(settings, chat_id, message_id, f"DB {listing_db_id} was not found.")
        return
    text, keyboard, _status = rendered
    edit_message(settings, chat_id, message_id, text, keyboard)


def _handle_reject(
    database: Database, settings: Settings, chat_id: Any, listing_db_id: int, message_id: str
) -> None:
    action = reject_listing(database, listing_db_id, confirmed=True)
    if action.ok:
        edit_message(settings, chat_id, message_id, f"❌ DB {listing_db_id} rejected")
    else:
        edit_message(
            settings, chat_id, message_id, f"Cannot reject DB {listing_db_id}: {action.detail}"
        )


def _handle_reconcile(
    database: Database, settings: Settings, chat_id: Any, listing_db_id: int, message_id: str
) -> None:
    action = reconcile_listing(database, settings, listing_db_id, reconcile_fn=reconcile_wg_url)
    if action.reconciliation is None:
        edit_message(
            settings, chat_id, message_id, f"Cannot reconcile DB {listing_db_id}: {action.detail}"
        )
        return
    result = action.reconciliation.result
    # reconcile_listing() just set listings.status to exactly this result -- re-read
    # fresh for the keyboard, so e.g. a "not_sent" result (not currently re-approvable;
    # see APPROVABLE_STATUSES) never shows a Send button that would just be refused.
    rendered = render_listing_card(database, listing_db_id)
    if rendered is None:
        edit_message(settings, chat_id, message_id, f"DB {listing_db_id} was not found.")
        return
    _card_text, keyboard, _status = rendered
    if result == "sent":
        text = f"✅ Previous application confirmed sent\nDB {listing_db_id}"
    elif result == "not_sent":
        text = f"No prior application was found for DB {listing_db_id}."
    else:
        text = f"❓ Send state still uncertain — do not retry yet\nDB {listing_db_id}"
    edit_message(settings, chat_id, message_id, text, keyboard)


def _handle_dismiss(
    database: Database, settings: Settings, chat_id: Any, listing_db_id: int, message_id: str
) -> None:
    edit_message(settings, chat_id, message_id, f"Dismissed DB {listing_db_id}.")


_ACTIONS: dict[str, Any] = {
    "send_request": _handle_send_request,
    "send_confirm": _handle_send_confirm,
    "send_cancel": _handle_send_cancel,
    "reject": _handle_reject,
    "reconcile": _handle_reconcile,
    "dismiss": _handle_dismiss,
}


def _process_callback_query(
    database: Database, settings: Settings, callback: dict[str, Any]
) -> None:
    query_id = str(callback.get("id") or "")
    from_id = (callback.get("from") or {}).get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    data = str(callback.get("data") or "")
    if not _is_authorized(settings, from_id, chat_id):
        return
    match = _CALLBACK_PATTERN.match(data)
    if not match or message_id is None:
        # Malformed/unrecognized callback data: never eval/exec/shell out, just
        # acknowledge (clears the client-side loading spinner) and drop it.
        answer_callback_query(settings, query_id)
        return
    action, raw_id = match.groups()
    answer_callback_query(settings, query_id)
    _ACTIONS[action](database, settings, chat_id, int(raw_id), str(message_id))


def _send_review_card(
    database: Database, settings: Settings, chat_id: Any, listing_db_id: int
) -> None:
    rendered = render_listing_card(database, listing_db_id)
    if rendered is None:
        send_message(settings, chat_id, f"DB {listing_db_id} was not found.")
        return
    text, keyboard, _status = rendered
    send_message(settings, chat_id, text, keyboard)


def _process_text_command(database: Database, settings: Settings, message: dict[str, Any]) -> None:
    from_id = (message.get("from") or {}).get("id")
    raw_chat_id = (message.get("chat") or {}).get("id")
    text = str(message.get("text") or "").strip()
    if not _is_authorized(settings, from_id, raw_chat_id):
        return
    chat_id = str(raw_chat_id)
    if text == "/status":
        current, preserved = database.status_counts_split()
        send_message(
            settings,
            chat_id,
            "Current run: "
            + (", ".join(f"{k}={v}" for k, v in current.items()) or "empty")
            + "\nPreserved contact history: "
            + (", ".join(f"{k}={v}" for k, v in preserved.items()) or "empty"),
        )
        return
    match = _TEXT_COMMAND_PATTERN.match(text)
    if not match:
        return
    command, raw_id = match.groups()
    listing_db_id = int(raw_id)
    if command == "review":
        _send_review_card(database, settings, chat_id, listing_db_id)
        return
    row = database.get_listing(listing_db_id)
    if row is None:
        send_message(settings, chat_id, f"DB {listing_db_id} was not found.")
        return
    if command == "send":
        # Same two-step confirmation as the button: this only ever opens the confirm
        # prompt, never sends directly.
        reason = _unsendable_reason(str(row["status"]))
        if reason:
            send_message(settings, chat_id, f"Cannot send DB {listing_db_id}: {reason}")
            return
        record = load_listing_record(row)
        if approval_blockers(record, database.get_actual_contact_attempt(listing_db_id)):
            send_message(
                settings,
                chat_id,
                f"⚠️ DB {listing_db_id} needs review before sending.\n"
                f"{current_status_reason(record)}\nOpen /review {listing_db_id} for details.",
            )
            return
        database.set_pending_telegram_send(listing_db_id, str(chat_id))
        send_message(
            settings,
            chat_id,
            f"Send application for DB {listing_db_id}?",
            confirm_send_keyboard(listing_db_id),
        )
        return
    if command == "reject":
        action = reject_listing(database, listing_db_id, confirmed=True)
        send_message(
            settings,
            chat_id,
            f"❌ DB {listing_db_id} rejected" if action.ok else f"Cannot reject: {action.detail}",
        )
        return
    if command == "reconcile":
        action = reconcile_listing(database, settings, listing_db_id, reconcile_fn=reconcile_wg_url)
        if action.reconciliation is not None:
            send_message(
                settings,
                chat_id,
                f"Reconcile result for DB {listing_db_id}: {action.reconciliation.result}",
            )
        else:
            send_message(settings, chat_id, f"Cannot reconcile: {action.detail}")
        return


def process_update(database: Database, settings: Settings, update: dict[str, Any]) -> None:
    """Dispatch one Telegram update. Never raises: a single malformed/unexpected
    update must never stop the polling loop."""
    try:
        if "callback_query" in update:
            _process_callback_query(database, settings, update["callback_query"])
        elif "message" in update:
            _process_text_command(database, settings, update["message"])
    except Exception:
        logger.exception("Telegram update processing failed; continuing")


def _resume_offset(stored: str | None) -> int | None:
    """The persisted value is already the next offset to request (the poll loop
    stores update_id + 1 after processing each update) -- resuming must use it as-is,
    never add 1 again, or a restart would silently skip one buffered update."""
    return int(stored) if stored else None


def run(settings: Settings | None = None, database: Database | None = None) -> None:
    if settings is None:
        _, _, settings = load_all()
    if not settings.telegram_enabled:
        logger.warning("Telegram is disabled (TELEGRAM_ENABLED=false); nothing to do")
        return
    if not (
        settings.telegram_bot_token
        and settings.telegram_allowed_user_id
        and settings.telegram_chat_id
    ):
        logger.error(
            "TELEGRAM_ENABLED=true but TELEGRAM_BOT_TOKEN/TELEGRAM_ALLOWED_USER_ID/"
            "TELEGRAM_CHAT_ID are not all set; exiting"
        )
        return
    database = database or Database(settings.database_path)
    offset = _resume_offset(database.get_telegram_state("update_offset"))
    running = True

    def _stop(*_args: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    logger.info("Telegram bot started (long polling)")
    while running:
        updates = get_updates(settings, offset)
        for update in updates:
            process_update(database, settings, update)
            update_id = update.get("update_id")
            if update_id is not None:
                offset = int(update_id) + 1
                database.set_telegram_state("update_offset", str(offset))
        if not updates:
            time.sleep(1)


if __name__ == "__main__":
    run()
