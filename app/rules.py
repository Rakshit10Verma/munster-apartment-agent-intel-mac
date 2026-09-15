from .schemas import ListingExtraction, RuleDecision

def apply_rules(x, config):
    r = config["housing_rules"]
    skip, warnings = [], []
    effective = None

    if x.women_only: skip.append("women-only / men excluded")
    if x.wbs_required: skip.append("WBS required")
    if x.religion_or_confession_required: skip.append("religion/confession required")
    if x.religious_fraternity_or_membership_required: skip.append("religious/fraternity membership or obligations required")
    if x.explicit_min_age is not None and x.explicit_min_age >= 30 and not x.age_preference_only:
        skip.append(f"mandatory minimum age {x.explicit_min_age}")
    if x.minimum_duration_months is not None and x.minimum_duration_months < r["min_duration_months"]:
        skip.append(f"duration {x.minimum_duration_months} months < {r['min_duration_months']}")
    if x.scam_risk == "high": skip.append("high scam risk")

    if x.warm_rent_eur is not None:
        if x.housing_type == "whole_flat" and r["whole_flat_exception"]["enabled"]:
            if x.realistic_residents:
                effective = round(x.warm_rent_eur/x.realistic_residents,2)
                if effective > r["whole_flat_exception"]["max_effective_warm_rent_per_person_eur"]:
                    skip.append(f"effective whole-flat rent €{effective}/person too high")
            else:
                warnings.append("whole-flat shareability needs review")
        elif x.warm_rent_eur > r["max_warm_rent_single_eur"]:
            skip.append(f"warm rent €{x.warm_rent_eur} > €{r['max_warm_rent_single_eur']}")

    if x.anmeldung == "no": warnings.append("no Anmeldung")
    if x.furniture_takeover_eur and x.furniture_takeover_eur > r["warnings"]["furniture_takeover_over_eur"]:
        warnings.append(f"furniture takeover €{x.furniture_takeover_eur}")
    if x.buergschaft_required: warnings.append("Bürgschaft required")
    if x.indexmiete: warnings.append("Indexmiete")
    if x.hauptmieter_liability: warnings.append("Hauptmieter liability")
    if x.deposit_eur and x.cold_rent_eur and x.deposit_eur/x.cold_rent_eur > r["warnings"]["deposit_over_cold_rent_multiple"]:
        warnings.append("high deposit")

    if skip: decision="SKIP"
    elif x.confidence < .55 or x.needs_cloud: decision="REVIEW"
    else: decision="APPLY"
    return RuleDecision(decision=decision, hard_skip_reasons=skip, warnings=warnings,
                        effective_warm_rent_per_person_eur=effective)
