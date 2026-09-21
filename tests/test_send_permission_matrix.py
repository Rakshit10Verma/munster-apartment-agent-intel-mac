from __future__ import annotations

from dataclasses import replace

import pytest

from app.contact import contact_listing
from app.database import Database
from tests.test_browser_dry_run import _wg_outcome, _write_wg_fixture

# ---- 1-4: the full DRY_RUN x AUTO_SEND x trigger permission matrix -----------------


@pytest.mark.parametrize(
    ("dry_run", "auto_send", "trigger", "expected"),
    [
        # 1. DRY_RUN=true, AUTO_SEND=true -> every trigger stays dry-run only
        (True, True, "auto", False),
        (True, True, "manual_cli", False),
        (True, True, "telegram", False),
        (True, True, "dashboard", False),
        # 2. DRY_RUN=true, AUTO_SEND=false -> every trigger stays dry-run only
        (True, False, "auto", False),
        (True, False, "manual_cli", False),
        (True, False, "telegram", False),
        (True, False, "dashboard", False),
        # 3. DRY_RUN=false, AUTO_SEND=true -> every trigger may send for real
        (False, True, "auto", True),
        (False, True, "manual_cli", True),
        (False, True, "telegram", True),
        (False, True, "dashboard", True),
        # 4. DRY_RUN=false, AUTO_SEND=false -> only explicit human triggers may send
        (False, False, "auto", False),
        (False, False, "manual_cli", True),
        (False, False, "telegram", True),
        (False, False, "dashboard", True),
    ],
)
def test_send_permission_matrix(settings, dry_run, auto_send, trigger, expected) -> None:
    permitted = replace(settings, dry_run=dry_run, auto_send=auto_send).send_permitted(trigger)
    assert permitted is expected


def test_dry_run_always_wins_regardless_of_auto_send_or_trigger(settings) -> None:
    always_dry = replace(settings, dry_run=True, auto_send=True)
    for trigger in ("auto", "manual_cli", "telegram", "dashboard"):
        assert always_dry.send_permitted(trigger) is False


def test_auto_send_false_blocks_only_the_autonomous_trigger(settings) -> None:
    """AUTO_SEND=false means 'no autonomous daemon send', not 'disable explicit
    human-approved sending' -- confirmed against the real Settings.send_permitted."""
    live = replace(settings, dry_run=False, auto_send=False)
    assert live.send_permitted("auto") is False
    assert live.send_permitted("manual_cli") is True
    assert live.send_permitted("telegram") is True
    assert live.send_permitted("dashboard") is True


# ---- integration: the real contact_listing pipeline honors the same matrix ---------


def _permitted_outcome(settings, tmp_path, *, dry_run: bool, auto_send: bool, trigger: str):
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html)
    outcome = _wg_outcome(html.as_uri())
    database = Database(settings.database_path)
    outcome.database_id, _ = database.discover(outcome.listing)
    outcome.database_id = database.save_outcome(outcome)
    actual_settings = replace(settings, dry_run=dry_run, auto_send=auto_send, browser_headless=True)
    result = contact_listing(outcome, actual_settings, database, trigger)
    return result, database, outcome.database_id


def test_auto_trigger_does_not_send_when_auto_send_false_but_dry_run_false(
    settings, tmp_path
) -> None:
    result, _database, _id = _permitted_outcome(
        settings, tmp_path, dry_run=False, auto_send=False, trigger="auto"
    )
    assert result.status == "dry_run_ready"


def test_manual_cli_trigger_sends_when_auto_send_false_but_dry_run_false(
    settings, tmp_path
) -> None:
    result, _database, _id = _permitted_outcome(
        settings, tmp_path, dry_run=False, auto_send=False, trigger="manual_cli"
    )
    assert result.status == "sent"


def test_telegram_trigger_sends_when_auto_send_false_but_dry_run_false(settings, tmp_path) -> None:
    result, _database, _id = _permitted_outcome(
        settings, tmp_path, dry_run=False, auto_send=False, trigger="telegram"
    )
    assert result.status == "sent"


def test_auto_trigger_sends_when_auto_send_true_and_dry_run_false(settings, tmp_path) -> None:
    result, _database, _id = _permitted_outcome(
        settings, tmp_path, dry_run=False, auto_send=True, trigger="auto"
    )
    assert result.status == "sent"


def test_manual_cli_trigger_stays_dry_run_when_dry_run_true(settings, tmp_path) -> None:
    result, _database, _id = _permitted_outcome(
        settings, tmp_path, dry_run=True, auto_send=True, trigger="manual_cli"
    )
    assert result.status == "dry_run_ready"


def test_telegram_trigger_stays_dry_run_when_dry_run_true(settings, tmp_path) -> None:
    result, _database, _id = _permitted_outcome(
        settings, tmp_path, dry_run=True, auto_send=False, trigger="telegram"
    )
    assert result.status == "dry_run_ready"


# ---- explicit approval never bypasses existing safety gates ------------------------


def test_explicit_approval_cannot_double_send_after_duplicate_claim(settings, tmp_path) -> None:
    """A trigger being permitted to send does not bypass the one-actual-contact-per-
    listing SQLite constraint: a second explicit-approval attempt on an already
    claimed listing must still be refused, exactly like the autonomous path."""
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html)
    outcome = _wg_outcome(html.as_uri())
    database = Database(settings.database_path)
    outcome.database_id, _ = database.discover(outcome.listing)
    outcome.database_id = database.save_outcome(outcome)
    assert database.claim_actual_contact(outcome.database_id, "platform")

    result = contact_listing(
        outcome,
        replace(settings, dry_run=False, auto_send=False, browser_headless=True),
        database,
        "manual_cli",
    )

    assert result.status == "already_contacted"
    assert "duplicate" in result.detail


def test_explicit_telegram_approval_cannot_resend_an_already_sent_listing(
    settings, tmp_path
) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html)
    outcome = _wg_outcome(html.as_uri())
    database = Database(settings.database_path)
    outcome.database_id, _ = database.discover(outcome.listing)
    outcome.database_id = database.save_outcome(outcome)
    live_settings = replace(settings, dry_run=False, auto_send=False, browser_headless=True)

    first = contact_listing(outcome, live_settings, database, "telegram")
    assert first.status == "sent"

    second = contact_listing(outcome, live_settings, database, "telegram")
    assert second.status == "already_contacted"


# ---- send verification is identical regardless of who triggered it -----------------


def test_send_verification_path_is_identical_for_auto_and_manual_triggers(
    settings, tmp_path
) -> None:
    """Two separately-discovered listings, one sent via the autonomous trigger and one
    via explicit manual approval, must reach the same ambiguous-send outcome through
    the same verification code -- the trigger only decides permission, never how
    verification itself behaves."""
    from tests.test_sent_state_verification import _write_wg_fixture_ambiguous_send

    results = {}
    for trigger in ("auto", "manual_cli"):
        html = tmp_path / f"room_{trigger}.1234567.html"
        _write_wg_fixture_ambiguous_send(html)
        outcome = _wg_outcome(html.as_uri())
        # Separate databases per trigger: _wg_outcome's listing_id is fixed, so
        # reusing one database would dedup the second iteration onto the first
        # listing's row instead of exercising a fresh send for each trigger.
        database = Database(tmp_path / f"{trigger}.sqlite")
        outcome.database_id, _ = database.discover(outcome.listing)
        outcome.database_id = database.save_outcome(outcome)
        result = contact_listing(
            outcome,
            replace(
                settings,
                dry_run=False,
                auto_send=True,
                browser_headless=True,
                browser_timeout_seconds=1,
            ),
            database,
            trigger,
        )
        results[trigger] = result

    assert results["auto"].status == results["manual_cli"].status == "send_state_unknown"
    assert "reconcile_send.sh" in results["auto"].detail
    assert "reconcile_send.sh" in results["manual_cli"].detail
