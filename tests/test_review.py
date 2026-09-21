from __future__ import annotations

from dataclasses import replace

from app.cli import _approve, _reconcile_by_id, _reject, _review, _review_detail
from app.database import Database
from app.review import (
    SEND_STATE_UNKNOWN_MESSAGE,
    approval_blockers,
    build_approved_outcome,
    load_listing_record,
    rows_needing_review,
)
from app.schemas import (
    AnalysisOutcome,
    AttachmentDecision,
    ListingFacts,
    MessageDraft,
    RuleDecision,
    ValidationResult,
)
from tests.conftest import viable_listing

BODY = (
    "Hallo zusammen, ich interessiere mich sehr für euer Zimmer und freue mich schon "
    "sehr auf eine Antwort von euch, vielen Dank im Voraus."
)


def _save_review_required(
    database: Database,
    *,
    listing_id: str = "1000001",
    hard_skip_reasons: list[str] | None = None,
    unresolved_required_facts: list[str] | None = None,
    scam_risk: str = "low",
    message_body: str | None = BODY,
    status: str = "review_required",
) -> int:
    listing = viable_listing(
        listing_id=listing_id, url=f"https://www.wg-gesucht.de/x.{listing_id}.html"
    )
    facts = ListingFacts(
        warm_rent_eur=490,
        unresolved_required_facts=unresolved_required_facts or [],
        scam_risk=scam_risk,  # type: ignore[arg-type]
    )
    decision = RuleDecision(
        decision="REVIEW",
        hard_skip_reasons=hard_skip_reasons or [],
        warnings=["soft age mismatch"],
    )
    message = (
        MessageDraft(subject="WG-Zimmer", body=message_body, answered_question_ids=["q1"])
        if message_body
        else None
    )
    # "send_state_unknown"/"filtered_skip" are DB-level statuses set post-hoc (by
    # reconcile_contact/record_contact, or apply_rules' SKIP path) rather than by
    # AnalysisOutcome construction, so build a valid outcome first and then, for those
    # two, overwrite the stored status directly -- mirroring how production actually
    # reaches them.
    build_status = status if status in {"review_required", "filtered_skip"} else "review_required"
    outcome = AnalysisOutcome(
        listing=listing,
        facts=facts,
        rule_decision=decision,
        status=build_status,  # type: ignore[arg-type]
        message=message,
        attachment=AttachmentDecision(
            allowed=True,
            should_attach=True,
            source="wg_account",
            requires_browser_verification=True,
        ),
        validation=ValidationResult(auto_send_allowed=False, errors=["needs human review"]),
    )
    listing_db_id = database.save_outcome(outcome)
    if status not in {"review_required", "filtered_skip"}:
        with database.connect() as connection:
            connection.execute("UPDATE listings SET status=? WHERE id=?", (status, listing_db_id))
    return listing_db_id


# ---- review listing / grouping -----------------------------------------------------


def test_review_list_includes_review_required(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)

    grouped = rows_needing_review(database)

    ids = [row["id"] for row in grouped["review_required"]]
    assert listing_db_id in ids


def test_review_detail_shows_exact_reason_and_message(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(
        database, unresolved_required_facts=["Immatrikulationsbescheinigung"]
    )

    row = database.get_listing(listing_db_id)
    record = load_listing_record(row)

    assert record.message is not None
    assert record.message.body == BODY
    assert any("Immatrikulationsbescheinigung" in reason for reason in record.reasons)


def test_review_commands_do_not_modify_unrelated_listings(settings) -> None:
    database = Database(settings.database_path)
    target_id = _save_review_required(database, listing_id="2000001")
    other_id = _save_review_required(database, listing_id="2000002")
    before = database.get_listing(other_id)

    _review(database, None)
    _review_detail(database, target_id)

    after = database.get_listing(other_id)
    assert after == before


# ---- approve safety gates -----------------------------------------------------------


def test_approve_default_no_does_not_send(settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    monkeypatch.setattr("builtins.input", lambda _: "")

    def fail(*_args, **_kwargs):
        raise AssertionError("contact_listing must not be called without explicit yes")

    monkeypatch.setattr("app.cli.contact_listing", fail)

    code = _approve(database, settings, listing_db_id)

    assert code == 0
    row = database.get_listing(listing_db_id)
    assert row["status"] == "review_required"


def test_approve_requires_explicit_yes(settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    def fail(*_args, **_kwargs):
        raise AssertionError("contact_listing must not be called on a 'n' answer")

    monkeypatch.setattr("app.cli.contact_listing", fail)

    code = _approve(database, settings, listing_db_id)

    assert code == 0


def test_approve_routes_through_existing_sender(settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    captured = {}

    def fake_contact_listing(outcome, _settings, _database, trigger):
        captured["outcome"] = outcome
        captured["trigger"] = trigger
        from app.schemas import ContactResult

        return ContactResult(status="dry_run_ready", detail="stopped before Send")

    monkeypatch.setattr("app.cli.contact_listing", fake_contact_listing)

    code = _approve(database, settings, listing_db_id)

    assert code == 0
    outcome = captured["outcome"]
    assert captured["trigger"] == "manual_cli"
    assert outcome.validation.auto_send_allowed is True
    assert outcome.rule_decision.decision == "APPLY"
    assert outcome.database_id == listing_db_id
    assert outcome.message.body == BODY


def test_hard_skipped_listing_cannot_be_approved(settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(
        database, hard_skip_reasons=["high scam risk"], scam_risk="high"
    )

    def fail(_prompt: str = "") -> str:
        raise AssertionError("must never prompt for a hard-skipped listing")

    monkeypatch.setattr("builtins.input", fail)

    code = _approve(database, settings, listing_db_id)

    assert code == 2


def test_filtered_skip_status_cannot_be_approved(settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database, status="filtered_skip")

    def fail(_prompt: str = "") -> str:
        raise AssertionError("must never prompt for a filtered_skip listing")

    monkeypatch.setattr("builtins.input", fail)

    code = _approve(database, settings, listing_db_id)

    assert code == 2


def test_send_state_unknown_cannot_be_approved(settings, monkeypatch, capsys) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database, status="send_state_unknown")

    def fail(_prompt: str = "") -> str:
        raise AssertionError("must never prompt for a send_state_unknown listing")

    monkeypatch.setattr("builtins.input", fail)

    code = _approve(database, settings, listing_db_id)

    assert code == 2
    printed = " ".join(capsys.readouterr().out.split())
    assert " ".join(SEND_STATE_UNKNOWN_MESSAGE.split()) in printed


def test_missing_draft_cannot_be_approved(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database, message_body=None)

    row = database.get_listing(listing_db_id)
    record = load_listing_record(row)
    blockers = approval_blockers(record)

    assert any("no valid drafted message" in blocker for blocker in blockers)


def test_build_approved_outcome_forces_apply_and_allowed(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    row = database.get_listing(listing_db_id)
    record = load_listing_record(row)

    outcome = build_approved_outcome(record)

    assert outcome.rule_decision.decision == "APPLY"
    assert outcome.validation.auto_send_allowed is True
    assert outcome.database_id == listing_db_id


# ---- reject ---------------------------------------------------------------------


def test_reject_prevents_future_reprocessing(settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing = viable_listing(listing_id="3000001")
    listing_db_id, _ = database.discover(listing)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    code = _reject(database, listing_db_id)

    assert code == 0
    row = database.get_listing(listing_db_id)
    assert row["status"] == "manually_rejected"
    assert database.contains(listing)


def test_reject_default_no_makes_no_changes(settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _save_review_required(database)
    monkeypatch.setattr("builtins.input", lambda _: "")

    code = _reject(database, listing_db_id)

    assert code == 0
    row = database.get_listing(listing_db_id)
    assert row["status"] == "review_required"


# ---- reconcile by id --------------------------------------------------------------


def test_reconcile_by_id_marks_sent_from_live_page(settings, tmp_path) -> None:
    html = tmp_path / "room.14096490.html"
    html.write_text(
        f"""<!doctype html><html><body>
        <a class='wgg-btn-primary' href='/nachricht.html?nachrichten-id=1'>conversation</a>
        <div class="messages">{BODY}</div>
        </body></html>""",
        encoding="utf-8",
    )
    database = Database(settings.database_path)
    listing = viable_listing(platform="wg_gesucht", listing_id="14096490", url=html.as_uri())
    listing_db_id, _ = database.discover(listing)
    with database.connect() as connection:
        connection.execute("UPDATE listings SET message_body=? WHERE id=?", (BODY, listing_db_id))

    code = _reconcile_by_id(listing_db_id, replace(settings, browser_headless=True), database)

    assert code == 0
    assert database.get_listing(listing_db_id)["status"] == "sent"


def test_reconcile_by_id_reports_not_sent(settings, tmp_path) -> None:
    html = tmp_path / "room.14096490.html"
    html.write_text(
        """<!doctype html><html><body>
        <a href='/nachricht-senden/room.14096490.html'>Contact</a>
        </body></html>""",
        encoding="utf-8",
    )
    database = Database(settings.database_path)
    listing = viable_listing(platform="wg_gesucht", listing_id="14096490", url=html.as_uri())
    listing_db_id, _ = database.discover(listing)
    with database.connect() as connection:
        connection.execute("UPDATE listings SET message_body=? WHERE id=?", (BODY, listing_db_id))

    code = _reconcile_by_id(listing_db_id, replace(settings, browser_headless=True), database)

    assert code == 0
    assert database.get_listing(listing_db_id)["status"] == "not_sent"


def test_reconcile_by_id_without_url_refuses(settings) -> None:
    database = Database(settings.database_path)
    listing = viable_listing(platform="wg_gesucht", listing_id="14096490", url="")
    listing_db_id, _ = database.discover(listing)

    code = _reconcile_by_id(listing_db_id, settings, database)

    assert code == 2


def test_reconcile_never_clicks_send_via_id(settings, tmp_path) -> None:
    html = tmp_path / "room.14096490.html"
    html.write_text(
        """<!doctype html><html><body>
        <a href='/nachricht-senden/room.14096490.html'>Contact</a>
        <button onclick="window.__clicked = (window.__clicked || 0) + 1">Send</button>
        </body></html>""",
        encoding="utf-8",
    )
    database = Database(settings.database_path)
    listing = viable_listing(platform="wg_gesucht", listing_id="14096490", url=html.as_uri())
    listing_db_id, _ = database.discover(listing)
    with database.connect() as connection:
        connection.execute("UPDATE listings SET message_body=? WHERE id=?", (BODY, listing_db_id))

    _reconcile_by_id(listing_db_id, replace(settings, browser_headless=True), database)
    # The reconciliation path never calls .click(); if it ever did, the listing page's
    # onclick handler above would have incremented window.__clicked, which we cannot
    # observe post-hoc here -- the guarantee itself is covered at the browser layer by
    # tests/test_reconciliation.py::test_reconcile_never_clicks_anything. This test only
    # asserts the id-based wiring reaches a definite, non-crashing result.
    assert database.get_listing(listing_db_id)["status"] in {"not_sent", "send_state_unknown"}
