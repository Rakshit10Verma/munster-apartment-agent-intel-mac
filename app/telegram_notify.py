from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from .config_loader import Settings
from .database import Database, has_send_evidence
from .review import (
    APPROVABLE_STATUSES,
    ListingRecord,
    approval_blockers,
    current_status_reason,
    current_unresolved_facts,
    load_listing_record,
    missing_fact_question,
)

logger = logging.getLogger("apartment_agent.telegram")

API_TIMEOUT_SECONDS = 15
LONG_POLL_TIMEOUT_SECONDS = 30

# Statuses worth a phone notification. Deliberately excludes filtered_skip/discovered
# (see PART 2: "Do NOT notify me for every filtered_skip. Do NOT create notification
# spam.") and any other transient/internal status.
NOTIFIABLE_STATUSES = frozenset(
    {
        "review_required",
        "drafted",
        "ai_failed",
        "premium_boost_failed",
        "bewerbermappe_attachment_failed",
        "send_state_unknown",
        "already_contacted",
        "sent",
    }
)

_HOUSING_LABELS: dict[str, str] = {
    "wg_room": "WG-Zimmer",
    "studio": "Studio",
    "whole_flat": "Wohnung",
    "zwischenmiete": "Zwischenmiete",
    "wohnheim": "Wohnheim",
    "nachmieter": "Nachmieter",
    "student_room": "Studentenzimmer",
}
_MESSAGE_SOURCE_LABELS: dict[str, str] = {
    "cloud_ai": "cloud AI",
    "universal_fallback": "deterministic fallback",
    "universal_answer_bank_fallback": "deterministic fallback",
    "none": "none",
}
_REASON_STATUSES = frozenset(
    {"review_required", "ai_failed", "premium_boost_failed", "bewerbermappe_attachment_failed"}
)
MESSAGE_PREVIEW_MAX_CHARS = 220


def _redact(text: str, token: str) -> str:
    """Strip the bot token out of a loggable string. httpx error strings often embed
    the full request URL, which otherwise would leak the token into logs."""
    return text.replace(token, "<redacted>") if token else text


def _api_url(settings: Settings, method: str) -> str:
    return f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"


def _post(
    settings: Settings, method: str, payload: dict[str, Any], *, timeout: float
) -> dict[str, Any] | None:
    if not settings.telegram_bot_token:
        return None
    try:
        response = httpx.post(_api_url(settings, method), json=payload, timeout=timeout)
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data
    except Exception as exc:
        logger.warning(
            "Telegram API call failed; apartment processing continues",
            extra={
                "fields": {
                    "method": method,
                    "error": _redact(str(exc)[:300], settings.telegram_bot_token),
                }
            },
        )
        return None


def get_updates(
    settings: Settings, offset: int | None, timeout: int = LONG_POLL_TIMEOUT_SECONDS
) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    result = _post(settings, "getUpdates", payload, timeout=timeout + 10)
    if result and result.get("ok"):
        return list(result.get("result", []))
    return []


def send_message(
    settings: Settings, chat_id: str, text: str, reply_markup: dict[str, Any] | None = None
) -> str | None:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = _post(settings, "sendMessage", payload, timeout=API_TIMEOUT_SECONDS)
    if result and result.get("ok"):
        return str(result["result"]["message_id"])
    return None


def edit_message(
    settings: Settings,
    chat_id: str,
    message_id: str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000]}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = _post(settings, "editMessageText", payload, timeout=API_TIMEOUT_SECONDS)
    return bool(result and result.get("ok"))


def answer_callback_query(settings: Settings, callback_query_id: str, text: str = "") -> None:
    _post(
        settings,
        "answerCallbackQuery",
        {"callback_query_id": callback_query_id, "text": text[:200]},
        timeout=API_TIMEOUT_SECONDS,
    )


def _open_button(url: str) -> dict[str, str]:
    return {"text": "🔗 Open Listing", "url": url or "https://www.wg-gesucht.de"}


def review_keyboard(listing_db_id: int, url: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [_open_button(url)],
            [
                {"text": "✅ Send", "callback_data": f"send_request:{listing_db_id}"},
                {"text": "❌ Reject", "callback_data": f"reject:{listing_db_id}"},
            ],
        ]
    }


def attachment_retry_keyboard(listing_db_id: int, url: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [_open_button(url)],
            [
                {"text": "🔁 Retry Send", "callback_data": f"send_request:{listing_db_id}"},
                {"text": "❌ Reject", "callback_data": f"reject:{listing_db_id}"},
            ],
        ]
    }


def send_state_unknown_keyboard(listing_db_id: int, url: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [_open_button(url)],
            [
                {"text": "🔍 Reconcile", "callback_data": f"reconcile:{listing_db_id}"},
                {"text": "Dismiss", "callback_data": f"dismiss:{listing_db_id}"},
            ],
        ]
    }


def confirm_send_keyboard(listing_db_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Confirm Send", "callback_data": f"send_confirm:{listing_db_id}"},
                {"text": "↩ Cancel", "callback_data": f"send_cancel:{listing_db_id}"},
            ]
        ]
    }


def open_only_keyboard(url: str) -> dict[str, Any]:
    """Open Listing only -- no Send, no Reject, no Reconcile. For any status that is
    settled (sent/already_contacted/manually_rejected) or not currently approvable
    (filtered_skip and anything outside APPROVABLE_STATUSES, e.g. dry_run_ready)."""
    return {"inline_keyboard": [[_open_button(url)]]}


def format_listing_notification(row: dict[str, Any], record: ListingRecord) -> str:
    """Concise, human-readable notification text. Deliberately never dumps internal
    debug traces (see ./review.sh <id> for those); the full technical reason stays in
    `review_reason_debug`, not here."""
    facts = record.facts
    housing = _HOUSING_LABELS.get(facts.housing_type, facts.housing_type)
    rent = (
        f"€{facts.warm_rent_eur:g} warm"
        if facts.warm_rent_eur
        else (f"€{facts.cold_rent_eur:g} cold" if facts.cold_rent_eur else "rent unknown")
    )
    size = f"{facts.room_size_m2:g} m²" if facts.room_size_m2 else None
    lines = [
        f"🏠 DB {row['id']} — {row.get('title') or '(no title)'}",
        "",
        housing,
        rent + (f", {size}" if size else ""),
        f"Available: {facts.move_in or 'unknown'}",
    ]
    if facts.age_min or facts.age_max:
        age_range = f"{facts.age_min or '?'}-{facts.age_max or '?'}"
        mismatch = " -- soft mismatch, still applicable" if facts.age_mismatch else ""
        lines.append(f"Age preference: {age_range}{mismatch}")
    status = str(row.get("status") or "")
    message_source = _MESSAGE_SOURCE_LABELS.get(row.get("message_source") or "none", "none")
    lines += [
        "",
        f"Status: {status}",
        f"Message source: {message_source}",
        f"Draft ready: {'yes' if record.message and record.message.body.strip() else 'no'}",
    ]
    if status in _REASON_STATUSES:
        lines.append(f"Reason for attention: {record.review_reason_human}")
    if record.message and record.message.body.strip():
        preview = " ".join(record.message.body.split())
        if len(preview) > MESSAGE_PREVIEW_MAX_CHARS:
            preview = preview[:MESSAGE_PREVIEW_MAX_CHARS] + "..."
        lines += ["", f"Message preview: {preview}"]
    url = str(row.get("canonical_url") or "")
    if url:
        lines += ["", url]
    return "\n".join(lines)


def keyboard_for_status(
    status: str,
    listing_db_id: int,
    url: str,
    *,
    safe_attachment_retry: bool = False,
    send_evidence: bool = False,
) -> dict[str, Any]:
    """The ONLY place that decides which Telegram buttons a listing status may show.
    Always call this with a freshly re-read status -- never a cached/stale one.

    Allowlist, not a denylist: Send/Reject only ever appear for a status that is
    actually in APPROVABLE_STATUSES (the same set approve_listing's own safety gate
    uses), so this can never drift into showing a button that the real approval path
    would immediately refuse anyway. Everything else -- sent, already_contacted,
    manually_rejected, filtered_skip, dry_run_ready, or any future/unexpected status
    -- safely falls back to Open Listing only.
    """
    if status == "send_state_unknown":
        return send_state_unknown_keyboard(listing_db_id, url)
    if status == "bewerbermappe_attachment_failed":
        if send_evidence:
            return send_state_unknown_keyboard(listing_db_id, url)
        if safe_attachment_retry:
            return attachment_retry_keyboard(listing_db_id, url)
        return open_only_keyboard(url)
    if status in APPROVABLE_STATUSES:
        return review_keyboard(listing_db_id, url)
    return open_only_keyboard(url)


# Concise, human-readable replacements for a settled/terminal status, standing in for
# the generic listing card. Never includes the raw technical DOM/debug reason -- that
# stays in review_reason_debug / ./review.sh only.
def _status_headline(status: str, listing_db_id: int) -> str | None:
    if status == "sent":
        return f"✅ Application confirmed sent\nDB {listing_db_id}"
    if status == "already_contacted":
        return (
            f"✅ DB {listing_db_id} was already contacted earlier.\n\n"
            "An existing WG-Gesucht conversation was detected, so no duplicate "
            "application was sent."
        )
    if status == "send_state_unknown":
        return (
            f"⚠️ Send state uncertain\nDB {listing_db_id}\n\nDo not retry. Reconciliation required."
        )
    if status == "manually_rejected":
        return f"❌ DB {listing_db_id} was rejected."
    if status == "filtered_skip":
        return f"DB {listing_db_id} was filtered out automatically; no action needed."
    if status == "dry_run_ready":
        return f"DRY_RUN is active: DB {listing_db_id} was prepared but not actually sent."
    return None


def render_listing_card(
    database: Database, listing_db_id: int
) -> tuple[str, dict[str, Any], str] | None:
    """Build the current Telegram text/keyboard for a listing, ALWAYS from a freshly
    re-read DB row -- this is the one place every handler (notifications, callback
    results, text commands) must use, so an impossible action (e.g. Send on an
    already_contacted listing) can never remain visible after a state change. Returns
    None if the listing no longer exists. The third element is the current status, for
    callers that also need it (e.g. to update the notification-dedup ledger)."""
    row = database.get_listing(listing_db_id)
    if row is None:
        return None
    status = str(row["status"])
    url = str(row.get("canonical_url") or "")
    record = load_listing_record(row)
    attempt = database.get_actual_contact_attempt(listing_db_id) or {}
    retry_safe = status == "bewerbermappe_attachment_failed" and not approval_blockers(
        record, attempt
    )
    headline = _status_headline(status, listing_db_id)
    if status == "bewerbermappe_attachment_failed":
        if has_send_evidence(attempt):
            text = f"⚠️ DB {listing_db_id}: send state is uncertain. Do not retry; reconcile first."
        elif retry_safe:
            text = (
                f"⚠️ DB {listing_db_id} was not sent.\n"
                "The Bewerbermappe could not be attached. You can retry safely."
            )
        else:
            text = (
                f"⚠️ DB {listing_db_id} was not sent.\n"
                "The Bewerbermappe could not be attached. Other safety checks also "
                "need review; see /review for details."
            )
        if url:
            text += f"\n\n{url}"
    elif status == "review_required" and current_unresolved_facts(record):
        text = (
            f"⚠️ DB {listing_db_id} needs your input.\n\nQuestion: {missing_fact_question(record)}"
        )
        if url:
            text += f"\n\n{url}"
    elif status == "review_required" and record.facts.parental_guarantor_required:
        text = (
            f"⚠️ DB {listing_db_id} needs review before sending.\n\n{current_status_reason(record)}"
        )
        if url:
            text += f"\n\n{url}"
    elif headline is not None:
        text = headline
    else:
        text = format_listing_notification(row, record)
    return (
        text,
        keyboard_for_status(
            status,
            listing_db_id,
            url,
            safe_attachment_retry=retry_safe,
            send_evidence=has_send_evidence(attempt),
        ),
        status,
    )


def notify_listing_if_changed(
    database: Database, settings: Settings, listing_db_id: int | None
) -> None:
    """Send (or refresh) a Telegram notification for a listing's current state, only
    if Telegram is enabled and the state actually changed since the last
    notification. Never raises: Telegram is only a mobile control surface and must
    never interrupt discovery, analysis, drafting, sending, or verification."""
    if not settings.telegram_enabled or listing_db_id is None:
        return
    try:
        rendered = render_listing_card(database, listing_db_id)
        if rendered is None:
            return
        text, keyboard, status = rendered
        if status not in NOTIFIABLE_STATUSES:
            return
        previous = database.get_telegram_notification(listing_db_id)
        if previous is not None and str(previous["status"]) == status:
            return
        message_id = send_message(settings, settings.telegram_chat_id, text, keyboard)
        if message_id is not None:
            database.record_telegram_notification(
                listing_db_id, status, settings.telegram_chat_id, message_id
            )
    except Exception:
        logger.exception("Telegram notification failed; apartment processing continues")


DISCOVERY_FAILURE_NOTIFY_THRESHOLD = 3


def record_discovery_outcome(
    database: Database, settings: Settings, source_name: str, *, failed: bool, detail: str = ""
) -> None:
    """Track consecutive discovery failures per source; notify exactly once when they
    cross DISCOVERY_FAILURE_NOTIFY_THRESHOLD, and reset silently on the next success.
    Avoids noise for a single transient failure while still surfacing a genuinely
    stuck source. Never raises."""
    if not settings.telegram_enabled:
        return
    try:
        key = f"discovery_failures:{source_name}"
        if not failed:
            if database.get_telegram_state(key) not in (None, "0"):
                database.set_telegram_state(key, "0")
            return
        count = int(database.get_telegram_state(key) or "0") + 1
        database.set_telegram_state(key, str(count))
        if count == DISCOVERY_FAILURE_NOTIFY_THRESHOLD:
            send_message(
                settings,
                settings.telegram_chat_id,
                f"⚠️ {source_name} discovery has failed {count} times in a row.\n{detail[:300]}",
            )
    except Exception:
        logger.exception("Telegram failure notification failed; apartment processing continues")


def check_watcher_health(database: Database, settings: Settings) -> None:
    """Notify once when the WG-Gesucht watcher reports human verification is required
    (expired login/CAPTCHA), and once when it clears. Never raises."""
    if not settings.telegram_enabled:
        return
    try:
        summary = database.watcher_status_summary()
        human_verification = any(
            row["status"] == "human_verification_required" for row in summary["searches"]
        )
        key = "watcher_human_verification_notified"
        already_notified = database.get_telegram_state(key) == "1"
        if human_verification and not already_notified:
            send_message(
                settings,
                settings.telegram_chat_id,
                "🔒 WG-Gesucht session needs attention: run ./login.sh wg",
            )
            database.set_telegram_state(key, "1")
        elif not human_verification and already_notified:
            database.set_telegram_state(key, "0")
    except Exception:
        logger.exception(
            "Telegram watcher-health notification failed; apartment processing continues"
        )


def notify_daemon_event(
    database: Database, settings: Settings, kind: str, detail: str, *, cooldown_seconds: int = 3600
) -> None:
    """One-off actionable daemon events (e.g. watcher crashed/stopped unexpectedly),
    deduplicated per `kind` with a cooldown so a repeating condition does not spam.
    Never raises."""
    if not settings.telegram_enabled:
        return
    try:
        key = f"event_notified:{kind}"
        last = database.get_telegram_state(key)
        now = datetime.now(UTC)
        if last:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < cooldown_seconds:
                    return
            except ValueError:
                pass
        send_message(settings, settings.telegram_chat_id, f"⚠️ {kind}\n{detail[:300]}")
        database.set_telegram_state(key, now.isoformat())
    except Exception:
        logger.exception(
            "Telegram daemon-event notification failed; apartment processing continues"
        )
