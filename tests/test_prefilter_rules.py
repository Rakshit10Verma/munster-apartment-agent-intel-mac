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
        ("Katholische Studentenverbindung sucht neues Mitglied.", "Studentenverbindung"),
        ("Kirchenmitgliedschaft und katholische Konfession erforderlich.", "religion"),
        ("Zwischenmiete für 3 Wochen, 400 € warm.", "duration"),
        ("WG-Zimmer, 650 € warm, mindestens 12 Monate.", "warm rent"),
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


def test_real_wg_frauen_category_and_age_wording_is_hard_skip(config: dict) -> None:
    listing = viable_listing(
        raw_text=(
            "2er WG (1 Frau). Studenten-WG, Frauen-WG, keine Zweck-WG. "
            "Frau zwischen 21 und 28 Jahren. 500 € warm."
        )
    )
    result = deterministic_prefilter(listing, config)
    assert result.facts.women_only
    assert "women-only / men excluded" in result.hard_skip_reasons


def test_existing_female_resident_alone_is_not_an_exclusion(config: dict) -> None:
    listing = viable_listing(raw_text="2er WG (1 Frau). Gesucht wird ein neues WG-Mitglied.")
    result = deterministic_prefilter(listing, config)
    assert not result.facts.women_only


def test_real_multiline_gesucht_wird_frau_is_hard_skip(config: dict) -> None:
    listing = viable_listing(raw_text="2er WG (1 Frau)\nGesucht wird:\nFrau\n500 € warm")
    result = deterministic_prefilter(listing, config)
    assert "women-only / men excluded" in result.hard_skip_reasons


def test_days_or_weeks_sublet_is_below_minimum(config: dict) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer 20 € pro Tag. Möbliert für ein paar Tage oder Wochen, vom 28.09. bis 09.10."
        )
    )
    result = deterministic_prefilter(listing, config)
    assert any("duration" in reason for reason in result.hard_skip_reasons)


def test_zwischenmiete_of_at_least_one_month_is_viable(config: dict) -> None:
    listing = viable_listing(raw_text="Zwischenmiete für 1 Monat, 400 € warm.")
    result = deterministic_prefilter(listing, config)
    assert result.facts.housing_type == "zwischenmiete"
    assert result.facts.minimum_duration_months == 1
    assert result.viable


def test_real_wg_fixed_date_range_under_six_months_is_acceptable_sublet(config: dict) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 455 € warm. Verfügbarkeit frei ab: 01.03.2027 "
            "frei bis: 31.05.2027. Geschlecht egal."
        )
    )
    result = deterministic_prefilter(listing, config)
    assert result.facts.move_in == "2027-03-01"
    assert result.facts.end_date == "2027-05-31"
    assert result.facts.housing_type == "zwischenmiete"
    assert not result.hard_skip_reasons


def test_unbounded_october_availability_remains_viable(config: dict) -> None:
    listing = viable_listing(
        raw_text="WG-Zimmer, 490 € warm. Verfügbarkeit frei ab: 01.10.2026. Unbefristet."
    )
    result = deterministic_prefilter(listing, config)
    assert result.facts.move_in == "2026-10-01"
    assert result.facts.end_date is None
    assert result.viable


def test_exact_six_month_calendar_range_is_viable(config: dict) -> None:
    listing = viable_listing(
        raw_text=("WG-Zimmer, 490 € warm. Verfügbarkeit frei ab: 01.10.2026 frei bis: 31.03.2027.")
    )
    result = deterministic_prefilter(listing, config)
    assert result.viable


def test_real_catholic_student_room_wording_is_hard_skip(config: dict) -> None:
    listing = viable_listing(
        raw_text=(
            "Wir vergeben teilmöblierte Zimmer an männliche, katholische Studenten. "
            "Im Erdgeschoss befinden sich die Gesellschaftsräume unserer Verbindung."
        )
    )
    result = deterministic_prefilter(listing, config)
    assert any("religion" in reason.casefold() for reason in result.hard_skip_reasons)
    assert any("verbindung" in reason.casefold() for reason in result.hard_skip_reasons)


def test_ordinary_sofa_in_room_is_not_treated_as_couch_listing(config: dict) -> None:
    listing = viable_listing(raw_text="WG-Zimmer, 490 € warm, mit Bett und Sofa möbliert.")
    result = deterministic_prefilter(listing, config)
    assert not any("couch" in reason for reason in result.hard_skip_reasons)


@pytest.mark.parametrize(
    ("fee_text", "expected_total"),
    [
        ("Ablöse für Möbel: 350 €.", 350),
        ("Möbel und Zubehör: 499 €.", 499),
        ("Bearbeitungsgebühr: 100 €.", 100),
    ],
)
def test_known_one_time_fee_below_500_is_acceptable(
    fee_text: str, expected_total: float, config: dict
) -> None:
    result = deterministic_prefilter(
        viable_listing(raw_text=f"WG-Zimmer 490 € warm. {fee_text}"), config
    )
    decision = apply_rules(result.facts, config, result)
    assert result.facts.one_time_fee_eur == expected_total
    assert result.facts.scam_risk == "low"
    assert not result.facts.odd_fee_or_payment_demands
    assert decision.decision == "APPLY"


@pytest.mark.parametrize("amount", [500, 650])
def test_one_time_fee_at_or_above_500_requires_review(amount: int, config: dict) -> None:
    result = deterministic_prefilter(
        viable_listing(raw_text=f"WG-Zimmer 490 € warm. Ablöse für Möbel: {amount} €."), config
    )
    assert result.facts.one_time_fee_eur == amount
    assert result.facts.scam_risk == "medium"
    assert result.facts.odd_fee_or_payment_demands
    assert apply_rules(result.facts, config, result).decision == "REVIEW"


def test_unpriced_furniture_takeover_is_normal_and_does_not_block(config: dict) -> None:
    """A furniture/inventory takeover mentioned without a fixed price is normal,
    negotiable WG practice ("Abschlag je nachdem was du übernimmst") and must not be
    treated as an unusual fee or payment demand, or imply the applicant agreed to pay
    anything. Only genuinely arbitrary, non-property fees with no stated total (e.g. a
    processing/reservation fee) remain review-worthy when unpriced."""
    result = deterministic_prefilter(
        viable_listing(
            raw_text="WG-Zimmer 490 € warm. Möbel müssen gegen Aufpreis übernommen werden."
        ),
        config,
    )
    assert result.facts.one_time_fee_eur is None
    assert result.facts.scam_risk == "low"
    assert not result.facts.odd_fee_or_payment_demands
    assert apply_rules(result.facts, config, result).decision == "APPLY"


def test_unpriced_administrative_fee_still_requires_review(config: dict) -> None:
    result = deterministic_prefilter(
        viable_listing(raw_text="WG-Zimmer 490 € warm. Es fällt eine Bearbeitungsgebühr an."),
        config,
    )
    assert result.facts.one_time_fee_eur is None
    assert result.facts.scam_risk == "medium"
    assert result.facts.odd_fee_or_payment_demands
    assert apply_rules(result.facts, config, result).decision == "REVIEW"


def test_suspicious_payment_method_is_not_accepted_below_500(config: dict) -> None:
    result = deterministic_prefilter(
        viable_listing(
            raw_text="WG-Zimmer 490 € warm. Bearbeitungsgebühr 100 € per Western Union."
        ),
        config,
    )
    assert result.facts.one_time_fee_eur == 100
    assert result.facts.scam_risk == "medium"
    assert "western union" in result.facts.odd_fee_or_payment_demands
    assert apply_rules(result.facts, config, result).decision == "REVIEW"


def test_payment_before_viewing_is_hard_skip_even_below_500(config: dict) -> None:
    result = deterministic_prefilter(
        viable_listing(raw_text="WG-Zimmer 490 € warm. Zahlung 100 € vor Besichtigung."), config
    )
    assert result.facts.scam_risk == "high"
    assert apply_rules(result.facts, config, result).decision == "SKIP"


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
        (ListingFacts(furniture_takeover_eur=500, confidence=0.9), "one-time fee"),
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
