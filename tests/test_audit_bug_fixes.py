from __future__ import annotations

from dataclasses import replace

import pytest

from app.analyzer import process_listing
from app.database import Database
from app.message_policy import is_formal_application_context
from app.prefilter import deterministic_prefilter
from app.providers import ProviderUnavailable
from app.review import reprocess_listing
from app.rules import apply_rules
from app.schemas import (
    AnalysisOutcome,
    AttachmentDecision,
    HiddenQuestion,
    ListingFacts,
    MessageDraft,
    RuleDecision,
    SourceListing,
    ValidationResult,
)
from app.sources import configured_sources
from app.validator import _words, validate_message
from tests.conftest import successful_call, viable_listing


def _wg_listing(listing_id: str, text: str, title: str = "") -> SourceListing:
    return SourceListing(
        platform="wg_gesucht",
        listing_id=listing_id,
        url=f"https://www.wg-gesucht.de/zimmer.{listing_id}.html",
        title=title,
        raw_text=text,
    )


def _outage(listing, settings, config, answers):
    def fail(*_args):
        raise ProviderUnavailable("all providers unavailable")

    return process_listing(
        listing,
        settings=replace(settings, provider_order=("anthropic",)),
        config=config,
        answers=answers,
        callers={"anthropic": fail},
    )


# ---- Task 3: Zwischenmiete final rule --------------------------------------------


@pytest.mark.parametrize(
    ("months", "rent", "expected_decision"),
    [
        (3, 500, "APPLY"),
        (5, 590, "APPLY"),
        (6, 600, "APPLY"),
        (7, 500, "SKIP"),
        (8, 350, "APPLY"),
        (1, 450, "APPLY"),
    ],
)
def test_zwischenmiete_final_rule_examples(
    months: int, rent: int, expected_decision: str, config: dict
) -> None:
    listing = _wg_listing(
        f"zw-{months}-{rent}",
        f"Zwischenmiete WG-Zimmer, {rent} € warm, für {months} Monate.",
    )
    result = deterministic_prefilter(listing, config)
    assert result.facts.housing_type == "zwischenmiete"
    decision = apply_rules(result.facts, config, result)
    assert decision.decision == expected_decision, decision.hard_skip_reasons


def test_zwischenmiete_duration_estimated_from_move_in_and_end_date(config: dict) -> None:
    """DB 78-style listing: duration given as move-in/end dates, not "für X Monate"."""
    listing = _wg_listing(
        "zw-dates-1",
        "Zwischenmiete WG-Zimmer, 550 € warm. frei ab: 01.10.2026 frei bis: 31.03.2027",
    )
    result = deterministic_prefilter(listing, config)
    decision = apply_rules(result.facts, config, result)
    assert decision.decision == "APPLY", decision.hard_skip_reasons


def test_zwischenmiete_seven_months_over_400_is_skipped_via_dates(config: dict) -> None:
    listing = _wg_listing(
        "zw-dates-2",
        "Zwischenmiete WG-Zimmer, 500 € warm. frei ab: 01.10.2026 frei bis: 30.04.2027",
    )
    result = deterministic_prefilter(listing, config)
    decision = apply_rules(result.facts, config, result)
    assert decision.decision == "SKIP"
    assert any("Zwischenmiete duration" in reason for reason in decision.hard_skip_reasons)


def test_normal_long_term_rental_still_uses_generic_minimum_duration(config: dict) -> None:
    """The Zwischenmiete max-duration rule must never apply to ordinary long-term
    rentals; those keep the generic minimum-duration requirement."""
    listing = _wg_listing("normal-1", "WG-Zimmer, 490 € warm, mindestens 3 Monate.")
    result = deterministic_prefilter(listing, config)
    assert result.facts.housing_type != "zwischenmiete"
    decision = apply_rules(result.facts, config, result)
    assert decision.decision == "SKIP"
    assert any("below minimum" in reason for reason in decision.hard_skip_reasons)


# ---- Price ceiling ------------------------------------------------------------------


def test_warm_rent_ceiling_is_600(config: dict) -> None:
    assert float(config["housing_rules"]["max_warm_rent_single_eur"]) == 600
    under = deterministic_prefilter(
        _wg_listing("price-1", "WG-Zimmer, 600 € warm, mindestens 12 Monate."), config
    )
    over = deterministic_prefilter(
        _wg_listing("price-2", "WG-Zimmer, 601 € warm, mindestens 12 Monate."), config
    )
    assert apply_rules(under.facts, config, under).decision != "SKIP"
    assert apply_rules(over.facts, config, over).decision == "SKIP"


# ---- Sources: WG-Gesucht active, AStA/na dann disabled by default -----------------


def test_asta_and_na_dann_disabled_by_default(settings, monkeypatch) -> None:
    for var in ("ENABLE_ASTA", "ENABLE_NA_DANN"):
        monkeypatch.delenv(var, raising=False)
    database = Database(settings.database_path)
    sources = configured_sources(replace(settings, gmail_enabled=False), database, {})
    names = {source.name for source in sources}
    assert "asta_muenster" not in names
    assert "na_dann" not in names


def test_asta_and_na_dann_can_be_reenabled_via_env(settings, monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_ASTA", "true")
    monkeypatch.setenv("ENABLE_NA_DANN", "true")
    database = Database(settings.database_path)
    sources = configured_sources(replace(settings, gmail_enabled=False), database, {})
    names = {source.name for source in sources}
    assert "asta_muenster" in names
    assert "na_dann" in names


def test_wg_search_watcher_active_when_configured(settings, monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_ASTA", raising=False)
    monkeypatch.delenv("ENABLE_NA_DANN", raising=False)
    database = Database(settings.database_path)
    config = {"wg_watch": {"searches": [{"name": "x", "url": "https://example.test/x.html"}]}}
    sources = configured_sources(replace(settings, gmail_enabled=False), database, config)
    assert "wg_search_watcher" in {source.name for source in sources}


# ---- Register / advertiser classification (Bug A) ---------------------------------


def test_obvious_wg_listing_with_stray_formal_name_is_still_informal(config: dict) -> None:
    """Regression for DB 9's class of bug: a clearly-WG listing describing multiple
    named current residents (one introduced with a demographic label containing
    "Frau") must never be downgraded to private_landlord/formal register."""
    listing = _wg_listing(
        "wg-formal-name",
        "Zimmer frei in unserer WG. Wir sind Karo, Ferdi und Dela. "
        "4er WG (1 Frau und 1 Mann und 2 Divers). WG-Zimmer, 400 € warm, mindestens 12 Monate. "
        "Es würde uns freuen, wenn du in unsere WG einziehst.",
    )
    result = deterministic_prefilter(listing, config)
    assert result.facts.advertiser_type == "wg"
    assert is_formal_application_context(listing, result.facts) is False


def test_wg_signal_without_wg_room_housing_type_is_still_informal(config: dict) -> None:
    """Robustness: WG context should be detected from several signals, not only the
    housing_type=="wg_room" heuristic."""
    listing = _wg_listing(
        "wg-mitbewohner",
        "Du würdest mit Julius und Dunja zusammenwohnen. Unsere WG ist entspannt. "
        "450 € warm, mindestens 12 Monate.",
    )
    result = deterministic_prefilter(listing, config)
    assert result.facts.advertiser_type == "wg"
    assert is_formal_application_context(listing, result.facts) is False


def test_obvious_private_landlord_uses_formal_register(config: dict) -> None:
    listing = _wg_listing(
        "landlord-1",
        "Sehr geehrte Damen und Herren, ich als privater Vermieter biete ein Studio an. "
        "550 € warm, mindestens 12 Monate.",
    )
    result = deterministic_prefilter(listing, config)
    assert result.facts.advertiser_type == "private_landlord"
    assert is_formal_application_context(listing, result.facts) is True


def test_company_agency_signal_takes_priority(config: dict) -> None:
    listing = _wg_listing(
        "agency-1",
        "Die Hausverwaltung Musterstadt GmbH bietet folgende Wohnung an. "
        "580 € warm, mindestens 12 Monate.",
    )
    result = deterministic_prefilter(listing, config)
    assert result.facts.advertiser_type == "company"
    assert is_formal_application_context(listing, result.facts) is True


def test_hausverwaltung_mentioned_as_third_party_does_not_override_wg_signal(
    config: dict,
) -> None:
    """Regression for DB 78: an individual WG member subletting their room, whose
    listing mentions the building's Hausverwaltung only as a third party the future
    subtenant will separately coordinate with, must stay classified as "wg" -- not be
    promoted to "company" just because that word appears somewhere in the text."""
    listing = _wg_listing(
        "sublet-hausverwaltung",
        "Hi, ich suche nach einer Zwischenmiete für mein WG-Zimmer. Deine Mitbewohner "
        "wären Jule und Niklas. Du würdest auch mit der Hausverwaltung abgesprochen "
        "einen eigenen Untermietvertrag bekommen. 550 € warm, für 6 Monate.",
    )
    result = deterministic_prefilter(listing, config)
    assert result.facts.advertiser_type == "wg"


def test_validator_accepts_correct_wg_ihr_euch_register(settings, config, answers) -> None:
    listing = _wg_listing(
        "wg-real-9",
        "Wir sind Karo, Ferdi und Dela. 4er WG (1 Frau und 1 Mann). WG-Zimmer, 400 € warm, "
        "mindestens 12 Monate. Es würde uns freuen, wenn eine gemeinschaftliche Nutzung "
        "der WG erhalten bleibt.",
    )
    expected = successful_call(listing, config)
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": lambda *_: expected},
    )
    assert not any("wrong register" in error for error in outcome.validation.errors)
    assert not any("formal Sie wording" in error for error in outcome.validation.errors)


# ---- Word count is a soft guideline (Bug B) ----------------------------------------


def test_142_word_message_does_not_hard_fail(settings, config, answers) -> None:
    listing = _wg_listing(
        "wordcount-1",
        "Sehr geehrte Damen und Herren, ich interessiere mich für Ihr Studio. "
        "550 € warm, mindestens 12 Monate.",
    )
    body = "Guten Tag, ich interessiere mich sehr für Ihr Studio in Münster. "
    body += "Die Lage und die Ausstattung gefallen mir außerordentlich gut hier. " * 12
    assert 130 <= len(_words(body)) <= 220
    draft = MessageDraft(body=body, hooks_used=["Studio"], language="de", address_register="sie")
    result = validate_message(
        listing,
        ListingFacts(
            listing_language="de", housing_type="studio", advertiser_type="private_landlord"
        ),
        RuleDecision(decision="APPLY"),
        draft,
        AttachmentDecision(),
        config,
    )
    assert result.auto_send_allowed
    assert not any("length" in error for error in result.errors)


# ---- Hidden question deduplication (Bug C) -----------------------------------------


def test_near_duplicate_hidden_question_is_deduplicated(settings, config, answers) -> None:
    """Regression for DB 11: the AI paraphrasing a listing's hidden question by one
    word must not create a second, unanswered "duplicate" of the same question."""
    # No sentence-ending punctuation before "Wenn du meinst" (real WG-Gesucht listings
    # commonly use ":)" here, like the actual DB 11 text), so the deterministic
    # extractor treats this as one continuous candidate -- exactly like production.
    deterministic_phrasing = (
        "Ich bin selbst erst vor einem halben Jahr eingezogen und hätte auch total Lust, "
        "noch ein bisschen was in der WG umzugestalten, damit es sich hier noch mehr "
        "zuhause fühlt :) Wenn du meinst, dass das gut passen könnte, schreib mir gerne "
        "ein paar Sätze über dich und sag mir direkt, welches Kartenspiel du am liebsten spielst :)"
    )
    ai_paraphrase = deterministic_phrasing.replace("damit es sich hier", "damit man sich hier")
    listing = _wg_listing("dup-question", deterministic_phrasing)
    expected = successful_call(listing, config)
    expected.data.facts.hidden_questions = [
        HiddenQuestion(id="ai-own-id", question=ai_paraphrase, required=True)
    ]
    expected.data.message.answered_question_ids = ["ai-own-id"]
    expected.data.message.body += "\n\nBei Kartenspielen bin ich klar bei Skyjo."

    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": lambda *_: expected},
    )

    assert len(outcome.facts.hidden_questions) == 1
    assert not any("unanswered hidden questions" in error for error in outcome.validation.errors)
    assert not any(
        "unexpected answered question ids" in error for error in outcome.validation.errors
    )
    assert outcome.message is not None
    assert len(outcome.message.answered_question_ids) == 1


def test_genuinely_different_questions_are_not_merged(config: dict) -> None:
    from app.prefilter import dedupe_similar

    a = HiddenQuestion(id="a", question="Was ist dein Lieblingsessen?")
    b = HiddenQuestion(id="b", question="Welches Kartenspiel spielst du am liebsten?")
    survivors, remap = dedupe_similar([a, b], text_of=lambda q: q.question, id_of=lambda q: q.id)
    assert len(survivors) == 2
    assert remap == {}


# ---- Hidden command determinism (Bug D) --------------------------------------------


def test_sonnenblove_instruction_deterministic_under_full_ai_outage(
    settings, config, answers
) -> None:
    """Regression for DB 71: a normal, unpriced furniture-takeover mention must not
    block the fallback, and the deterministic "start with Sonnenblume" instruction
    must be satisfied even when every cloud provider fails."""
    listing = _wg_listing(
        "sonnenblume-1",
        "WG-Zimmer, 450 € warm, mindestens 12 Monate. "
        "Wenn du Möbel von mir übernehmen möchtest, können wir da gerne drüber reden. "
        "Wenn du Interesse hast, schreib uns gerne und erzähl ein bisschen was über dich "
        "und beginne deine Nachricht mit dem Wort Sonnenblume.",
    )
    outcome = _outage(listing, settings, config, answers)

    assert outcome.status == "drafted", (outcome.validation.errors, outcome.router_notes)
    assert outcome.facts.scam_risk == "low"
    assert outcome.message is not None
    assert outcome.message.body.strip().split()[0] == "Sonnenblume"


def test_duplicate_hidden_command_from_duplicated_page_text_is_deduplicated(
    config: dict,
) -> None:
    sentence = (
        "Wenn du Interesse hast, schreib uns gerne und erzähl ein bisschen was über dich "
        "und beginne deine Nachricht mit dem Wort Sonnenblume."
    )
    text = f"WG-Leben\n{sentence}\n\nSonstiges\n{sentence}"
    listing = _wg_listing("dup-command", text)
    result = deterministic_prefilter(listing, config)
    commands = [c for c in result.facts.hidden_commands if c.kind == "required_first_word"]
    assert len(commands) == 1


def test_generic_tell_about_yourself_question_does_not_block_fallback(
    settings, config, answers
) -> None:
    listing = _wg_listing(
        "self-intro-1",
        "WG-Zimmer, 450 € warm, mindestens 12 Monate. "
        "Schreib uns gerne ein bisschen was über dich.",
    )
    outcome = _outage(listing, settings, config, answers)
    assert outcome.status == "drafted", (outcome.validation.errors, outcome.router_notes)


# ---- AI outage does not mean "cannot apply" (Bug E) --------------------------------


def test_all_providers_unavailable_still_produces_a_safe_application(
    settings, config, answers
) -> None:
    listing = _wg_listing(
        "outage-safe-1", "WG-Zimmer, 490 € warm, mindestens 12 Monate. Wir kochen gerne zusammen."
    )
    outcome = _outage(listing, settings, config, answers)
    assert outcome.status == "drafted"
    assert outcome.validation.auto_send_allowed


# ---- Documents: enrollment certificate is a known available document (Bug K) ------


def test_enrollment_certificate_concern_is_not_unresolved(settings, config, answers) -> None:
    listing = _wg_listing(
        "enrollment-1",
        "WG-Zimmer, 490 € warm, mindestens 12 Monate. Bitte Immatrikulationsbescheinigung "
        "mitsenden.",
    )
    expected = successful_call(listing, config)
    expected.data.facts.unresolved_required_facts = [
        "Ob Rakshit zum relevanten Zeitpunkt eine Immatrikulationsbescheinigung "
        "vorlegen kann, muss geklärt werden."
    ]
    expected.data.facts.critical_ambiguities = [
        "Eine Immatrikulationsbescheinigung wird ausdrücklich verlangt; nicht bestätigt, "
        "ob diese vorliegt."
    ]
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": lambda *_: expected},
    )
    assert not outcome.facts.unresolved_required_facts
    assert not outcome.facts.critical_ambiguities
    assert not any("unresolved required listing facts" in e for e in outcome.validation.errors)


def test_document_not_in_bewerbermappe_is_still_unresolved(settings, config, answers) -> None:
    """Must never fabricate a document that isn't actually configured as available."""
    listing = viable_listing()
    expected = successful_call(listing, config)
    expected.data.facts.unresolved_required_facts = [
        "Ob Rakshit einen aktuellen Kontoauszug der letzten drei Monate vorlegen kann, ist unklar."
    ]
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": lambda *_: expected},
    )
    assert outcome.facts.unresolved_required_facts


# ---- Reprocessing safety (Task G) --------------------------------------------------


def test_review_required_row_can_be_safely_reprocessed(settings, config, answers) -> None:
    database = Database(settings.database_path)
    listing = _wg_listing(
        "reprocess-1",
        "WG-Zimmer, 450 € warm, mindestens 12 Monate. "
        "Wenn du Möbel von mir übernehmen möchtest, können wir da gerne drüber reden.",
    )

    def fail(*_args):
        raise ProviderUnavailable("outage")

    process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        database=database,
        callers={"anthropic": fail},
    )
    row = database.find_listing_by_url_or_id(listing.url)
    assert row is not None

    action = reprocess_listing(database, settings, config, answers, int(row["id"]))

    assert action.ok
    assert action.status == "drafted"


@pytest.mark.parametrize("status", ["sent", "already_contacted", "send_state_unknown"])
def test_settled_send_states_cannot_be_reprocessed(settings, status: str) -> None:
    database = Database(settings.database_path)
    listing = viable_listing(listing_id="9999001")
    listing_db_id, _ = database.discover(listing)
    with database.connect() as connection:
        connection.execute("UPDATE listings SET status=? WHERE id=?", (status, listing_db_id))

    action = reprocess_listing(database, settings, {}, {}, listing_db_id)

    assert not action.ok
    assert action.status == status


def test_manually_rejected_requires_explicit_force(settings) -> None:
    database = Database(settings.database_path)
    listing = viable_listing(listing_id="9999002")
    listing_db_id, _ = database.discover(listing)
    database.mark_manually_rejected(listing_db_id)

    refused = reprocess_listing(database, settings, {}, {}, listing_db_id)
    assert not refused.ok

    def fake_process(*_args, **_kwargs) -> AnalysisOutcome:
        return AnalysisOutcome(
            listing=listing,
            facts=ListingFacts(),
            rule_decision=RuleDecision(decision="REVIEW"),
            status="review_required",
            validation=ValidationResult(),
        )

    forced = reprocess_listing(
        database, settings, {}, {}, listing_db_id, force=True, process_fn=fake_process
    )
    assert forced.ok
