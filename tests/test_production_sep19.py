"""Offline regressions from the 18/19 September production audit.

No test opens a real WG-Gesucht URL or the production SQLite database.
"""

import json
from dataclasses import replace

import pytest
from playwright.sync_api import sync_playwright

from app import telegram_bot
from app.analyzer import reconcile_facts
from app.browser import attach_wg_bewerbermappe, prepare_platform_contact
from app.contact import contact_listing
from app.database import Database, has_send_evidence
from app.documents import validate_local_bewerbermappe
from app.prefilter import deterministic_prefilter
from app.review import (
    approval_blockers,
    current_status_reason,
    load_listing_record,
    missing_fact_question,
)
from app.schemas import (
    AIAnalysis,
    AttachmentDecision,
    ContactResult,
    HiddenCommand,
    ListingFacts,
    MessageDraft,
)
from app.telegram_notify import render_listing_card
from tests.conftest import viable_listing
from tests.test_browser_dry_run import _wg_outcome, _write_wg_fixture
from tests.test_dashboard import _stored_listing


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def test_real_security_advice_overlay_is_dismissed_before_account_attachment(page) -> None:
    page.set_content("""
      <button data-target="#attachment_options_modal"
        onclick="menu.style.display='block'">Attachment</button>
      <div id="menu" style="display:none">
        <p id="share_application_package"
          onclick="attached.style.display='block'">Meine Bewerbermappe</p>
      </div>
      <div id="attached" class="pre_attached_application_package" style="display:none">
        Meine Bewerbermappe<button id="detach_application_package">Remove</button>
      </div>
      <div id="sec_advice" class="modal" role="dialog"
        style="display:block;position:fixed;inset:0;z-index:999;background:white">
        <button id="sec_advice_submit_button"
          onclick="this.closest('#sec_advice').remove()">
          Ich habe die Sicherheitstipps gelesen</button>
      </div>
    """)
    result = attach_wg_bewerbermappe(page)
    assert result.verified, result.detail
    assert page.locator("#sec_advice").count() == 0
    assert page.locator(".pre_attached_application_package:visible").count() == 1


def test_attachment_relocates_after_rerender_and_second_attempt_succeeds(page) -> None:
    page.set_content("""
      <button data-testid="bewerbermappe-option" onclick="
        const replacement=this.cloneNode(true);
        replacement.removeAttribute('onclick');
        replacement.onclick=function(){document.getElementById('attached').style.display='block'};
        this.replaceWith(replacement)">Meine Bewerbermappe</button>
      <div id="attached" class="pre_attached_application_package" style="display:none">
        <button id="detach_application_package">Remove</button>
      </div>
    """)
    result = attach_wg_bewerbermappe(page)
    assert result.verified, result.detail
    assert "attempt 2" in result.detail


def test_click_without_visible_attachment_retries_three_times_then_fails(page) -> None:
    page.set_content("""
      <button data-testid="bewerbermappe-option"
        onclick="window.attachClicks=(window.attachClicks||0)+1">Meine Bewerbermappe</button>
    """)
    result = attach_wg_bewerbermappe(page)
    assert result.state == "failed" and not result.verified
    assert "attempt 3" in result.detail
    assert page.evaluate("() => window.attachClicks") == 3


def test_active_control_without_attached_document_is_not_verification(page) -> None:
    page.set_content("""
      <button data-testid="bewerbermappe-option"
        onclick="this.classList.add('active')">Meine Bewerbermappe</button>
    """)
    result = attach_wg_bewerbermappe(page)
    assert not result.verified
    assert result.state == "failed"


def test_unverified_attachment_never_claims_or_clicks_send(settings, tmp_path) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html, dossier_success=False)
    source = html.read_text(encoding="utf-8").replace(
        "sent.style.display='block'", "window.sendClicks=(window.sendClicks||0)+1"
    )
    html.write_text(source, encoding="utf-8")
    claims: list[bool] = []
    result = prepare_platform_contact(
        _wg_outcome(html.as_uri()),
        replace(settings, dry_run=False, auto_send=True),
        send_permitted=True,
        claim_actual=lambda: claims.append(True) or True,
    )
    assert result.status == "bewerbermappe_attachment_failed"
    assert not claims
    assert "attempt 3" in result.detail


def test_second_attachment_attempt_continues_to_normal_send_verification(
    settings, tmp_path
) -> None:
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html)
    source = html.read_text(encoding="utf-8").replace(
        "this.dataset.state='attached'",
        "window.attachClicks=(window.attachClicks||0)+1; "
        "if(window.attachClicks===2)this.dataset.state='attached'",
    )
    html.write_text(source, encoding="utf-8")
    claims: list[bool] = []
    result = prepare_platform_contact(
        _wg_outcome(html.as_uri()),
        replace(settings, dry_run=False, auto_send=True),
        send_permitted=True,
        claim_actual=lambda: claims.append(True) or True,
    )
    assert result.status == "sent", result.detail
    assert claims == [True]
    assert "positively verified" in result.detail


def test_local_document_preflight_rejects_missing_empty_and_wrong_type(config, tmp_path) -> None:
    missing = tmp_path / "missing.pdf"
    assert "does not exist" in validate_local_bewerbermappe(missing, config)
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert "empty" in validate_local_bewerbermappe(empty, config)
    wrong = tmp_path / "dossier.zip"
    wrong.write_bytes(b"x")
    assert "type" in validate_local_bewerbermappe(wrong, config)
    valid = tmp_path / "dossier.pdf"
    valid.write_bytes(b"%PDF-1.4\n")
    assert validate_local_bewerbermappe(valid, config) is None


def test_missing_local_document_fails_before_any_email_send(
    settings, monkeypatch, tmp_path
) -> None:
    database = Database(settings.database_path)
    outcome = _wg_outcome("https://example.test/listing.1234567.html")
    outcome = outcome.model_copy(
        update={
            "listing": viable_listing(platform="asta_muenster", contact_email="owner@example.test"),
            "facts": outcome.facts.model_copy(update={"contact_method": "email"}),
            "attachment": AttachmentDecision(
                allowed=True,
                should_attach=True,
                source="local_file",
                path=str(tmp_path / "missing.pdf"),
            ),
        }
    )
    listing_db_id = database.save_outcome(outcome)
    outcome = outcome.model_copy(update={"database_id": listing_db_id})
    monkeypatch.setattr("app.contact.send_gmail", lambda *_a, **_k: pytest.fail("email opened"))
    result = contact_listing(outcome, replace(settings, dry_run=False), database, "manual_cli")
    assert result.status == "bewerbermappe_attachment_failed"
    assert "does not exist" in result.detail


def test_valid_local_document_is_passed_to_existing_email_sender(
    settings, monkeypatch, tmp_path
) -> None:
    database = Database(settings.database_path)
    path = tmp_path / "dossier.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    outcome = _wg_outcome("https://example.test/listing.1234567.html")
    outcome = outcome.model_copy(
        update={
            "listing": viable_listing(platform="asta_muenster", contact_email="owner@example.test"),
            "facts": outcome.facts.model_copy(update={"contact_method": "email"}),
            "attachment": AttachmentDecision(
                allowed=True,
                should_attach=True,
                source="local_file",
                path=str(path),
            ),
        }
    )
    listing_db_id = database.save_outcome(outcome)
    outcome = outcome.model_copy(update={"database_id": listing_db_id})
    seen: list[object] = []

    def fake_email(_settings, _recipient, _subject, _body, attachment, **_kwargs):
        seen.append(attachment)
        return ContactResult(status="sent", detail="offline stub")

    monkeypatch.setattr("app.contact.send_gmail", fake_email)
    result = contact_listing(outcome, replace(settings, dry_run=False), database, "manual_cli")
    assert result.status == "sent"
    assert seen == [path]


def _attachment_failed_row(database: Database) -> int:
    listing_db_id = _stored_listing(database)
    database.record_contact(
        listing_db_id,
        "actual",
        "platform",
        "bewerbermappe_attachment_failed",
        detail="security overlay",
        trigger="telegram",
    )
    return listing_db_id


def test_pre_send_attachment_failure_can_be_claimed_once_for_retry(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _attachment_failed_row(database)
    assert database.claim_actual_contact(listing_db_id, "platform")
    assert not database.claim_actual_contact(listing_db_id, "platform")
    events = database.listing_history(listing_db_id)["events"]
    assert any(event["event"] == "attachment_retry_claimed" for event in events)


def test_any_send_click_evidence_forbids_attachment_retry(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _attachment_failed_row(database)
    with database.connect() as connection:
        connection.execute(
            "UPDATE contact_attempts SET send_clicked_at='2026-09-19T12:00:00+00:00' "
            "WHERE listing_db_id=?",
            (listing_db_id,),
        )
    attempt = database.get_actual_contact_attempt(listing_db_id)
    record = load_listing_record(database.get_listing(listing_db_id))
    assert has_send_evidence(attempt)
    assert not database.claim_actual_contact(listing_db_id, "platform")
    assert any("reconcile" in item for item in approval_blockers(record, attempt))
    rendered = render_listing_card(database, listing_db_id)
    buttons = str(rendered[1])
    assert "reconcile:" in buttons and "Retry Send" not in buttons


def test_competing_pre_send_result_cannot_overwrite_claim_or_send_evidence(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _attachment_failed_row(database)
    assert database.claim_actual_contact(listing_db_id, "platform")
    database.record_contact(
        listing_db_id,
        "actual",
        "platform",
        "bewerbermappe_attachment_failed",
        detail="competing attempt failed before Send",
        trigger="telegram",
    )
    assert database.get_actual_contact_attempt(listing_db_id)["status"] == "claimed"
    database.record_send_clicked(
        listing_db_id, "fingerprint", "message text", "verified", "attached", "url"
    )
    database.record_contact(
        listing_db_id,
        "actual",
        "platform",
        "bewerbermappe_attachment_failed",
        detail="late competing result",
        trigger="telegram",
    )
    assert database.get_actual_contact_attempt(listing_db_id)["status"] == "send_clicked"


def test_telegram_retry_is_two_tap_and_reuses_shared_approval_pipeline(
    settings, monkeypatch
) -> None:
    database = Database(settings.database_path)
    listing_db_id = _attachment_failed_row(database)
    telegram_settings = replace(
        settings,
        telegram_enabled=True,
        telegram_allowed_user_id=111,
        telegram_chat_id="222",
        telegram_bot_token="fake-token",
    )
    edits: list[str] = []
    monkeypatch.setattr("app.telegram_bot.edit_message", lambda *_a, **_k: edits.append(str(_a[3])))
    monkeypatch.setattr("app.telegram_bot.answer_callback_query", lambda *_a, **_k: None)
    calls: list[tuple[object, ...]] = []

    def fake_approve(db, config, ident, *, confirmed, trigger, contact_fn):
        calls.append((db, config, ident, confirmed, trigger, contact_fn))
        with db.connect() as connection:
            connection.execute("UPDATE listings SET status='sent' WHERE id=?", (ident,))
        return None

    monkeypatch.setattr("app.telegram_bot.approve_listing", fake_approve)
    base = {"id": "cb", "from": {"id": 111}, "message": {"chat": {"id": 222}, "message_id": 3}}
    telegram_bot.process_update(
        database,
        telegram_settings,
        {"callback_query": {**base, "data": f"send_request:{listing_db_id}"}},
    )
    assert "Retry application" in edits[-1]
    assert not calls
    telegram_bot.process_update(
        database,
        telegram_settings,
        {"callback_query": {**base, "data": f"send_confirm:{listing_db_id}"}},
    )
    assert len(calls) == 1
    assert calls[0][2:5] == (listing_db_id, True, "telegram")
    assert calls[0][5] is telegram_bot.contact_listing
    assert database.get_listing(listing_db_id)["status"] == "sent"
    telegram_bot.process_update(
        database,
        telegram_settings,
        {"callback_query": {**base, "data": f"send_confirm:{listing_db_id}"}},
    )
    assert len(calls) == 1
    assert "No pending send confirmation" in edits[-1]


def test_stale_telegram_retry_cannot_bypass_changed_state(settings, monkeypatch) -> None:
    database = Database(settings.database_path)
    listing_db_id = _attachment_failed_row(database)
    telegram_settings = replace(
        settings,
        telegram_enabled=True,
        telegram_allowed_user_id=111,
        telegram_chat_id="222",
        telegram_bot_token="fake-token",
    )
    monkeypatch.setattr("app.telegram_bot.edit_message", lambda *_a, **_k: None)
    monkeypatch.setattr("app.telegram_bot.answer_callback_query", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "app.telegram_bot.approve_listing",
        lambda *_a, **_k: pytest.fail("stale callback reached send pipeline"),
    )
    base = {"id": "cb", "from": {"id": 111}, "message": {"chat": {"id": 222}, "message_id": 3}}
    telegram_bot.process_update(
        database,
        telegram_settings,
        {"callback_query": {**base, "data": f"send_request:{listing_db_id}"}},
    )
    with database.connect() as connection:
        connection.execute(
            "UPDATE listings SET status='send_state_unknown' WHERE id=?", (listing_db_id,)
        )
    telegram_bot.process_update(
        database,
        telegram_settings,
        {"callback_query": {**base, "data": f"send_confirm:{listing_db_id}"}},
    )
    assert database.get_listing(listing_db_id)["status"] == "send_state_unknown"


def test_current_dashboard_reasons_are_not_stale_generation_reasons(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _stored_listing(database)
    with database.connect() as connection:
        connection.execute("UPDATE listings SET status='sent' WHERE id=?", (listing_db_id,))
    record = load_listing_record(database.get_listing(listing_db_id))
    assert current_status_reason(record).startswith("Application confirmed sent")
    with database.connect() as connection:
        connection.execute(
            "UPDATE listings SET status='already_contacted' WHERE id=?", (listing_db_id,)
        )
    record = load_listing_record(database.get_listing(listing_db_id))
    assert "Existing WG-Gesucht conversation" in current_status_reason(record)
    with database.connect() as connection:
        connection.execute(
            "UPDATE listings SET status='filtered_skip', decision_json=? WHERE id=?",
            (
                json.dumps(
                    {"decision": "SKIP", "hard_skip_reasons": ["women-only / men excluded"]}
                ),
                listing_db_id,
            ),
        )
    record = load_listing_record(database.get_listing(listing_db_id))
    assert "women-only" in current_status_reason(record)


def test_telegram_missing_fact_is_a_plain_question_not_validator_debug(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _stored_listing(database)
    with database.connect() as connection:
        connection.execute(
            "UPDATE listings SET facts_json=json_set(facts_json, "
            "'$.unresolved_required_facts', json_array(?)) WHERE id=?",
            ("Vor Erstkontakt muss eine Bürgschaft geklärt werden.", listing_db_id),
        )
    record = load_listing_record(database.get_listing(listing_db_id))
    assert missing_fact_question(record) == (
        "Can you provide the Bürgschaft requested in this listing?"
    )
    text, _keyboard, _status = render_listing_card(database, listing_db_id)
    assert "Question: Can you provide" in text
    assert "unresolved_required_facts" not in text


@pytest.mark.parametrize(
    ("text", "expected_later"),
    [
        ("Benötigte Unterlagen Bewerbermappe Elternbürgschaft WG-Gesucht+", True),
        ("Zum Mietvertrag ist eine Bürgschaft erforderlich.", True),
        ("Bürgschaft direkt mit der Bewerbung einreichen.", False),
        ("Später benötigen wir ein Foto.", True),
        ("Bitte ein Foto in der ersten Nachricht mitschicken.", False),
        ("Es wird eine Bürgschaft benötigt.", False),
    ],
)
def test_document_requirement_stage_keeps_only_later_facts_informational(
    settings, config, text, expected_later
) -> None:
    listing = viable_listing(
        raw_text="WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. " + text
    )
    prefilter = deterministic_prefilter(listing, config)
    token = "foto" if "Foto" in text else "bürgschaft"
    assert (
        any(token in item.casefold() for item in prefilter.facts.later_contract_requirements)
        == expected_later
    )
    ai = AIAnalysis(
        facts=ListingFacts(
            housing_type="wg_room",
            warm_rent_eur=490,
            confidence=0.9,
            unresolved_required_facts=[f"Vor Erstkontakt klären, ob {token} vorhanden ist"],
            critical_ambiguities=[f"Unklar, ob {token} vorliegt"],
        ),
        message=MessageDraft(body="x"),
    )
    facts, _ = reconcile_facts(prefilter, ai, config)
    assert bool(facts.unresolved_required_facts) is not expected_later
    assert bool(facts.critical_ambiguities) is not expected_later


def test_external_application_process_is_review_not_automatic_send(config) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 305 € warm. "
            "Siehe Bewerbungsverfahren auf der Homepage. Bewerbungsbogen erforderlich."
        )
    )
    result = deterministic_prefilter(listing, config)
    assert "separate mandatory external application process" in result.facts.critical_ambiguities
    assert not result.hard_skip_reasons


def test_real_cheap_listing_regressions_do_not_hard_skip_on_short_sublet_or_age(config) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer, 330 € Gesamtmiete. Verfügbarkeit frei ab: 01.10.2026 "
            "frei bis: 30.03.2027. Gesucht wird: Geschlecht egal zwischen 25 und 35 Jahren."
        )
    )
    result = deterministic_prefilter(listing, config)
    assert result.facts.housing_type == "zwischenmiete"
    assert result.facts.age_mismatch
    assert not result.hard_skip_reasons


def test_studentenverbindung_without_mandatory_membership_is_not_hard_skip(config) -> None:
    listing = viable_listing(
        raw_text=(
            "Zimmer im Haus einer Studentenverbindung, 265 € warm. "
            "Du kannst ein Semester hier wohnen ohne beizutreten."
        )
    )
    result = deterministic_prefilter(listing, config)
    assert not result.facts.religious_fraternity_or_membership_required
    assert not result.hard_skip_reasons


def test_ai_suspicion_about_fraternity_membership_goes_to_review_not_skip(config) -> None:
    listing = viable_listing(
        raw_text=(
            "Zimmer im Verbindungshaus, 265 € warm. "
            "Du kannst ein Semester hier wohnen ohne beizutreten."
        )
    )
    prefilter = deterministic_prefilter(listing, config)
    ai = AIAnalysis(
        facts=ListingFacts(
            housing_type="wg_room",
            warm_rent_eur=265,
            confidence=0.9,
            religious_fraternity_or_membership_required=True,
            mandatory_incompatibilities=["membership in Studentenverbindung unconfirmed"],
        ),
        message=MessageDraft(body="x"),
    )
    facts, _ = reconcile_facts(prefilter, ai, config)
    assert not facts.religious_fraternity_or_membership_required
    assert not facts.mandatory_incompatibilities
    assert any("membership" in note for note in facts.critical_ambiguities)


def test_conflicting_fixed_dates_require_review_instead_of_cheap_skip(config) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer, 495 € Gesamtmiete. frei ab: 01.10.2026 frei bis: 01.11.2026. "
            "Der Vertrag ist bis maximal zum 31.8.2027 befristet."
        )
    )
    result = deterministic_prefilter(listing, config)
    assert not result.hard_skip_reasons
    assert (
        "listing availability end date conflicts with contract end date"
        in result.facts.critical_ambiguities
    )


def test_house_cleaning_rule_is_not_an_exact_application_command(config) -> None:
    listing = viable_listing(
        raw_text=("WG-Zimmer in Münster, 490 € warm. Halte dich an den Putzplan der WG.")
    )
    prefilter = deterministic_prefilter(listing, config)
    house_rule = HiddenCommand(
        id="cmd_cleaning",
        instruction="Halte dich an den Putzplan der WG.",
        kind="other",
        required=True,
    )
    ai = AIAnalysis(
        facts=ListingFacts(housing_type="wg_room", warm_rent_eur=490, hidden_commands=[house_rule]),
        message=MessageDraft(
            body="An den Putzplan halte ich mich.", applied_command_ids=[house_rule.id]
        ),
    )
    facts, remap = reconcile_facts(prefilter, ai, config)
    assert facts.hidden_commands == []
    assert remap[house_rule.id] == ""


def test_attachment_retry_cannot_override_unverifiable_command(settings) -> None:
    database = Database(settings.database_path)
    listing_db_id = _attachment_failed_row(database)
    with database.connect() as connection:
        connection.execute(
            "UPDATE listings SET validation_json=? WHERE id=?",
            (
                json.dumps(
                    {
                        "auto_send_allowed": False,
                        "errors": ["command cmd_cleaning cannot be deterministically verified"],
                    }
                ),
                listing_db_id,
            ),
        )
    record = load_listing_record(database.get_listing(listing_db_id))
    attempt = database.get_actual_contact_attempt(listing_db_id)
    assert any("deterministic validation" in item for item in approval_blockers(record, attempt))
    assert "Retry Send" not in str(render_listing_card(database, listing_db_id)[1])
