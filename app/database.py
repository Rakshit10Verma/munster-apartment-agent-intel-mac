from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .schemas import AnalysisOutcome, SourceListing

# Listing statuses that ./reset_run.sh must never clear, regardless of how old they
# are: each one either represents a real send, an unresolved send attempt, or a
# considered human decision. Deliberately NOT included: `not_sent` -- a reconciliation
# that positively established nothing was sent is not a duplicate-send risk and may be
# safely reconsidered (see Database._preserve_clause for the full rule, including the
# contact_attempts-based crash-recovery case).
PRESERVED_LISTING_STATUSES: tuple[str, ...] = (
    "sent",
    "already_contacted",
    "send_state_unknown",
    "manually_rejected",
)

# contact_attempts.status values that mean Send was actually clicked (or the process
# crashed immediately after a real click, before the outcome could be determined --
# see record_send_clicked's comment in browser.py). A listing must be preserved if ANY
# contact attempt on record shows one of these, regardless of the listing's own
# (possibly stale, in the crash case) status. Deliberately does NOT include every
# 'actual'-mode row: contact.py's record_contact() logs an 'actual' row for every
# actual-mode attempt unconditionally, including ones that failed before Send was ever
# reached (e.g. premium_boost_failed, bewerbermappe_attachment_failed, a final-gate
# review_required) -- those are not a duplicate-send risk and may be reconsidered.
SEND_CLICKED_CONTACT_STATUSES: tuple[str, ...] = ("sent", "send_state_unknown", "send_clicked")


def has_send_evidence(attempt: dict[str, Any] | None) -> bool:
    """Fail closed on any evidence that Send may have been clicked."""
    if not attempt:
        return False
    return bool(
        attempt.get("status") in SEND_CLICKED_CONTACT_STATUSES
        or any(
            attempt.get(field)
            for field in (
                "send_clicked_at",
                "message_fingerprint",
                "message_body",
                "confirmed_at",
                "external_message_id",
            )
        )
    )


def utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def canonical_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_") and key.casefold() not in {"ref", "source"}
        )
    )
    return urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), query, "")
    )


def listing_key(listing: SourceListing) -> str:
    identity = listing.listing_id.strip() or canonical_url(listing.url)
    if not identity:
        identity = hashlib.sha256(listing.raw_text.encode()).hexdigest()
    return hashlib.sha256(f"{listing.platform}:{identity}".encode()).hexdigest()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS listings (
                    id INTEGER PRIMARY KEY,
                    dedup_key TEXT NOT NULL UNIQUE,
                    platform TEXT NOT NULL,
                    listing_id TEXT NOT NULL DEFAULT '',
                    canonical_url TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    raw_text TEXT NOT NULL,
                    contact_email TEXT,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    facts_json TEXT,
                    decision_json TEXT,
                    message_subject TEXT,
                    message_body TEXT,
                    message_source TEXT NOT NULL DEFAULT 'none',
                    generation_trace_json TEXT,
                    attachment_json TEXT,
                    validation_json TEXT,
                    provider TEXT,
                    model TEXT,
                    latency_ms INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
                CREATE TABLE IF NOT EXISTS contact_attempts (
                    id INTEGER PRIMARY KEY,
                    listing_db_id INTEGER NOT NULL REFERENCES listings(id),
                    mode TEXT NOT NULL CHECK(mode IN ('dry_run', 'actual')),
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    external_message_id TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_actual_contact_per_listing
                    ON contact_attempts(listing_db_id) WHERE mode = 'actual';
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    listing_db_id INTEGER REFERENCES listings(id),
                    event TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watcher_seen_listings (
                    id INTEGER PRIMARY KEY,
                    listing_key TEXT NOT NULL UNIQUE,
                    search_name TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    is_baseline INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS watcher_search_state (
                    search_name TEXT PRIMARY KEY,
                    baseline_done INTEGER NOT NULL DEFAULT 0,
                    last_checked_at TEXT,
                    last_success_at TEXT,
                    last_new_listing_at TEXT,
                    next_check_at TEXT,
                    status TEXT NOT NULL DEFAULT 'ok',
                    consecutive_errors INTEGER NOT NULL DEFAULT 0,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS telegram_notifications (
                    listing_db_id INTEGER PRIMARY KEY REFERENCES listings(id),
                    status TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_pending_sends (
                    listing_db_id INTEGER PRIMARY KEY REFERENCES listings(id),
                    chat_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(listings)").fetchall()
            }
            if "message_source" not in columns:
                connection.execute(
                    "ALTER TABLE listings ADD COLUMN message_source TEXT NOT NULL DEFAULT 'none'"
                )
            if "generation_trace_json" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN generation_trace_json TEXT")
            # Router/fallback notes (e.g. exactly which fallback safety condition
            # blocked generation) were previously computed but never persisted, so a
            # review_required row only ever showed the raw provider exception text.
            if "router_notes_json" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN router_notes_json TEXT")
            # Persisted so ./review.sh can show which hidden questions/commands a stored
            # draft actually resolved, without re-running AI/analysis.
            for column in ("message_answered_question_ids", "message_applied_command_ids"):
                if column not in columns:
                    connection.execute(f"ALTER TABLE listings ADD COLUMN {column} TEXT")
            contact_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(contact_attempts)").fetchall()
            }
            # Persisted immediately after a Send click, before waiting for verification,
            # so a browser/process crash mid-verification never loses the record of what
            # was attempted. See record_send_clicked/reconcile_contact.
            for column in (
                "message_fingerprint",
                "message_body",
                "send_clicked_at",
                "confirmed_at",
                "premium_state",
                "attachment_state",
                "browser_url",
            ):
                if column not in contact_columns:
                    connection.execute(f"ALTER TABLE contact_attempts ADD COLUMN {column} TEXT")
            # Who initiated this contact attempt (auto/manual_cli/telegram/dashboard,
            # see SendTrigger) -- observability only, never read for any safety
            # decision. Blank for rows written before this column existed.
            if "trigger" not in contact_columns:
                connection.execute("ALTER TABLE contact_attempts ADD COLUMN trigger TEXT")

    def discover(self, listing: SourceListing) -> tuple[int, bool]:
        key = listing_key(listing)
        now = utc_iso()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM listings WHERE dedup_key = ?", (key,)
            ).fetchone()
            if existing:
                return int(existing["id"]), False
            cursor = connection.execute(
                """
                INSERT INTO listings (
                    dedup_key, platform, listing_id, canonical_url, title, raw_text,
                    contact_email, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    listing.platform,
                    listing.listing_id,
                    canonical_url(listing.url),
                    listing.title,
                    listing.raw_text,
                    listing.contact_email,
                    listing.discovered_at.isoformat(),
                    now,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a listing id")
            listing_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO events(listing_db_id, event, created_at) VALUES (?, 'discovered', ?)",
                (listing_id, now),
            )
            return listing_id, True

    def contains(self, listing: SourceListing) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM listings WHERE dedup_key = ?", (listing_key(listing),)
            ).fetchone()
        return row is not None

    def save_outcome(self, outcome: AnalysisOutcome, error: str | None = None) -> int:
        listing_id, _ = self.discover(outcome.listing)
        provider = outcome.provider
        message = outcome.message
        now = utc_iso()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE listings SET
                    updated_at=?, status=?, facts_json=?, decision_json=?, message_subject=?,
                    message_body=?, message_source=?, message_answered_question_ids=?,
                    message_applied_command_ids=?, generation_trace_json=?, router_notes_json=?,
                    attachment_json=?,
                    validation_json=?,
                    provider=?, model=?,
                    latency_ms=?, input_tokens=?, output_tokens=?, error=?
                WHERE id=?
                """,
                (
                    now,
                    outcome.status,
                    outcome.facts.model_dump_json(),
                    outcome.rule_decision.model_dump_json(),
                    message.subject if message else None,
                    message.body if message else None,
                    outcome.message_source,
                    json.dumps(message.answered_question_ids) if message else None,
                    json.dumps(message.applied_command_ids) if message else None,
                    outcome.generation_trace.model_dump_json(),
                    json.dumps(outcome.router_notes, ensure_ascii=False),
                    outcome.attachment.model_dump_json(),
                    outcome.validation.model_dump_json(),
                    provider.provider if provider else None,
                    provider.model if provider else None,
                    provider.latency_ms if provider else None,
                    provider.input_tokens if provider else None,
                    provider.output_tokens if provider else None,
                    error,
                    listing_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO events(listing_db_id, event, detail_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    listing_id,
                    outcome.status,
                    json.dumps({"errors": outcome.validation.errors}, ensure_ascii=False),
                    now,
                ),
            )
        return listing_id

    def claim_actual_contact(self, listing_db_id: int, channel: str) -> bool:
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO contact_attempts
                        (listing_db_id, mode, channel, status, created_at)
                    VALUES (?, 'actual', ?, 'claimed', ?)
                    """,
                    (listing_db_id, channel, utc_iso()),
                )
            return True
        except sqlite3.IntegrityError:
            # A prior attachment failure happened BEFORE Send. The unique actual
            # attempt remains as an audit record, so atomically reclaim only that
            # exact pre-Send state. A concurrent retry changes status to claimed;
            # any send evidence, unknown state, or changed listing then blocks us.
            with self.connect() as connection:
                prior = connection.execute(
                    "SELECT * FROM contact_attempts WHERE listing_db_id=? AND mode='actual'",
                    (listing_db_id,),
                ).fetchone()
                if prior is None or has_send_evidence(dict(prior)):
                    return False
                changed = connection.execute(
                    """
                    UPDATE contact_attempts SET status='claimed'
                    WHERE listing_db_id=? AND mode='actual'
                      AND status='bewerbermappe_attachment_failed'
                      AND send_clicked_at IS NULL AND message_fingerprint IS NULL
                      AND message_body IS NULL AND confirmed_at IS NULL
                      AND external_message_id IS NULL
                      AND EXISTS (
                        SELECT 1 FROM listings WHERE id=?
                        AND status='bewerbermappe_attachment_failed'
                      )
                    """,
                    (listing_db_id, listing_db_id),
                ).rowcount
                if changed:
                    connection.execute(
                        "INSERT INTO events(listing_db_id,event,detail_json,created_at) "
                        "VALUES (?,'attachment_retry_claimed','{}',?)",
                        (listing_db_id, utc_iso()),
                    )
                return bool(changed)

    def record_send_clicked(
        self,
        listing_db_id: int,
        message_fingerprint: str,
        message_body: str,
        premium_state: str,
        attachment_state: str,
        browser_url: str,
    ) -> None:
        """Persist exactly what we attempted to send immediately after the Send click,
        before waiting for verification. Protects against browser/process crashes,
        timeouts, and network interruption during the verification wait: even if the
        process dies right after this call, the attempt is not lost and a later
        `reconcile_contact` can recover the correct final state without resending."""
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE contact_attempts
                SET status='send_clicked', message_fingerprint=?, message_body=?,
                    send_clicked_at=?, premium_state=?, attachment_state=?, browser_url=?
                WHERE listing_db_id=? AND mode='actual'
                """,
                (
                    message_fingerprint,
                    message_body,
                    utc_iso(),
                    premium_state,
                    attachment_state,
                    browser_url,
                    listing_db_id,
                ),
            )

    def get_actual_contact_attempt(self, listing_db_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM contact_attempts WHERE listing_db_id=? AND mode='actual'",
                (listing_db_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_listing_by_url_or_id(self, url: str, listing_id: str = "") -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM listings
                WHERE canonical_url=? OR (listing_id != '' AND listing_id=?)
                ORDER BY updated_at DESC LIMIT 1
                """,
                (canonical_url(url), listing_id),
            ).fetchone()
        return dict(row) if row else None

    def get_listing(self, listing_db_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM listings WHERE id=?", (listing_db_id,)
            ).fetchone()
        return dict(row) if row else None

    def listings_by_status(self, status: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM listings WHERE status=? ORDER BY updated_at DESC", (status,)
            ).fetchall()
        return [dict(row) for row in rows]

    def listings_for_dashboard(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            if status and status != "all":
                rows = connection.execute(
                    "SELECT * FROM listings WHERE status=? ORDER BY updated_at DESC", (status,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM listings ORDER BY updated_at DESC"
                ).fetchall()
        return [dict(row) for row in rows]

    def listing_history(self, listing_db_id: int) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as connection:
            events = connection.execute(
                "SELECT * FROM events WHERE listing_db_id=? ORDER BY created_at DESC, id DESC",
                (listing_db_id,),
            ).fetchall()
            attempts = connection.execute(
                "SELECT * FROM contact_attempts WHERE listing_db_id=? "
                "ORDER BY created_at DESC, id DESC",
                (listing_db_id,),
            ).fetchall()
        return {
            "events": [dict(row) for row in events],
            "contact_attempts": [dict(row) for row in attempts],
        }

    def mark_manually_rejected(self, listing_db_id: int) -> None:
        """Record a human's explicit rejection. This never needs to guard against the
        listing reappearing in search results: the permanent `dedup_key` row created by
        `discover()` already makes any future `database.contains()` check (used by both
        the daemon's discovery loop and the search watcher's own claim table) return
        True regardless of status, so a rejected listing is never reprocessed."""
        now = utc_iso()
        with self.connect() as connection:
            connection.execute(
                "UPDATE listings SET status='manually_rejected', updated_at=? WHERE id=?",
                (now, listing_db_id),
            )
            connection.execute(
                "INSERT INTO events(listing_db_id, event, created_at) "
                "VALUES (?, 'manually_rejected', ?)",
                (listing_db_id, now),
            )

    def reconcile_contact(self, listing_db_id: int, status: str, detail: str) -> None:
        """Update a listing's send state from a read-only reconciliation pass. Never
        called from a Send-click path; used only to recover a stalled/ambiguous
        `send_state_unknown` record after inspecting the live conversation."""
        now = utc_iso()
        with self.connect() as connection:
            changed = connection.execute(
                """
                UPDATE contact_attempts SET status=?, detail=?, confirmed_at=?
                WHERE listing_db_id=? AND mode='actual'
                """,
                (status, detail, now, listing_db_id),
            ).rowcount
            if not changed:
                connection.execute(
                    """
                    INSERT INTO contact_attempts
                        (listing_db_id, mode, channel, status, detail, confirmed_at, created_at)
                    VALUES (?, 'actual', 'platform', ?, ?, ?, ?)
                    """,
                    (listing_db_id, status, detail, now, now),
                )
            connection.execute(
                "UPDATE listings SET status=?, updated_at=? WHERE id=?",
                (status, now, listing_db_id),
            )

    def record_contact(
        self,
        listing_db_id: int,
        mode: str,
        channel: str,
        status: str,
        external_message_id: str | None = None,
        detail: str = "",
        trigger: str = "",
    ) -> None:
        now = utc_iso()
        with self.connect() as connection:
            if mode == "actual":
                prior = connection.execute(
                    "SELECT * FROM contact_attempts WHERE listing_db_id=? AND mode='actual'",
                    (listing_db_id,),
                ).fetchone()
                if (
                    prior is not None
                    and has_send_evidence(dict(prior))
                    and status not in {"sent", "send_state_unknown"}
                ):
                    # A later pre-Send failure/duplicate check must never erase a
                    # previously clicked or confirmed send record.
                    return
                if (
                    prior is not None
                    and prior["status"] == "claimed"
                    and status
                    in {
                        "already_contacted",
                        "bewerbermappe_attachment_failed",
                        "premium_boost_failed",
                        "review_required",
                        "dry_run_ready",
                    }
                ):
                    # Another concurrent invocation may own the final SQLite
                    # claim. Its in-flight state must not be overwritten by a
                    # competing invocation that never reached Send.
                    return
                changed = connection.execute(
                    """
                    UPDATE contact_attempts SET status=?, external_message_id=?, detail=?, trigger=?
                    WHERE listing_db_id=? AND mode='actual'
                    """,
                    (status, external_message_id, detail, trigger, listing_db_id),
                ).rowcount
                if not changed:
                    connection.execute(
                        """
                        INSERT INTO contact_attempts
                            (listing_db_id, mode, channel, status,
                             external_message_id, detail, trigger, created_at)
                        VALUES (?, 'actual', ?, ?, ?, ?, ?, ?)
                        """,
                        (listing_db_id, channel, status, external_message_id, detail, trigger, now),
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO contact_attempts
                        (listing_db_id, mode, channel, status,
                         external_message_id, detail, trigger, created_at)
                    VALUES (?, 'dry_run', ?, ?, ?, ?, ?, ?)
                    """,
                    (listing_db_id, channel, status, external_message_id, detail, trigger, now),
                )
            connection.execute(
                "UPDATE listings SET status=?, updated_at=? WHERE id=?",
                (status, now, listing_db_id),
            )
            connection.execute(
                "INSERT INTO events(listing_db_id,event,detail_json,created_at) VALUES (?,?,?,?)",
                (
                    listing_db_id,
                    status,
                    json.dumps({"trigger": trigger, "mode": mode, "detail": detail[:1000]}),
                    now,
                ),
            )

    def watcher_claim_seen(
        self, listing_id: str, search_name: str, *, platform: str = "wg_gesucht"
    ) -> bool:
        """Atomically claim a listing as seen by the watcher. Returns True only the
        first time this listing is ever claimed (by any search), so the same listing
        appearing in multiple configured searches, or reappearing after a refresh or
        reorder, is enqueued at most once. The UNIQUE constraint on listing_key makes
        this race-safe across concurrent callers."""
        key = f"{platform}:{listing_id}"
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO watcher_seen_listings
                        (listing_key, search_name, first_seen_at, is_baseline)
                    VALUES (?, ?, ?, 0)
                    """,
                    (key, search_name, utc_iso()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def watcher_mark_baseline(
        self, search_name: str, listing_ids: list[str], *, platform: str = "wg_gesucht"
    ) -> int:
        """Record the listings visible on a search's first-ever check as baseline: seen,
        but never enqueued. Idempotent and race-safe; a listing already claimed (by
        this or another search) is left alone. Also marks the search's baseline as
        done so subsequent checks only enqueue genuinely new listings."""
        now = utc_iso()
        inserted = 0
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO watcher_search_state (search_name, last_checked_at, status)
                VALUES (?, ?, 'ok')
                """,
                (search_name, now),
            )
            for listing_id in listing_ids:
                key = f"{platform}:{listing_id}"
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO watcher_seen_listings
                        (listing_key, search_name, first_seen_at, is_baseline)
                    VALUES (?, ?, ?, 1)
                    """,
                    (key, search_name, now),
                )
                inserted += cursor.rowcount
            connection.execute(
                "UPDATE watcher_search_state SET baseline_done=1, last_checked_at=? "
                "WHERE search_name=?",
                (now, search_name),
            )
        return inserted

    def watcher_get_search_state(self, search_name: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM watcher_search_state WHERE search_name=?", (search_name,)
            ).fetchone()
        return dict(row) if row else None

    def watcher_record_check(
        self,
        search_name: str,
        *,
        status: str,
        detail: str = "",
        new_listing: bool = False,
        next_check_at: str | None = None,
    ) -> int:
        """Persist the outcome of one watcher check for a search, independent of the
        running daemon process, so `status.sh`/`watch_once.sh` can report it. Tracks
        consecutive errors (reset to 0 on `status='ok'`) so callers can compute
        backoff. Returns the resulting consecutive-error count."""
        now = utc_iso()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT consecutive_errors, last_success_at, last_new_listing_at "
                "FROM watcher_search_state WHERE search_name=?",
                (search_name,),
            ).fetchone()
            consecutive_errors = (
                0 if status == "ok" else (int(row["consecutive_errors"]) + 1 if row else 1)
            )
            last_success_at = row["last_success_at"] if row else None
            last_new_listing_at = row["last_new_listing_at"] if row else None
            if status == "ok":
                last_success_at = now
            if new_listing:
                last_new_listing_at = now
            if row is None:
                connection.execute(
                    """
                    INSERT INTO watcher_search_state
                        (search_name, baseline_done, last_checked_at, last_success_at,
                         last_new_listing_at, next_check_at, status, consecutive_errors, detail)
                    VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        search_name,
                        now,
                        last_success_at,
                        last_new_listing_at,
                        next_check_at,
                        status,
                        consecutive_errors,
                        detail,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE watcher_search_state
                    SET last_checked_at=?, last_success_at=?, last_new_listing_at=?,
                        next_check_at=?, status=?, consecutive_errors=?, detail=?
                    WHERE search_name=?
                    """,
                    (
                        now,
                        last_success_at,
                        last_new_listing_at,
                        next_check_at,
                        status,
                        consecutive_errors,
                        detail,
                        search_name,
                    ),
                )
        return consecutive_errors

    def watcher_status_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            searches = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM watcher_search_state ORDER BY search_name"
                ).fetchall()
            ]
            today = datetime.now(UTC).date().isoformat()
            discovered_today = connection.execute(
                "SELECT COUNT(*) FROM watcher_seen_listings "
                "WHERE is_baseline=0 AND first_seen_at >= ?",
                (today,),
            ).fetchone()[0]
            queue_depth = connection.execute(
                "SELECT COUNT(*) FROM listings WHERE status='discovered'"
            ).fetchone()[0]
        return {
            "searches": searches,
            "discovered_today": int(discovered_today),
            "queue_depth": int(queue_depth),
        }

    def status_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM listings GROUP BY status ORDER BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, platform, listing_id, title, status, message_source, provider, model,
                       updated_at, error
                FROM listings ORDER BY updated_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def export_csv(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM listings ORDER BY discovered_at").fetchall()
        if not rows:
            path.write_text("", encoding="utf-8")
            return 0
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)
        return len(rows)

    @staticmethod
    def _preserve_clause() -> str:
        """A listing is preserved when its own status says so, OR when any contact
        attempt on record shows Send was actually clicked (or the process crashed
        immediately after a real click -- the 'send_clicked' intermediate status).

        Deliberately NOT based on contact_attempts.mode='actual' existing at all:
        contact.py's record_contact() logs an 'actual' row for every actual-mode
        attempt unconditionally, including ones that returned before Send was ever
        clicked (e.g. premium_boost_failed, bewerbermappe_attachment_failed, a
        final-gate review_required) -- those are not a duplicate-send risk.
        """
        status_placeholders = ",".join("?" * len(PRESERVED_LISTING_STATUSES))
        click_placeholders = ",".join("?" * len(SEND_CLICKED_CONTACT_STATUSES))
        return (
            f"status IN ({status_placeholders}) OR EXISTS ("
            "SELECT 1 FROM contact_attempts "
            "WHERE contact_attempts.listing_db_id = listings.id "
            f"AND contact_attempts.status IN ({click_placeholders}))"
        )

    @staticmethod
    def _preserve_params() -> tuple[str, ...]:
        return PRESERVED_LISTING_STATUSES + SEND_CLICKED_CONTACT_STATUSES

    def reset_run_preview(self) -> dict[str, int]:
        """Read-only counts for the ./reset_run.sh confirmation prompt. Never
        modifies anything."""
        with self.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
            preserved = connection.execute(
                f"SELECT COUNT(*) FROM listings WHERE {self._preserve_clause()}",
                self._preserve_params(),
            ).fetchone()[0]
            send_state_unknown = connection.execute(
                "SELECT COUNT(*) FROM listings WHERE status='send_state_unknown'"
            ).fetchone()[0]
        return {
            "total": int(total),
            "preserved": int(preserved),
            "to_clear": int(total) - int(preserved),
            "send_state_unknown": int(send_state_unknown),
        }

    def status_counts_split(self) -> tuple[dict[str, int], dict[str, int]]:
        """Status counts split into (current_run, preserved_contact_history), using
        the same preservation rule as reset_run, so ./status.sh can show a clean
        current-run view without hiding safety-critical history."""
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT status, {self._preserve_clause()} AS preserved, COUNT(*) AS count
                FROM listings GROUP BY status, preserved ORDER BY status
                """,
                self._preserve_params(),
            ).fetchall()
        current: dict[str, int] = {}
        preserved: dict[str, int] = {}
        for row in rows:
            target = preserved if row["preserved"] else current
            target[str(row["status"])] = target.get(str(row["status"]), 0) + int(row["count"])
        return current, preserved

    def reset_run(self) -> dict[str, int]:
        """Clear non-safety-critical processing history (discovered/filtered_skip/
        review_required/drafted/ai_failed/premium_boost_failed/dry_run_ready/etc. and
        their draft messages, extraction records, and events) so status/review/
        dashboard start clean, while never touching a listing that is sent,
        already_contacted, send_state_unknown, a manually rejected decision, or has
        any contact attempt on record showing Send was actually clicked (including a
        process-crash-before-verification case). A reconciled `not_sent` listing is
        NOT preserved by status alone: reconciliation already positively established
        nothing was sent, so it is safe to clear and let normal listing rules decide
        if it resurfaces.

        Also resets the search watcher's own "already seen" bookkeeping so cleared
        listings can be freshly rediscovered; this is safe even for a listing that
        happens to still be live on WG-Gesucht, because `discover()`/`contains()`
        checking `listings.dedup_key` -- untouched here for every preserved listing
        -- is the actual, independent duplicate-contact safety net (see
        daemon.cycle()), not the watcher's bookkeeping.

        Transactional: this runs inside one `connect()` block, so if anything raises,
        nothing is committed (see `connect`'s rollback-on-exception behavior).
        """
        with self.connect() as connection:
            to_delete = connection.execute(
                f"SELECT id FROM listings WHERE NOT ({self._preserve_clause()})",
                self._preserve_params(),
            ).fetchall()
            ids = [int(row["id"]) for row in to_delete]
            cleared = len(ids)
            if ids:
                placeholders = ",".join("?" * len(ids))
                # Every contact_attempts row for a cleared listing -- dry_run rows,
                # and any 'actual'-mode row too (e.g. a premium_boost_failed attempt
                # that never reached Send) -- is safe to remove: the WHERE clause
                # above already guarantees none of these ids has a row showing Send
                # was actually clicked.
                connection.execute(
                    f"DELETE FROM contact_attempts WHERE listing_db_id IN ({placeholders})", ids
                )
                connection.execute(
                    f"DELETE FROM events WHERE listing_db_id IN ({placeholders})", ids
                )
                # Telegram notification/pending-confirmation rows for a cleared listing
                # are FK-referenced against listings(id); they must go before the
                # listings row itself, and clearing them is exactly correct -- a fresh
                # run should be able to notify again if the listing resurfaces.
                connection.execute(
                    f"DELETE FROM telegram_notifications WHERE listing_db_id IN ({placeholders})",
                    ids,
                )
                connection.execute(
                    f"DELETE FROM telegram_pending_sends WHERE listing_db_id IN ({placeholders})",
                    ids,
                )
                connection.execute(f"DELETE FROM listings WHERE id IN ({placeholders})", ids)
            connection.execute("DELETE FROM watcher_seen_listings")
            connection.execute("DELETE FROM watcher_search_state")
            preserved = connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
            send_state_unknown = connection.execute(
                "SELECT COUNT(*) FROM listings WHERE status='send_state_unknown'"
            ).fetchone()[0]
        return {
            "cleared": cleared,
            "preserved": int(preserved),
            "send_state_unknown": int(send_state_unknown),
        }

    def reset_watcher_bookkeeping(self) -> None:
        """Clear ONLY the search watcher's own baseline/already-seen tracking. Never
        touches listings/contact_attempts/events. Used by ./baseline_watcher.sh to
        re-baseline independently of a full ./reset_run.sh."""
        with self.connect() as connection:
            connection.execute("DELETE FROM watcher_seen_listings")
            connection.execute("DELETE FROM watcher_search_state")

    # ---- Telegram notification/confirmation state -----------------------------------
    # Telegram is only a mobile control surface; SQLite remains the source of truth.
    # These tables never gate a send by themselves -- they only prevent duplicate
    # notifications and hold a short-lived two-step Send confirmation. Every action
    # taken from Telegram re-reads the live listing/contact_attempts state through the
    # normal review.py functions before doing anything real.

    def get_telegram_notification(self, listing_db_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM telegram_notifications WHERE listing_db_id=?", (listing_db_id,)
            ).fetchone()
            return dict(row) if row else None

    def record_telegram_notification(
        self, listing_db_id: int, status: str, chat_id: str, message_id: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO telegram_notifications
                    (listing_db_id, status, chat_id, message_id, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(listing_db_id) DO UPDATE SET
                    status=excluded.status,
                    chat_id=excluded.chat_id,
                    message_id=excluded.message_id,
                    updated_at=excluded.updated_at
                """,
                (listing_db_id, status, chat_id, message_id, utc_iso()),
            )

    def set_pending_telegram_send(self, listing_db_id: int, chat_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO telegram_pending_sends (listing_db_id, chat_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(listing_db_id) DO UPDATE SET
                    chat_id=excluded.chat_id, created_at=excluded.created_at
                """,
                (listing_db_id, chat_id, utc_iso()),
            )

    def pop_pending_telegram_send(self, listing_db_id: int) -> dict[str, Any] | None:
        """Consume (read-and-delete) a pending two-step Send confirmation, so a
        confirmation can only ever be acted on once."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM telegram_pending_sends WHERE listing_db_id=?", (listing_db_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "DELETE FROM telegram_pending_sends WHERE listing_db_id=?", (listing_db_id,)
            )
            return dict(row)

    def clear_pending_telegram_send(self, listing_db_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM telegram_pending_sends WHERE listing_db_id=?", (listing_db_id,)
            )

    def get_telegram_state(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM telegram_bot_state WHERE key=?", (key,)
            ).fetchone()
            return str(row["value"]) if row else None

    def set_telegram_state(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO telegram_bot_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    def backup_to(self, backups_dir: Path) -> Path:
        """Consistent timestamped copy of the live database, taken via SQLite's
        online backup API (safe under WAL mode, unlike a plain file copy which could
        miss data still sitting in the WAL file)."""
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
        target_path = backups_dir / f"pre_reset_{stamp}.sqlite"
        source = sqlite3.connect(self.path)
        try:
            target = sqlite3.connect(target_path)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        return target_path
