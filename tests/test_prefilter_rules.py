from __future__ import annotations

import pytest

from app.prefilter import deterministic_prefilter
from app.rules import apply_rules
from app.schemas import ListingFacts
from tests.conftest import viable_listing


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("WG-Zimmer 450 € warm. Nur für Frauen.", "women-only"),
        ("Apartment 430 € warm. WBS erforderlich.", "WBS"),
        ("Zimmer 400 € warm, Mindestalter 30 Jahre.", "minimum age"),
        ("Katholische Studentenverbindung sucht neues Mitglied.", "Studentenverbindung"),
        ("Kirchenmitgliedschaft und katholische Konfession erforderlich.", "religion"),
        ("Zwischenmiete für 3 Monate, 400 € warm.", "duration"),
        ("WG-Zimmer, 600 € warm, mindestens 12 Monate.", "warm rent"),
        (
            "Vermieter im Ausland, Schlüssel per Post. Zahlung vor Besichtigung.",
            "scam",
        ),
        ("Couch für Erstis, kostenlos für ein paar Nächte.", "couch"),
    ],
)
def test_every_hard_skip(text: str, reason: str, config: dict) -> None:
    result = deterministic_prefilter(viable_listing(raw_text=text), config)
    assert not result.viable
    assert any(reason.casefold() in item.casefold() for item in result.hard_skip_reasons)


def test_age_preference_is_not_hard_requirement(config: dict) -> None:
    result = deterministic_prefilter(
        viable_listing(raw_text="WG-Zimmer 490 € warm. Idealerweise ungefähr 30 Jahre alt."), config
    )
    assert result.viable


def test_whole_flat_exception(config: dict) -> None:
    facts = ListingFacts(
        housing_type="whole_flat",
        warm_rent_eur=1100,
        total_rooms=3,
        realistic_residents=3,
        confidence=0.9,
    )
    result = apply_rules(facts, config)
    assert result.decision == "APPLY"
    assert result.effective_warm_rent_per_person_eur == pytest.approx(366.67)


def test_whole_flat_without_shareability_is_review_warning(config: dict) -> None:
    facts = ListingFacts(housing_type="whole_flat", warm_rent_eur=1100, confidence=0.9)
    result = apply_rules(facts, config)
    assert result.decision == "REVIEW"
    assert "whole-flat shareability needs review" in result.warnings


@pytest.mark.parametrize(
    ("facts", "warning"),
    [
        (ListingFacts(anmeldung="no", confidence=0.9), "Anmeldung"),
        (ListingFacts(furniture_takeover_eur=500, confidence=0.9), "Ablöse"),
        (ListingFacts(deposit_eur=1200, cold_rent_eur=400, confidence=0.9), "deposit"),
        (ListingFacts(buergschaft_required=True, confidence=0.9), "Bürgschaft"),
        (ListingFacts(indexmiete=True, confidence=0.9), "Indexmiete"),
        (ListingFacts(hauptmieter_liability=True, confidence=0.9), "Hauptmieter"),
        (
            ListingFacts(odd_fee_or_payment_demands=["Western Union"], confidence=0.9),
            "payment",
        ),
    ],
)
def test_every_warning(facts: ListingFacts, warning: str, config: dict) -> None:
    result = apply_rules(facts, config)
    assert any(warning.casefold() in item.casefold() for item in result.warnings)


def test_any_unsatisfied_mandatory_requirement_skips(config: dict) -> None:
    result = apply_rules(
        ListingFacts(
            mandatory_incompatibilities=["must already be enrolled in a PhD"], confidence=0.9
        ),
        config,
    )
    assert result.decision == "SKIP"


def test_unknown_rent_and_unknown_sublet_duration_require_review(config: dict) -> None:
    no_rent = apply_rules(ListingFacts(confidence=0.9), config)
    sublet = apply_rules(
        ListingFacts(housing_type="zwischenmiete", warm_rent_eur=450, confidence=0.9), config
    )
    assert no_rent.decision == "REVIEW"
    assert sublet.decision == "REVIEW"


def test_hidden_items_get_stable_ids(config: dict) -> None:
    text = (
        "Damit wir wissen, dass du die Anzeige gelesen hast: Schreib uns bitte, welches "
        'Kartenspiel du am liebsten spielst. Beginne deine Nachricht mit "Moin". '
        'Der Betreff soll "Sonnenblume" lauten.'
    )
    first = deterministic_prefilter(viable_listing(raw_text=text), config).facts
    second = deterministic_prefilter(viable_listing(raw_text=text), config).facts
    assert first.hidden_questions
    assert first.hidden_questions == second.hidden_questions
    assert {item.kind for item in first.hidden_commands} == {
        "required_first_word",
        "exact_subject",
    }
