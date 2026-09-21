from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .schemas import Decision, ListingFacts, PrefilterResult, RuleDecision


def zwischenmiete_duration_months(facts: ListingFacts) -> int | None:
    if facts.minimum_duration_months is not None:
        return facts.minimum_duration_months
    if not facts.move_in or not facts.end_date:
        return None
    try:
        start = date.fromisoformat(facts.move_in)
        end_inclusive = date.fromisoformat(facts.end_date)
    except ValueError:
        return None
    target = end_inclusive + timedelta(days=1)
    months = (target.year - start.year) * 12 + (target.month - start.month)
    if target.day < start.day:
        months -= 1
    return max(months, 0)


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
    unavailable_from = config.get("applicant", {}).get("move_in", {}).get("unavailable_from")
    if facts.move_in and unavailable_from:
        try:
            if date.fromisoformat(facts.move_in) >= date.fromisoformat(str(unavailable_from)):
                add_unique(
                    skip, f"availability starts {facts.move_in}, after applicant's useful window"
                )
        except ValueError:
            pass
    if facts.wbs_required:
        add_unique(skip, "WBS required")
    if facts.religion_or_confession_required:
        add_unique(skip, "religion/confession membership required")
    if facts.religious_fraternity_or_membership_required:
        add_unique(skip, "religious Studentenverbindung/fraternity obligations required")
    # Age mismatch is never a hard skip, regardless of how strictly the listing states
    # its preferred range: it is handled entirely as a soft, informational warning
    # below, and the drafted message may acknowledge it once naturally.
    if facts.housing_type == "zwischenmiete":
        # Zwischenmiete is judged by a MAXIMUM duration, not a minimum: a genuinely
        # short-term sublet is the whole point. A longer temporary sublet is only
        # accepted when the rent is cheap enough to justify the shorter commitment
        # regardless of length. When no explicit duration is stated, estimate it from
        # the listing's own move-in/end dates rather than skipping the check entirely.
        if (
            facts.minimum_duration_months is not None
            and facts.minimum_duration_months < rules["min_zwischenmiete_months"]
        ):
            add_unique(skip, f"duration {facts.minimum_duration_months} months below minimum")
        zwischenmiete_duration = zwischenmiete_duration_months(facts)
        max_months = rules["zwischenmiete_max_duration_months"]
        price_exception = float(rules["zwischenmiete_price_exception_below_eur"])
        if (
            zwischenmiete_duration is not None
            and zwischenmiete_duration > max_months
            and not (facts.warm_rent_eur is not None and facts.warm_rent_eur < price_exception)
        ):
            add_unique(
                skip,
                f"Zwischenmiete duration {zwischenmiete_duration} months exceeds the "
                f"{max_months}-month limit (rent not below €{price_exception:g})",
            )
    elif (
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
    if facts.housing_type == "zwischenmiete" and zwischenmiete_duration_months(facts) is None:
        # Only genuinely undated/undurationed Zwischenmiete needs a human look; a
        # duration stated via move-in/end dates is already resolved above.
        add_unique(warnings, "Zwischenmiete duration needs review")
        needs_manual_housing_review = True

    if facts.anmeldung == "no":
        add_unique(warnings, "no Anmeldung")
    fee_threshold = float(rules["warnings"]["one_time_fee_review_at_or_above_eur"])
    effective_one_time_fee = facts.one_time_fee_eur or facts.furniture_takeover_eur
    if effective_one_time_fee is not None and effective_one_time_fee >= fee_threshold:
        add_unique(warnings, f"one-time fee €{effective_one_time_fee:g}")
    if facts.buergschaft_required:
        add_unique(warnings, "Bürgschaft required")
    if facts.parental_guarantor_required and not config["applicant"].get(
        "parental_guarantor_confirmed", False
    ):
        add_unique(warnings, "Elternbürgschaft explicitly required; not confirmed")
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
    if facts.age_mismatch:
        age_range = (
            f"{facts.age_min}-{facts.age_max}" if facts.age_max is not None else f"{facts.age_min}+"
        )
        if not any("soft age mismatch" in warning for warning in warnings):
            add_unique(
                warnings,
                f"soft age mismatch outside {age_range}; acknowledge maturity and fit naturally",
            )

    decision: Decision
    if skip:
        decision = "SKIP"
    elif (
        facts.scam_risk == "medium"
        or facts.unresolved_required_facts
        or (
            facts.parental_guarantor_required
            and not config["applicant"].get("parental_guarantor_confirmed", False)
        )
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
