from __future__ import annotations

from app.daemon import ApartmentDaemon
from app.database import Database
from app.sources import SourceAdapter
from tests.conftest import viable_listing
from tests.test_wg_watcher import _write_search_fixture


def _sent_listing(database: Database, listing_id: str = "9000001") -> int:
    listing = viable_listing(listing_id=listing_id)
    listing_db_id, _ = database.discover(listing)
    assert database.claim_actual_contact(listing_db_id, "platform")
    database.record_send_clicked(
        listing_db_id, "fp1", "Hallo, ...", "premium_priority_verified", "attached", "https://x"
    )
    database.record_contact(listing_db_id, "actual", "platform", "sent")
    return listing_db_id


def _already_contacted_listing(database: Database, listing_id: str = "9000002") -> int:
    listing = viable_listing(listing_id=listing_id)
    listing_db_id, _ = database.discover(listing)
    with database.connect() as connection:
        connection.execute(
            "UPDATE listings SET status='already_contacted' WHERE id=?", (listing_db_id,)
        )
    return listing_db_id


def _send_state_unknown_listing(database: Database, listing_id: str = "9000003") -> int:
    listing_db_id = _sent_listing(database, listing_id)
    database.reconcile_contact(listing_db_id, "send_state_unknown", "ambiguous")
    return listing_db_id


def _processing_listing(database: Database, status: str, listing_id: str) -> int:
    listing = viable_listing(listing_id=listing_id)
    listing_db_id, _ = database.discover(listing)
    with database.connect() as connection:
        connection.execute("UPDATE listings SET status=? WHERE id=?", (status, listing_db_id))
    return listing_db_id


def watcher_baseline_ids(database: Database, search_name: str) -> set[str]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT listing_key FROM watcher_seen_listings WHERE search_name=? AND is_baseline=1",
            (search_name,),
        ).fetchall()
    return {str(row["listing_key"]).split(":", 1)[1] for row in rows}


# ---- what gets preserved -----------------------------------------------------------


def test_sent_listing_survives_reset(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _sent_listing(database)

    database.reset_run()

    row = database.get_listing(listing_db_id)
    assert row is not None
    assert row["status"] == "sent"
    assert database.get_actual_contact_attempt(listing_db_id) is not None


def test_already_contacted_survives_reset(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _already_contacted_listing(database)

    database.reset_run()

    row = database.get_listing(listing_db_id)
    assert row is not None
    assert row["status"] == "already_contacted"


def test_send_state_unknown_survives_reset(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _send_state_unknown_listing(database)

    database.reset_run()

    row = database.get_listing(listing_db_id)
    assert row is not None
    assert row["status"] == "send_state_unknown"
    assert database.get_actual_contact_attempt(listing_db_id) is not None


def test_manually_rejected_survives_reset(settings) -> None:
    database = Database(settings.database_path)
    listing = viable_listing(listing_id="9000004")
    listing_db_id, _ = database.discover(listing)
    database.mark_manually_rejected(listing_db_id)

    database.reset_run()

    row = database.get_listing(listing_db_id)
    assert row is not None
    assert row["status"] == "manually_rejected"


def test_not_sent_reconciliation_is_cleared(settings) -> None:
    """Case D: reconciliation positively established nothing was sent. This is not a
    duplicate-send risk, so it is cleared and may be safely reconsidered on the next
    run, subject to normal listing rules."""
    database = Database(settings.database_path)
    listing_db_id = _sent_listing(database, "9000005")
    database.reconcile_contact(listing_db_id, "not_sent", "no application found")

    database.reset_run()

    assert database.get_listing(listing_db_id) is None


def test_actual_attempt_without_a_real_send_click_is_cleared(settings) -> None:
    """Case C: the real browser/contact flow started (an 'actual'-mode row was
    logged, e.g. via a Premium-strict-mode failure before the composer was ever
    reached) but Send was NEVER clicked. This is the exact regression for listing
    14021501: an 'actual'-mode row alone must never be treated as proof of a possible
    send -- only send_clicked_at / a send/send_state_unknown/send_clicked status is."""
    database = Database(settings.database_path)
    listing = viable_listing(listing_id="9000006")
    listing_db_id, _ = database.discover(listing)
    # Mirrors contact.py's contact_listing(): record_contact() logs an 'actual' row
    # unconditionally, even though claim_actual_contact()/send.click() were never
    # reached (see browser.py's premium-strict-mode early return, which happens
    # before the composer, let alone Send, is ever touched).
    database.record_contact(listing_db_id, "actual", "platform", "premium_boost_failed")
    attempt = database.get_actual_contact_attempt(listing_db_id)
    assert attempt is not None
    assert attempt["send_clicked_at"] is None

    database.reset_run()

    assert database.get_listing(listing_db_id) is None


def test_send_failed_before_any_claim_is_cleared(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _processing_listing(database, "send_failed", "9000007")

    database.reset_run()

    assert database.get_listing(listing_db_id) is None


def test_crash_after_send_clicked_is_preserved_even_with_stale_listing_status(
    settings,
) -> None:
    """Case B (crash variant): Send was actually clicked and record_send_clicked()
    persisted that immediately, but the process crashed before verification could
    determine sent/send_state_unknown, so contact_attempts.status is stuck at the
    intermediate 'send_clicked' value and listings.status was NEVER updated (still
    whatever it was before this attempt, e.g. 'drafted'). This must still be
    preserved -- the stale listings.status must never be trusted on its own."""
    database = Database(settings.database_path)
    listing = viable_listing(listing_id="9000008")
    listing_db_id, _ = database.discover(listing)
    with database.connect() as connection:
        connection.execute("UPDATE listings SET status='drafted' WHERE id=?", (listing_db_id,))
    assert database.claim_actual_contact(listing_db_id, "platform")
    database.record_send_clicked(
        listing_db_id,
        "fp-crash",
        "Hallo, ...",
        "premium_priority_verified",
        "attached",
        "https://x",
    )
    # No further record_contact()/reconcile_contact() call: simulates the process
    # dying during verify_message_sent()'s polling, exactly per browser.py's own
    # comment on why record_send_clicked persists immediately.
    attempt = database.get_actual_contact_attempt(listing_db_id)
    assert attempt is not None
    assert attempt["status"] == "send_clicked"

    database.reset_run()

    row = database.get_listing(listing_db_id)
    assert row is not None
    assert row["status"] == "drafted"  # still stale, but the row itself survived


# ---- what gets cleared ---------------------------------------------------------------


def test_review_required_is_cleared(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _processing_listing(database, "review_required", "9000010")

    result = database.reset_run()

    assert database.get_listing(listing_db_id) is None
    assert result["cleared"] >= 1


def test_drafted_is_cleared(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _processing_listing(database, "drafted", "9000011")

    database.reset_run()

    assert database.get_listing(listing_db_id) is None


def test_filtered_skip_is_cleared(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _processing_listing(database, "filtered_skip", "9000012")

    database.reset_run()

    assert database.get_listing(listing_db_id) is None


def test_discovered_ai_failed_premium_boost_failed_dry_run_ready_are_cleared(settings) -> None:
    database = Database(settings.database_path)
    ids = [
        _processing_listing(database, status, f"9000020{i}")
        for i, status in enumerate(
            ["discovered", "ai_failed", "premium_boost_failed", "dry_run_ready"]
        )
    ]

    database.reset_run()

    for listing_db_id in ids:
        assert database.get_listing(listing_db_id) is None


def test_dry_run_contact_attempt_for_cleared_listing_is_removed(settings) -> None:
    database = Database(settings.database_path)
    listing = viable_listing(listing_id="9000030")
    listing_db_id, _ = database.discover(listing)
    database.record_contact(listing_db_id, "dry_run", "platform", "dry_run_ready")

    database.reset_run()

    assert database.get_listing(listing_db_id) is None
    assert database.get_actual_contact_attempt(listing_db_id) is None


def test_watcher_bookkeeping_is_reset(settings) -> None:
    database = Database(settings.database_path)
    assert database.watcher_claim_seen("14000001", "munster_wg")
    database.watcher_mark_baseline("munster_wg", ["1", "2"])

    database.reset_run()

    assert database.watcher_get_search_state("munster_wg") is None
    assert database.watcher_claim_seen("14000001", "munster_wg")


# ---- the actual duplicate-protection guarantee --------------------------------------


class _StubSource(SourceAdapter):
    name = "stub"

    def __init__(self, listings):
        self._listings = listings
        self.min_interval_seconds = 0

    def discover(self):
        return self._listings


def test_previously_sent_wg_listing_rediscovered_after_reset_cannot_be_resent(settings) -> None:
    """The core safety guarantee: even after reset_run(), if the exact same
    WG-Gesucht listing is rediscovered, the daemon's own dedup check must skip it
    before any processing/contact happens."""
    database = Database(settings.database_path)
    original = viable_listing(listing_id="9000040")
    listing_db_id = _sent_listing_from(database, original)

    database.reset_run()

    assert database.get_listing(listing_db_id) is not None  # still there, still 'sent'
    assert database.contains(original)  # dedup_key still matches -> daemon will skip

    rediscovered = viable_listing(listing_id="9000040")  # same platform + listing_id
    daemon = ApartmentDaemon(
        settings, sources=[_StubSource([rediscovered])], database=database, callers={}
    )
    processed = daemon.cycle()

    assert processed == 0
    row = database.get_listing(listing_db_id)
    assert row["status"] == "sent"
    assert database.get_actual_contact_attempt(listing_db_id)["status"] == "sent"


def _sent_listing_from(database: Database, listing) -> int:
    listing_db_id, _ = database.discover(listing)
    assert database.claim_actual_contact(listing_db_id, "platform")
    database.record_send_clicked(
        listing_db_id, "fp1", "Hallo, ...", "premium_priority_verified", "attached", "https://x"
    )
    database.record_contact(listing_db_id, "actual", "platform", "sent")
    return listing_db_id


# ---- backup, confirmation, transactionality ------------------------------------------


def test_backup_to_creates_a_restorable_copy(settings, tmp_path) -> None:
    database = Database(settings.database_path)
    _sent_listing(database, "9000050")
    backups_dir = tmp_path / "backups"

    backup_path = database.backup_to(backups_dir)

    assert backup_path.is_file()
    assert backup_path.parent == backups_dir
    assert backup_path.name.startswith("pre_reset_")
    restored = Database(backup_path)
    counts = restored.status_counts()
    assert counts.get("sent", 0) == 1


def test_reset_run_preview_does_not_modify_anything(settings) -> None:
    database = Database(settings.database_path)
    _sent_listing(database, "9000051")
    _processing_listing(database, "review_required", "9000052")

    preview = database.reset_run_preview()

    assert preview["to_clear"] == 1
    assert preview["preserved"] == 1
    assert database.status_counts().get("review_required", 0) == 1  # untouched


def test_cli_reset_run_requires_explicit_confirmation(settings, monkeypatch, tmp_path) -> None:
    from app.cli import _reset_run

    database = Database(settings.database_path)
    listing_db_id = _processing_listing(database, "review_required", "9000060")
    monkeypatch.setattr("builtins.input", lambda _: "")
    monkeypatch.setattr("app.cli._daemon_pid_running", lambda: False)

    code = _reset_run(database, backups_dir=tmp_path / "backups")

    assert code == 0
    assert database.get_listing(listing_db_id) is not None  # nothing was cleared


def test_cli_reset_run_explicit_yes_clears_and_backs_up(settings, monkeypatch, tmp_path) -> None:
    from app.cli import _reset_run

    database = Database(settings.database_path)
    listing_db_id = _processing_listing(database, "review_required", "9000061")
    monkeypatch.setattr("builtins.input", lambda _: "y")
    monkeypatch.setattr("app.cli._daemon_pid_running", lambda: False)
    backups_dir = tmp_path / "backups"

    code = _reset_run(database, backups_dir=backups_dir)

    assert code == 0
    assert database.get_listing(listing_db_id) is None
    backup_files = list(backups_dir.glob("pre_reset_*.sqlite"))
    assert len(backup_files) == 1


def test_reset_run_refuses_while_daemon_running(settings, monkeypatch, tmp_path) -> None:
    from app.cli import _reset_run

    database = Database(settings.database_path)
    listing_db_id = _processing_listing(database, "review_required", "9000062")
    monkeypatch.setattr("app.cli._daemon_pid_running", lambda: True)

    def fail(_prompt: str = "") -> str:
        raise AssertionError("must not even prompt while the daemon is running")

    monkeypatch.setattr("builtins.input", fail)

    code = _reset_run(database, backups_dir=tmp_path / "backups")

    assert code == 2
    assert database.get_listing(listing_db_id) is not None


def test_reset_run_is_transactional_on_failure(settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _processing_listing(database, "review_required", "9000070")
    _sent_listing(database, "9000071")

    original_execute = database.connect

    class _BoomConnection:
        def __init__(self, real_cm):
            self._real_cm = real_cm

        def __enter__(self):
            self._connection = self._real_cm.__enter__()
            return self._connection

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                # Sabotage right before the (would-be) commit, after the deletes
                # were issued on this connection, to prove a mid-transaction
                # failure leaves the database untouched.
                raise RuntimeError("simulated failure before commit")
            return self._real_cm.__exit__(exc_type, exc, tb)

    call_count = 0

    def flaky_connect():
        nonlocal call_count
        call_count += 1
        cm = original_execute()
        if call_count == 1:
            return _BoomConnection(cm)
        return cm

    monkeypatch.setattr(database, "connect", flaky_connect)

    raised = False
    try:
        database.reset_run()
    except RuntimeError:
        raised = True

    assert raised
    # Nothing was committed: the review_required row must still be present.
    assert database.get_listing(listing_db_id) is not None


# ---- never sends, never touches the browser ------------------------------------------


def test_reset_run_never_calls_contact_or_browser_code(settings, monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("reset_run must never contact WG-Gesucht or send anything")

    monkeypatch.setattr("app.contact.contact_listing", fail)
    monkeypatch.setattr("app.browser.prepare_platform_contact", fail)

    database = Database(settings.database_path)
    _sent_listing(database, "9000080")
    _processing_listing(database, "review_required", "9000081")

    database.reset_run()  # must not raise, must not touch the patched functions


def test_reset_run_without_flag_never_touches_the_watcher_source(
    settings, monkeypatch, tmp_path
) -> None:
    from app.cli import _reset_run

    def fail(*_args, **_kwargs):
        raise AssertionError("reset-run without --baseline-current must never open a browser")

    monkeypatch.setattr("app.sources.WGSearchWatcherSource.discover", fail)
    monkeypatch.setattr("app.cli._daemon_pid_running", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    database = Database(settings.database_path)
    _processing_listing(database, "review_required", "9000090")

    code = _reset_run(database, backups_dir=tmp_path / "backups")  # settings/config omitted

    assert code == 0


# ---- watcher baseline (task 2) ------------------------------------------------------


def test_baseline_watcher_records_current_listings_without_processing(
    settings, config, tmp_path, monkeypatch
) -> None:
    from app.cli import _baseline_watcher

    html = tmp_path / "search.html"
    _write_search_fixture(html, ["500001", "500002"])
    search_config = {"wg_watch": {"searches": [{"name": "munster_wg", "url": html.as_uri()}]}}

    def fail(*_args, **_kwargs):
        raise AssertionError("baseline_watcher must never analyze, draft, or contact")

    monkeypatch.setattr("app.cli.process_listing", fail)
    monkeypatch.setattr("app.cli.contact_listing", fail)

    database = Database(settings.database_path)
    code = _baseline_watcher(settings, database, search_config)

    assert code == 0
    state = database.watcher_get_search_state("munster_wg")
    assert state is not None
    assert state["baseline_done"] == 1
    assert database.status_counts() == {}  # nothing was ever discovered into listings


def test_baseline_watcher_replaces_stale_bookkeeping(settings, config, tmp_path) -> None:
    from app.cli import _baseline_watcher

    database = Database(settings.database_path)
    # Simulate an old baseline/claim from a previous run with different listing ids.
    database.watcher_mark_baseline("munster_wg", ["100", "200"])
    assert database.watcher_claim_seen("999", "munster_wg")

    html = tmp_path / "search.html"
    _write_search_fixture(html, ["500001", "500002"])
    search_config = {"wg_watch": {"searches": [{"name": "munster_wg", "url": html.as_uri()}]}}

    code = _baseline_watcher(settings, database, search_config)

    assert code == 0
    # The stale claim for "999" is gone: it can be claimed fresh again, rather than
    # being permanently invisible to the watcher because of pre-reset bookkeeping.
    assert database.watcher_claim_seen("999", "munster_wg") is True
    state = database.watcher_get_search_state("munster_wg")
    assert state is not None and state["baseline_done"] == 1
    assert watcher_baseline_ids(database, "munster_wg") == {"500001", "500002"}


def test_reset_run_baseline_current_clears_history_and_rebaselines(
    settings, config, tmp_path, monkeypatch
) -> None:
    from app.cli import _reset_run

    monkeypatch.setattr("app.cli._daemon_pid_running", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    html = tmp_path / "search.html"
    _write_search_fixture(html, ["500001", "500002"])
    search_config = {"wg_watch": {"searches": [{"name": "munster_wg", "url": html.as_uri()}]}}

    database = Database(settings.database_path)
    listing_db_id = _processing_listing(database, "review_required", "9000091")
    database.watcher_mark_baseline("munster_wg", ["999999"])  # stale, pre-reset baseline

    code = _reset_run(
        database,
        settings,
        search_config,
        backups_dir=tmp_path / "backups",
        baseline_current=True,
    )

    assert code == 0
    assert database.get_listing(listing_db_id) is None
    state = database.watcher_get_search_state("munster_wg")
    assert state is not None
    assert state["baseline_done"] == 1
    # A listing visible right now must be recorded as baseline, not enqueued.
    assert database.status_counts() == {}


def test_baseline_watcher_requires_configured_searches(settings, monkeypatch) -> None:
    """An empty config alone is NOT enough to prove "no searches configured":
    configured_wg_searches() falls back to the legacy WG_SEARCH_URL env var, which
    defaults to a real WG-Gesucht URL. Must also clear that env var, or this test
    would silently launch a real browser against the live site."""
    from app.cli import _baseline_watcher

    monkeypatch.setenv("WG_SEARCH_URL", "")
    database = Database(settings.database_path)

    code = _baseline_watcher(settings, database, {})

    assert code == 2
