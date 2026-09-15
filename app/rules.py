from __future__ import annotations

from typing import Any

from .schemas import Decision, ListingFacts, PrefilterResult, RuleDecision


def apply_rules(
    facts: ListingFacts,
    config: dict[str, Any],
    prefilter: PrefilterResult | None = None,
) -> RuleDecision:
    rules = config["housing_rules"]
    skip = list(prefilter.hard_skip_reasons) if prefilter else []
    warnings = list(prefilter.warnings) if prefilter else []
    effective: float | None = None

    def add_unique(target: list[str], reason: str) -> None:
        if reason not in target:
            target.append(reason)

    if facts.women_only:
        add_unique(skip, "women-only / men excluded")
    if facts.wbs_required:
        add_unique(skip, "WBS required")
    if facts.religion_or_confession_required:
        add_unique(skip, "religion/confession membership required")
    if facts.religious_fraternity_or_membership_required:
        add_unique(skip, "religious Studentenverbindung/fraternity obligations required")
    if (
        facts.explicit_min_age is not None
        and facts.explicit_min_age >= 30
        and not facts.age_preference_only
    ):
        add_unique(skip, f"mandatory minimum age {facts.explicit_min_age}")
    if (
        facts.minimum_duration_months is not None
        and facts.minimum_duration_months < rules["min_duration_months"]
    ):
        add_unique(skip, f"duration {facts.minimum_duration_months} months below minimum")
    if facts.scam_risk == "high":
        add_unique(skip, "high scam risk")
    for incompatibility in facts.mandatory_incompatibilities:
        add_unique(skip, f"mandatory requirement cannot be satisfied: {incompatibility}")

    if facts.warm_rent_eur is not None:
        if facts.housing_type == "whole_flat" and rules["whole_flat_exception"]["enabled"]:
            if facts.realistic_residents:
                effective = round(facts.warm_rent_eur / facts.realistic_residents, 2)
                if (
                    effective
                    > rules["whole_flat_exception"]["max_effective_warm_rent_per_person_eur"]
                ):
                    add_unique(skip, f"effective whole-flat rent €{effective:g}/person too high")
            else:
                add_unique(warnings, "whole-flat shareability needs review")
        elif facts.warm_rent_eur > rules["max_warm_rent_single_eur"]:
            add_unique(
                skip,
                f"warm rent €{facts.warm_rent_eur:g} above €{rules['max_warm_rent_single_eur']:g}",
            )
    else:
        add_unique(warnings, "warm rent is unknown")

    needs_manual_housing_review = False
    if (
        facts.housing_type == "whole_flat"
        and facts.warm_rent_eur is not None
        and facts.warm_rent_eur > rules["max_warm_rent_single_eur"]
        and not facts.realistic_residents
    ):
        needs_manual_housing_review = True
    if facts.housing_type == "zwischenmiete" and facts.minimum_duration_months is None:
        add_unique(warnings, "Zwischenmiete duration needs review")
        needs_manual_housing_review = True

    if facts.anmeldung == "no":
        add_unique(warnings, "no Anmeldung")
    if (
        facts.furniture_takeover_eur is not None
        and facts.furniture_takeover_eur > rules["warnings"]["furniture_takeover_over_eur"]
    ):
        add_unique(warnings, f"Ablöse €{facts.furniture_takeover_eur:g}")
    if facts.buergschaft_required:
        add_unique(warnings, "Bürgschaft required")
    if facts.indexmiete:
        add_unique(warnings, "Indexmiete")
    if facts.hauptmieter_liability:
        add_unique(warnings, "Hauptmieter liability")
    if (
        facts.deposit_eur
        and facts.cold_rent_eur
        and facts.deposit_eur / facts.cold_rent_eur
        > rules["warnings"]["deposit_over_cold_rent_multiple"]
    ):
        add_unique(warnings, "high deposit")
    for reason in facts.scam_reasons:
        if facts.scam_risk != "low":
            add_unique(warnings, reason)
    for demand in facts.odd_fee_or_payment_demands:
        add_unique(warnings, f"odd fee/payment demand: {demand}")
    for ambiguity in facts.critical_ambiguities:
        add_unique(warnings, f"critical contradiction/ambiguity: {ambiguity}")

    decision: Decision
    if skip:
        decision = "SKIP"
    elif (
        facts.scam_risk == "medium"
        or facts.unresolved_required_facts
        or facts.confidence < 0.55
        or facts.warm_rent_eur is None
        or needs_manual_housing_review
    ):
        decision = "REVIEW"
    else:
        decision = "APPLY"
    return RuleDecision(
        decision=decision,
        hard_skip_reasons=skip,
        warnings=warnings,
        effective_warm_rent_per_person_eur=effective,
    )
