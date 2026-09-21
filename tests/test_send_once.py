from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from app.cli import _contact_exit_code
from app.contact import contact_listing
from app.database import Database
from tests.test_browser_dry_run import _wg_outcome, _write_wg_fixture

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "send_once.sh"


def test_send_once_refuses_when_auto_send_is_false() -> None:
    env = os.environ.copy()
    env["AUTO_SEND"] = "false"
    result = subprocess.run(
        [str(SCRIPT), "https://www.wg-gesucht.de/wg-zimmer.1234567.html"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "AUTO_SEND=true" in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "http://www.wg-gesucht.de/wg-zimmer.1234567.html",
        "https://www.wg-gesucht.de.evil.example/wg-zimmer.1234567.html",
        "https://example.com/wg-zimmer.1234567.html",
    ],
)
def test_send_once_rejects_non_https_wg_urls(url: str) -> None:
    env = os.environ.copy()
    env["AUTO_SEND"] = "true"
    result = subprocess.run(
        ["bash", str(SCRIPT), url],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 64
    assert "exactly one HTTPS WG-Gesucht URL" in result.stderr


def test_one_shot_success_requires_verified_sent_status() -> None:
    assert _contact_exit_code("sent", require_sent=True) == 0
    assert _contact_exit_code("dry_run_ready", require_sent=True) != 0
    assert _contact_exit_code("send_failed", require_sent=True) != 0


def test_one_shot_verified_send_is_logged_as_actual_contact(settings, tmp_path: Path) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html)
    outcome = _wg_outcome(html.as_uri())
    database = Database(settings.database_path)
    outcome.database_id, _ = database.discover(outcome.listing)
    outcome.database_id = database.save_outcome(outcome)

    result = contact_listing(
        outcome,
        replace(settings, dry_run=False, auto_send=True),
        database,
        "manual_cli",
    )

    assert result.status == "sent"
    with database.connect() as connection:
        contact = connection.execute(
            "SELECT mode, status FROM contact_attempts WHERE listing_db_id=?",
            (outcome.database_id,),
        ).fetchone()
        listing = connection.execute(
            "SELECT status FROM listings WHERE id=?", (outcome.database_id,)
        ).fetchone()
    assert dict(contact) == {"mode": "actual", "status": "sent"}
    assert listing["status"] == "sent"
