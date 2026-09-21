"""Offline regressions for confirmed September 2026 applicant facts.

All browser tests use local HTML; none can contact WG-Gesucht or send a message.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from playwright.sync_api import sync_playwright

from app.analyzer import _confirmed_profile_resolves, process_listing
from app.answer_bank_resolver import resolve_hidden_questions
from app.browser import attach_wg_applicant_photo, prepare_platform_contact
from app.database import Database
from app.documents import validate_applicant_photo
from app.prefilter import deterministic_prefilter, photo_required_for_initial_contact
from app.rules import apply_rules
from app.schemas import HiddenQuestion
from app.validator import validate_message
from tests.conftest import NATURAL_WG_BODY, viable_listing
from tests.test_browser_dry_run import _wg_outcome, _write_wg_fixture


def _photo(tmp_path):
    path = tmp_path / "applicant.jpg"
    path.write_bytes(b"\xff\xd8\xff" + b"x" * 2048)
    return path


def test_confirmed_profile_facts_are_centralized(config):
    profile = config["applicant"]
    assert profile["degree_duration_semesters"] == 4
    assert profile["expected_muenster_residence_semesters"] == 4
    assert profile["pets"] == {
        "owns_pets": False,
        "comfortable_living_with_pets": True,
        "particularly_likes": ["dogs"],
    }
    assert profile["private_liability_insurance"] is True
    assert profile["regular_own_income"] is True
    assert profile["parental_guarantor_confirmed"] is False
    assert "salary slip" in config["documents"]["wg_gesucht_bewerbermappe"]["contents"]


@pytest.mark.parametrize(
    "availability",
    ["frei ab: 01.04.2027", "Zimmer erst ab April 2027", "ab April 2027"],
)
def test_april_2027_only_is_timing_skip_not_review(config, availability):
    listing = viable_listing(raw_text=f"WG-Zimmer in Münster, 490 € warm. {availability}.")
    prefilter = deterministic_prefilter(listing, config)
    assert prefilter.facts.move_in == "2027-04-01"
    assert any("availability starts" in item for item in prefilter.hard_skip_reasons)
    assert apply_rules(prefilter.facts, config, prefilter).decision == "SKIP"


def test_october_2026_availability_remains_eligible(config):
    listing = viable_listing(raw_text="WG-Zimmer in Münster, 490 € warm. frei ab: 01.10.2026.")
    prefilter = deterministic_prefilter(listing, config)
    assert not any("availability starts" in item for item in prefilter.hard_skip_reasons)


def test_income_proof_or_guarantee_uses_confirmed_income_not_guarantee(config):
    listing = viable_listing(
        raw_text="WG-Zimmer in Münster, 490 € warm. Einkommensnachweis ODER Bürgschaft nötig."
    )
    prefilter = deterministic_prefilter(listing, config)
    assert prefilter.facts.income_or_guarantor_accepted
    assert not prefilter.facts.parental_guarantor_required
    assert apply_rules(prefilter.facts, config, prefilter).decision == "APPLY"
    assert _confirmed_profile_resolves("Bürgschaft nicht bekannt", config, prefilter.facts, False)


def test_explicit_elternbuergschaft_without_alternative_stays_review(config):
    listing = viable_listing(
        raw_text="WG-Zimmer in Münster, 490 € warm. Elternbürgschaft ist erforderlich."
    )
    prefilter = deterministic_prefilter(listing, config)
    assert prefilter.facts.parental_guarantor_required
    assert apply_rules(prefilter.facts, config, prefilter).decision == "REVIEW"
    assert not _confirmed_profile_resolves(
        "Elternbürgschaft nicht bekannt", config, prefilter.facts, False
    )


def test_explicit_income_alternative_to_parental_guarantee_is_accepted(config):
    listing = viable_listing(
        raw_text="WG-Zimmer in Münster, 490 € warm. Elternbürgschaft oder ein Einkommensnachweis."
    )
    prefilter = deterministic_prefilter(listing, config)
    assert prefilter.facts.income_or_guarantor_accepted
    assert not prefilter.facts.parental_guarantor_required


def test_three_salary_slips_are_not_inferred_from_one(config):
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm. Bitte mit der Bewerbung eine "
            "Elternbürgschaft oder einen Einkommensnachweis über drei Gehälter einreichen."
        )
    )
    prefilter = deterministic_prefilter(listing, config)
    assert prefilter.facts.income_or_guarantor_accepted
    assert prefilter.facts.unresolved_required_facts
    assert apply_rules(prefilter.facts, config, prefilter).decision == "REVIEW"


def test_outgoing_message_cannot_claim_unconfirmed_guarantor(config):
    from app.schemas import AttachmentDecision, MessageDraft, RuleDecision

    listing = viable_listing()
    facts = deterministic_prefilter(listing, config).facts
    draft = MessageDraft(
        body=NATURAL_WG_BODY.replace(
            "Ich suche ein langfristiges Zuhause für ungefähr zwei Jahre.",
            "Eine Elternbürgschaft habe ich. "
            "Ich suche ein langfristiges Zuhause für ungefähr zwei Jahre.",
        ),
        language="de",
        address_register="du",
        hooks_used=["gemeinsamen Kochabenden"],
    )
    validation = validate_message(
        listing,
        facts,
        RuleDecision(decision="APPLY"),
        draft,
        AttachmentDecision(allowed=True, should_attach=True, source="wg_account"),
        config,
    )
    assert "unconfirmed guarantor mentioned in outgoing message" in validation.errors


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Schreib uns, ob du Haustiere hast?", "keine Haustiere"),
        ("Schreib uns, ob du eine Haftpflichtversicherung hast?", "Haftpflichtversicherung"),
        ("Schreib uns, wie viele Semester dein Master dauert?", "vier Semester"),
    ],
)
def test_confirmed_profile_answers_hidden_questions(config, answers, question, expected):
    resolved, unresolved = resolve_hidden_questions(
        [HiddenQuestion(id="q1", question=question)], "de", answers, config["applicant"]
    )
    assert not unresolved
    assert expected in resolved[0].resolved_answer
    assert resolved[0].answer_source == "applicant_profile"


def test_lack_of_profile_fact_never_produces_pet_or_insurance_answer(config, answers):
    profile = {**config["applicant"], "pets": {}, "private_liability_insurance": None}
    questions = [
        HiddenQuestion(id="pets", question="Schreib uns, ob du Haustiere hast?"),
        HiddenQuestion(id="insurance", question="Schreib uns, ob du Haftpflicht hast?"),
    ]
    resolved, unresolved = resolve_hidden_questions(questions, "de", answers, profile)
    assert not resolved
    assert len(unresolved) == 2


@pytest.mark.parametrize(
    "text, required",
    [
        ("Schickt mir bei eurer Bewerbung bitte auch ein aktuelles Foto.", True),
        ("Bewerbungen ohne Foto beantworte ich nicht.", True),
        ("Ein aktuelles Foto ist später zum Vertrag nötig.", False),
        ("Fotos der Wohnung findet ihr in der Anzeige.", False),
    ],
)
def test_photo_requires_explicit_first_contact_demand(text, required):
    assert photo_required_for_initial_contact(text) is required


def test_photo_file_must_exist_and_be_image(tmp_path, config):
    assert "not configured" in validate_applicant_photo(None, config)
    assert "does not exist" in validate_applicant_photo(tmp_path / "missing.jpg", config)
    wrong = tmp_path / "wrong.jpg"
    wrong.write_bytes(b"not a JPEG" + b"x" * 2048)
    assert "JPEG" in validate_applicant_photo(wrong, config)
    too_small = tmp_path / "small.jpg"
    too_small.write_bytes(b"\xff\xd8\xff")
    assert "too small" in validate_applicant_photo(too_small, config)
    assert validate_applicant_photo(_photo(tmp_path), config) is None


def test_photo_required_without_path_reviews_before_cloud_fallback(settings, config, answers):
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm. Wir fahren gern Fahrrad. "
            "Schickt mir bei eurer Bewerbung bitte auch ein aktuelles Foto."
        )
    )
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=settings,
        database=Database(settings.database_path),
        force_fallback=True,
    )
    assert outcome.status == "review_required"
    assert any("APPLICANT_PHOTO_PATH" in item for item in outcome.facts.unresolved_required_facts)
    assert not outcome.validation.auto_send_allowed


def test_photo_required_with_valid_path_can_use_fallback(settings, config, answers, tmp_path):
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm. Wir fahren gern Fahrrad. "
            "Schickt mir bei eurer Bewerbung bitte auch ein aktuelles Foto."
        )
    )
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, applicant_photo_path=_photo(tmp_path)),
        database=Database(settings.database_path),
        force_fallback=True,
    )
    assert outcome.facts.photo_required_with_first_message
    assert outcome.status == "drafted", outcome.validation.errors


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def _photo_dom(accepts_upload: bool) -> str:
    on_change = (
        "document.getElementById('attached').textContent=this.files[0].name; "
        "document.getElementById('attached').style.display='block'"
        if accepts_upload
        else "window.uploadAttempted=true"
    )
    return f"""
      <button data-target="#attachment_options_modal"
        onclick="options.style.display='block'">Attachments</button>
      <div id="options" style="display:none">
        <button class="conversation-attachment-option attach_file"
          onclick="modal.style.display='block'; options.style.display='none'">Foto/Datei</button>
      </div>
      <div id="attachments_modal" style="display:none">
        <div id="file_storage_wrapper"></div>
        <button data-dismiss="modal" onclick="modal.style.display='none'">Bestätigen</button>
      </div>
      <input id="file_input" type="file" onchange="{on_change}">
      <div id="attached" class="pre_attached_files" style="display:none"></div>
      <button id="send" onclick="window.sent=true">Send</button>
    """


def test_photo_upload_requires_visible_attached_filename(page, tmp_path):
    page.set_content(_photo_dom(True))
    result = attach_wg_applicant_photo(page, _photo(tmp_path))
    assert result.verified, result.detail
    assert page.locator(".pre_attached_files:visible").inner_text() == "applicant.jpg"
    assert page.evaluate("() => window.sent || false") is False


def test_photo_upload_without_ui_evidence_never_sends(page, tmp_path):
    page.set_content(_photo_dom(False))
    result = attach_wg_applicant_photo(page, _photo(tmp_path))
    assert not result.verified
    assert "attempt 2" in result.detail
    assert page.evaluate("() => window.uploadAttempted") is True
    assert page.evaluate("() => window.sent || false") is False


def test_photo_path_failure_stops_before_browser_open(settings, tmp_path):
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html)
    outcome = _wg_outcome(html.as_uri())
    outcome.facts.photo_required_with_first_message = True
    result = prepare_platform_contact(
        outcome, replace(settings, applicant_photo_path=None), send_permitted=False
    )
    assert result.status == "review_required"
    assert "APPLICANT_PHOTO_PATH" in result.detail
    assert not (tmp_path / "profile").exists()


def test_required_photo_missing_from_composer_blocks_actual_send(settings, tmp_path):
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html)
    outcome = _wg_outcome(html.as_uri())
    outcome.facts.photo_required_with_first_message = True
    claimed = []
    clicked = []
    result = prepare_platform_contact(
        outcome,
        replace(
            settings,
            applicant_photo_path=_photo(tmp_path),
            browser_headless=True,
            wg_premium_strict=False,
        ),
        send_permitted=True,
        claim_actual=lambda: claimed.append(True) or True,
        record_send_clicked=lambda *_args: clicked.append(True),
    )
    assert result.status == "review_required"
    assert "photo" in result.detail
    assert not claimed and not clicked


def test_required_photo_can_reach_dry_run_with_both_attachments(settings, tmp_path):
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html)
    html.write_text(
        html.read_text(encoding="utf-8").replace("</body>", _photo_dom(True) + "</body>"),
        encoding="utf-8",
    )
    outcome = _wg_outcome(html.as_uri())
    outcome.facts.photo_required_with_first_message = True
    result = prepare_platform_contact(
        outcome,
        replace(
            settings,
            applicant_photo_path=_photo(tmp_path),
            browser_headless=True,
            wg_premium_strict=False,
        ),
        send_permitted=False,
    )
    assert result.status == "dry_run_ready", result.detail
    assert "required photo attached" in result.detail


def test_ordinary_listing_does_not_attempt_separate_photo(settings, tmp_path, monkeypatch):
    html = tmp_path / "room.1234567.html"
    _write_wg_fixture(html)

    def forbidden(*_args):
        raise AssertionError("photo attachment must not run for an ordinary listing")

    monkeypatch.setattr("app.browser.attach_wg_applicant_photo", forbidden)
    result = prepare_platform_contact(
        _wg_outcome(html.as_uri()),
        replace(settings, applicant_photo_path=_photo(tmp_path), wg_premium_strict=False),
        send_permitted=False,
    )
    assert result.status == "dry_run_ready", result.detail
