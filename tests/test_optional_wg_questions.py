"""Optional example questions must not become invented facts or false send blockers."""

from __future__ import annotations

from dataclasses import replace

from app.analyzer import process_listing, reconcile_facts
from app.database import Database
from app.prefilter import deterministic_prefilter, optional_example_question
from app.providers import ProviderCall
from app.review import current_status_reason, current_unresolved_facts, load_listing_record
from app.rules import apply_rules
from app.schemas import (
    AIAnalysis,
    AnalysisOutcome,
    HiddenQuestion,
    ListingFacts,
    MessageDraft,
    ProviderMetadata,
    RuleDecision,
    ValidationResult,
)
from app.telegram_notify import render_listing_card
from tests.conftest import NATURAL_WG_BODY, viable_listing

EXAMPLE = (
    "WG-Zimmer in Münster, 430 € warm. Große Küche mit Balkon, wir fahren gerne Fahrrad. "
    "Falls du dich in der Wohnung siehst, schreib mir gerne so ca. 10 Sätze zu dir, "
    "und z.B. auch: hast du schon mal in einer WG gewohnt, was wäre dir sehr wichtig, "
    "was eher weniger beim zusammen Wohnen? :)"
)
EXPERIENCE_NOTE = (
    "Bisherige WG-Erfahrung ist nicht im Profil bestätigt und muss vor einer "
    "wahrheitsgemäßen Beantwortung geklärt werden."
)


def _ai_with_overstated_requirement() -> AIAnalysis:
    return AIAnalysis(
        facts=ListingFacts(
            housing_type="wg_room",
            warm_rent_eur=430,
            confidence=0.9,
            hidden_questions=[
                HiddenQuestion(
                    id="experience", question="Hast du schon einmal in einer WG gewohnt?"
                ),
                HiddenQuestion(
                    id="preferences", question="Was wäre dir beim Zusammenwohnen wichtig?"
                ),
            ],
            unresolved_required_facts=[EXPERIENCE_NOTE],
        ),
        decision_recommendation="REVIEW",
        message=MessageDraft(
            body=NATURAL_WG_BODY,
            language="de",
            address_register="du",
            hooks_used=["Radfahren"],
            unresolved_required_facts=[EXPERIENCE_NOTE],
        ),
    )


def test_example_cue_marks_matching_questions_optional() -> None:
    assert optional_example_question("Hast du schon einmal in einer WG gewohnt?", EXAMPLE)
    assert optional_example_question("Was wäre dir beim Zusammenwohnen wichtig?", EXAMPLE)
    assert not optional_example_question(
        "Welches Codewort soll in deiner Nachricht stehen?",
        EXAMPLE + " Bitte nenne unbedingt das Codewort Sonnenblume.",
    )


def test_explicit_mandatory_wg_experience_still_requires_real_answer() -> None:
    text = (
        "WG-Zimmer in Münster, 430 € warm. "
        "Bitte beantworte unbedingt: Hast du schon mal in einer WG gewohnt?"
    )
    assert not optional_example_question("Hast du schon mal in einer WG gewohnt?", text)


def test_cloud_overstatement_of_optional_wg_experience_is_removed(config) -> None:
    listing = viable_listing(raw_text=EXAMPLE)
    prefilter = deterministic_prefilter(listing, config)
    facts, _ = reconcile_facts(
        prefilter, _ai_with_overstated_requirement(), config, listing_text=listing.raw_text
    )
    assert facts.hidden_questions
    assert all(not question.required for question in facts.hidden_questions)
    assert facts.unresolved_required_facts == []
    assert apply_rules(facts, config, prefilter).decision == "APPLY"


def test_optional_unknown_fact_does_not_block_cloud_draft(settings, config, answers) -> None:
    listing = viable_listing(raw_text=EXAMPLE)
    call = ProviderCall(
        _ai_with_overstated_requirement(),
        ProviderMetadata(provider="mock", model="offline", latency_ms=1),
    )
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        database=Database(settings.database_path),
        callers={"anthropic": lambda *_args: call},
    )
    assert outcome.facts.unresolved_required_facts == []
    assert outcome.message is not None
    assert outcome.message.unresolved_required_facts == []
    assert outcome.rule_decision.decision == "APPLY"
    assert outcome.status == "drafted", (outcome.validation.errors, outcome.router_notes)
    assert "WG-Erfahrung" not in outcome.message.body


def test_optional_examples_do_not_block_no_ai_fallback(settings, config, answers) -> None:
    listing = viable_listing(raw_text=EXAMPLE)
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=settings,
        database=Database(settings.database_path),
        force_fallback=True,
    )
    assert outcome.status == "drafted", (outcome.validation.errors, outcome.router_notes)
    assert not any(question.required for question in outcome.facts.hidden_questions)


def test_mandatory_unknown_experience_still_reviews_on_outage(settings, config, answers) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 430 € warm. Wir fahren gern Fahrrad. "
            "Bitte beantworte unbedingt: Hast du schon mal in einer WG gewohnt?"
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


def test_db144_still_requires_review_for_separate_parental_guarantee(config) -> None:
    listing = viable_listing(
        raw_text=(
            EXAMPLE + "\nBenötigte Unterlagen: Bewerbermappe Ausweis/ID Bürgschaft "
            "Elternbürgschaft. Bürgschaft ist dem Vermieter sehr wichtig."
        )
    )
    prefilter = deterministic_prefilter(listing, config)
    facts, _ = reconcile_facts(
        prefilter, _ai_with_overstated_requirement(), config, listing_text=listing.raw_text
    )
    assert facts.unresolved_required_facts == []
    assert facts.parental_guarantor_required
    assert apply_rules(facts, config, prefilter).decision == "REVIEW"


def test_existing_db144_style_row_no_longer_displays_optional_wg_prompt(settings) -> None:
    listing = viable_listing(raw_text=EXAMPLE + "\nBenötigte Unterlagen: Elternbürgschaft.")
    facts = ListingFacts(
        housing_type="wg_room",
        warm_rent_eur=430,
        parental_guarantor_required=True,
        hidden_questions=[
            HiddenQuestion(id="old", question="Hast du schon einmal in einer WG gewohnt?")
        ],
        unresolved_required_facts=[EXPERIENCE_NOTE],
    )
    database = Database(settings.database_path)
    outcome = AnalysisOutcome(
        listing=listing,
        facts=facts,
        rule_decision=RuleDecision(decision="REVIEW"),
        status="review_required",
        validation=ValidationResult(
            auto_send_allowed=False, errors=["unresolved required listing facts"]
        ),
    )
    outcome.database_id = database.save_outcome(outcome)
    row = database.get_listing(outcome.database_id)
    assert row is not None
    record = load_listing_record(row)
    assert current_unresolved_facts(record) == []
    assert "Elternbürgschaft" in current_status_reason(record)
    card = render_listing_card(database, outcome.database_id)
    assert card is not None
    assert "WG-Erfahrung" not in card[0]
    assert "Elternbürgschaft" in card[0]
