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
                """
            )

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
                    message_body=?, attachment_json=?, validation_json=?, provider=?, model=?,
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
            return False

    def record_contact(
        self,
        listing_db_id: int,
        mode: str,
        channel: str,
        status: str,
        external_message_id: str | None = None,
        detail: str = "",
    ) -> None:
        now = utc_iso()
        with self.connect() as connection:
            if mode == "actual":
                changed = connection.execute(
                    """
                    UPDATE contact_attempts SET status=?, external_message_id=?, detail=?
                    WHERE listing_db_id=? AND mode='actual'
                    """,
                    (status, external_message_id, detail, listing_db_id),
                ).rowcount
                if not changed:
                    connection.execute(
                        """
                        INSERT INTO contact_attempts
                            (listing_db_id, mode, channel, status,
                             external_message_id, detail, created_at)
                        VALUES (?, 'actual', ?, ?, ?, ?, ?)
                        """,
                        (listing_db_id, channel, status, external_message_id, detail, now),
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO contact_attempts
                        (listing_db_id, mode, channel, status,
                         external_message_id, detail, created_at)
                    VALUES (?, 'dry_run', ?, ?, ?, ?, ?)
                    """,
                    (listing_db_id, channel, status, external_message_id, detail, now),
                )
            connection.execute(
                "UPDATE listings SET status=?, updated_at=? WHERE id=?",
                (status, now, listing_db_id),
            )

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
                SELECT id, platform, listing_id, title, status, provider, model, updated_at, error
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
