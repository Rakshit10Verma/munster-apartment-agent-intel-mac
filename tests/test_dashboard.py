from __future__ import annotations

from app.browser import ReconciliationOutcome
from app.dashboard import create_app
from app.database import Database
from app.schemas import (
    AnalysisOutcome,
    AttachmentDecision,
    ContactResult,
    GenerationTrace,
    ListingFacts,
    MessageDraft,
    RuleDecision,
    ValidationResult,
)
from tests.conftest import viable_listing

MESSAGE = (
    "Hey zusammen,\n\n"
    "ich bin Rakshit, 21, und starte im Oktober meinen Master in Münster. Eure entspannte WG "
    "klingt gut. Ich koche gerne, fahre Fahrrad und mag gemeinsame Kartenabende, respektiere "
    "aber genauso den persönlichen Freiraum. Ich bin ordentlich und halte mich an einen "
    "Putzplan.\n\n"
    "Da ich aktuell noch in Berlin wohne, wäre ein erstes Kennenlernen per Video-Call für mich "
    "natürlich am einfachsten. Wenn eine Besichtigung vor Ort lieber ist, sagt mir einfach ein "
    "bisschen vorher Bescheid. Dann kann ich meine Tickets planen und in den nächsten Tagen nach "
    "Münster kommen.\n\n"
    "Falls noch etwas offen ist, schreibt mir einfach gerne. Ich würde mich freuen, mehr über "
    "die Wohnung und das Zusammenleben zu erfahren :)\n\nLiebe Grüße\nRakshit"
)


def _stored_listing(database: Database, *, status: str = "review_required") -> int:
    listing = viable_listing(
        listing_id="9200001",
        title="WG-Zimmer am Hafen",
        raw_text="WG-Zimmer in Münster-Hafen, 490 € warm, mindestens 12 Monate.",
    )
    outcome = AnalysisOutcome(
        listing=listing,
        facts=ListingFacts(
            housing_type="wg_room",
            advertiser_type="wg",
            location="Münster-Hafen",
            warm_rent_eur=490,
        ),
        rule_decision=RuleDecision(decision="REVIEW", warnings=["manual review requested"]),
        status="review_required",
        message=MessageDraft(body=MESSAGE, language="de", address_register="du"),
        attachment=AttachmentDecision(
            allowed=True,
            should_attach=True,
            source="wg_account",
            requires_browser_verification=True,
        ),
        validation=ValidationResult(auto_send_allowed=False, errors=["manual review requested"]),
        generation_trace=GenerationTrace(
            ai_attempted=True,
            ai_failure_reason="insufficient credits",
            fallback_used=True,
            fallback_scenario="wg_room",
            fallback_template="de_wg_wg_room",
            optional_clauses=["work_and_income"],
        ),
        message_source="universal_fallback",
    )
    listing_db_id = database.save_outcome(outcome)
    if status != "review_required":
        with database.connect() as connection:
            connection.execute("UPDATE listings SET status=? WHERE id=?", (status, listing_db_id))
    return listing_db_id


def _token(client) -> str:
    with client.session_transaction() as current:
        return str(current["csrf_token"])


def test_dashboard_lists_and_details_stored_application(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _stored_listing(database)
    app = create_app(settings=settings, database=database)
    app.config["TESTING"] = True
    client = app.test_client()

    index = client.get("/?status=review_required")
    detail = client.get(f"/listing/{listing_db_id}")

    assert index.status_code == 200
    assert b"WG-Zimmer am Hafen" in index.data
    assert b"M\xc3\xbcnster-Hafen" in index.data
    assert b"fallback" in index.data
    assert detail.status_code == 200
    assert b"insufficient credits" in detail.data
    assert b"wg_room" in detail.data
    assert b"Exact message" in detail.data
    assert b"Bewerbermappe and send gates" in detail.data


def test_opening_pages_never_sends(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _stored_listing(database)
    calls = []

    def fake_contact(*args):
        calls.append(args)
        return ContactResult(status="dry_run_ready")

    app = create_app(settings=settings, database=database, contact_fn=fake_contact)
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.get(f"/listing/{listing_db_id}").status_code == 200
    assert client.get(f"/listing/{listing_db_id}/approve").status_code == 200
    assert calls == []


def test_approve_requires_explicit_confirmation_and_uses_normal_pipeline(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _stored_listing(database)
    captured = []

    def fake_contact(outcome, passed_settings, passed_database, trigger):
        captured.append((outcome, passed_settings, passed_database, trigger))
        return ContactResult(status="dry_run_ready", detail="stopped before Send")

    app = create_app(settings=settings, database=database, contact_fn=fake_contact)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get(f"/listing/{listing_db_id}/approve")
    csrf = _token(client)

    no_confirmation = client.post(f"/listing/{listing_db_id}/approve", data={"csrf_token": csrf})
    confirmed = client.post(
        f"/listing/{listing_db_id}/approve",
        data={"csrf_token": csrf, "confirm": "yes"},
    )

    assert b"confirmation_required" in no_confirmation.data
    assert len(captured) == 1
    assert captured[0][0].database_id == listing_db_id
    assert captured[0][0].validation.auto_send_allowed
    assert captured[0][1] is settings
    assert captured[0][2] is database
    assert captured[0][3] == "dashboard"
    assert b"dry_run_ready" in confirmed.data


def test_send_state_unknown_cannot_be_approved_from_dashboard(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _stored_listing(database, status="send_state_unknown")

    def fail(*_args):
        raise AssertionError("unknown send state must never enter the send pipeline")

    app = create_app(settings=settings, database=database, contact_fn=fail)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get(f"/listing/{listing_db_id}/approve")
    response = client.post(
        f"/listing/{listing_db_id}/approve",
        data={"csrf_token": _token(client), "confirm": "yes"},
    )

    assert response.status_code == 200
    assert b"must be reconciled" in response.data
    assert database.get_listing(listing_db_id)["status"] == "send_state_unknown"


def test_reconcile_route_is_read_only_and_never_calls_sender(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _stored_listing(database, status="send_state_unknown")
    reconciliations = []

    def fail_contact(*_args):
        raise AssertionError("reconciliation must not call the sender")

    def fake_reconcile(url, passed_settings, expected_body, listing_id):
        reconciliations.append((url, passed_settings, expected_body, listing_id))
        return ReconciliationOutcome(
            result="sent", signals=["message_fingerprint_exact"], detail="found"
        )

    app = create_app(
        settings=settings,
        database=database,
        contact_fn=fail_contact,
        reconcile_fn=fake_reconcile,
    )
    app.config["TESTING"] = True
    client = app.test_client()
    client.get(f"/listing/{listing_db_id}")
    response = client.post(
        f"/listing/{listing_db_id}/reconcile", data={"csrf_token": _token(client)}
    )

    assert response.status_code == 200
    assert len(reconciliations) == 1
    assert reconciliations[0][2] == MESSAGE
    assert database.get_listing(listing_db_id)["status"] == "sent"


def test_reject_requires_explicit_confirmation(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _stored_listing(database)
    app = create_app(settings=settings, database=database)
    app.config["TESTING"] = True
    client = app.test_client()
    client.get(f"/listing/{listing_db_id}/reject")
    csrf = _token(client)

    response = client.post(f"/listing/{listing_db_id}/reject", data={"csrf_token": csrf})
    assert b"confirmation_required" in response.data
    assert database.get_listing(listing_db_id)["status"] == "review_required"

    response = client.post(
        f"/listing/{listing_db_id}/reject",
        data={"csrf_token": csrf, "confirm": "yes"},
    )
    assert b"manually_rejected" in response.data
    assert database.get_listing(listing_db_id)["status"] == "manually_rejected"
