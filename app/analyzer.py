from __future__ import annotations

import logging
import re
from typing import Any, Literal

from .config_loader import Settings, load_all
from .database import Database
from .documents import decide_attachment, validate_applicant_photo
from .message_policy import enforce_message_policy
from .prefilter import (
    LATER_STAGE_DOCUMENT_LABELS,
    dedupe_similar,
    deterministic_prefilter,
    optional_example_question,
    stable_id,
)
from .prompts import SYSTEM_PROMPT, combined_prompt
from .providers import ProviderCaller, ProviderError, route_cloud
from .rules import apply_rules
from .schemas import (
    AIAnalysis,
    AnalysisOutcome,
    AttachmentDecision,
    GenerationTrace,
    HiddenAnswerTrace,
    ListingFacts,
    PrefilterResult,
    RuleDecision,
    SourceListing,
    ValidationResult,
)
from .universal_fallback import build_deterministic_fallback
from .validator import validate_message

logger = logging.getLogger("apartment_agent.analyzer")


def _union_by_id(first: list[Any], second: list[Any]) -> list[Any]:
    merged = {item.id: item for item in second}
    merged.update({item.id: item for item in first})
    return list(merged.values())


# Maps a Bewerbermappe content-list label to the wording an AI note might use to
# describe uncertainty about it. Only documents actually configured as available are
# ever treated as "known" here -- nothing is fabricated.
_DOCUMENT_KEYWORD_ALIASES: dict[str, tuple[str, ...]] = {
    "immatrikulationsbescheinigung": (
        "immatrikulationsbescheinigung",
        "immatrikulation",
        "enrollment certificate",
        "enrollment confirmation",
        "certificate of enrollment",
        "studienbescheinigung",
    ),
    "schufa": ("schufa",),
    "mietschuldenfreiheitsbescheinigung": ("mietschuldenfreiheitsbescheinigung", "mietschulden"),
    "salary slip": (
        "salary slip",
        "gehaltsnachweis",
        "gehaltsabrechnung",
        "verdienstbescheinigung",
    ),
}


def _configured_document_keywords(config: dict[str, Any]) -> tuple[str, ...]:
    bewerbermappe = config.get("documents", {}).get("wg_gesucht_bewerbermappe", {})
    if not bewerbermappe.get("available"):
        return ()
    keywords: list[str] = []
    for content in bewerbermappe.get("contents", []):
        keywords.extend(_DOCUMENT_KEYWORD_ALIASES.get(str(content).casefold(), (str(content),)))
    return tuple(dict.fromkeys(keyword.casefold() for keyword in keywords))


def _required_blockers(
    items: list[str],
    config: dict[str, Any],
    later_requirements: list[str] | None = None,
    facts: ListingFacts | None = None,
    photo_available: bool = False,
) -> list[str]:
    ordinary_due_diligence = (
        "kaution",
        "kaltmiete",
        "nebenkosten",
        "mietaufteilung",
        "ausstattung",
        "genaue lage",
        "jahr des einzugs",
        "jahr des einzugstermins",
        "year of the move-in",
        "utility breakdown",
    )
    available_documents = _configured_document_keywords(config)
    later_keywords = tuple(
        keyword
        for keyword, label in LATER_STAGE_DOCUMENT_LABELS
        if label in (later_requirements or [])
    )
    return [
        item
        for item in items
        if not any(marker in item.casefold() for marker in ordinary_due_diligence)
        and not any(keyword in item.casefold() for keyword in available_documents)
        and not any(keyword in item.casefold() for keyword in later_keywords)
        and not _confirmed_profile_resolves(item, config, facts, photo_available)
        and not _optional_personal_note(item, facts)
    ]


def _optional_personal_note(item: str, facts: ListingFacts | None) -> bool:
    """A missing answer to an optional WG-life example is not a send blocker."""
    if facts is None:
        return False
    optional = [
        question.question.casefold() for question in facts.hidden_questions if not question.required
    ]
    if not optional:
        return False
    lower = item.casefold()
    experience = (
        r"(?:wg[- ]?erfahrung|(?:in\s+einer\s+wg|flatshare).{0,30}gewohnt|"
        r"shared.flat.experience)"
    )
    preferences = (
        r"(?:zusammenwohn\w*|wg[- ]?leben|cohabitation).{0,80}"
        r"(?:wichtig|important|weniger|less)"
    )
    if re.search(experience, lower) and any(
        re.search(r"\bwg\b.{0,45}gewohnt|wg[- ]?erfahrung|flatshare", question)
        for question in optional
    ):
        return True
    return bool(re.search(preferences, lower)) and any(
        re.search(r"zusammenwohn\w*|zusammen\s+wohn\w*|wichtig|important", question)
        for question in optional
    )


def _confirmed_profile_resolves(
    item: str, config: dict[str, Any], facts: ListingFacts | None, photo_available: bool
) -> bool:
    """Remove only provider uncertainty answered by an explicit profile fact."""
    lower = item.casefold()
    profile = config.get("applicant", {})
    if "semester" in lower and profile.get("degree_duration_semesters") == 4:
        if re.search(r"\b(?:5|6|7|8|fünf|sechs|sieben|acht)\b", lower):
            return False
        return bool(re.search(r"\b(?:4|vier|dauer|lang|mindestens)\b", lower))
    if "haftpflicht" in lower and profile.get("private_liability_insurance") is True:
        # Having insurance does not establish possession of a policy document.
        return not bool(re.search(r"nachweis|bescheinigung|police|dokument|anhäng|beifüg", lower))
    if re.search(r"haustier|pets?|hunde?", lower):
        pets = profile.get("pets", {})
        return pets.get("owns_pets") is False and pets.get("comfortable_living_with_pets") is True
    if re.search(r"bürgschaft|buergschaft|guarant", lower):
        return bool(
            facts
            and facts.income_or_guarantor_accepted
            and profile.get("regular_own_income") is True
            and "salary slip"
            in config.get("documents", {}).get("wg_gesucht_bewerbermappe", {}).get("contents", [])
        )
    if re.search(r"foto|photo", lower):
        return bool(facts and facts.photo_required_with_first_message and photo_available)
    return False


def _is_soft_age_mismatch_note(item: str) -> bool:
    lower = item.casefold()
    return bool(
        any(marker in lower for marker in ("alter", "age", "jünger", "younger"))
        or re.search(r"\b2[12]\b.{0,50}\b(?:25|30|35)\b", lower)
    )


def _is_zwischenmiete_duration_note(item: str) -> bool:
    """An AI note second-guessing whether the applicant accepts a temporary
    sublet's duration. Whether to apply to a given Zwischenmiete duration is already
    a deterministic policy decision (rules.apply_rules); if that policy accepts it,
    the AI re-litigating the same question must not block APPLY."""
    lower = item.casefold()
    return bool(
        "zwischenmiete" in lower
        and re.search(r"akzeptieren|befristung|befristet|begrenzt|langfristig|längerfristig", lower)
    )


def _is_benign_fee_note(item: str) -> bool:
    """Identify an AI note that refers only to an ordinary one-time fee.

    This is used only after the deterministic parser has established a known total
    below the configured limit. Explicit scam mechanics are never normalized away.
    """
    lower = item.casefold()
    dangerous = re.search(
        r"western union|moneygram|krypto|crypto|bitcoin|vor (?:der )?besichtigung|"
        r"before (?:the )?viewing|schlüssel.{0,20}post|keys?.{0,20}(?:mail|courier)|"
        r"vermieter.{0,30}ausland|landlord.{0,30}abroad",
        lower,
    )
    fee_related = re.search(
        r"ablöse|abloese|abschlag|gebühr|gebuehr|fee|möbel|moebel|furniture|"
        r"zubehör|zubehoer|accessor|inventar|einrichtung|one.time|kaution|deposit",
        lower,
    )
    return bool(fee_related and not dangerous)


def canonicalize_hidden_ids(ai: AIAnalysis) -> AIAnalysis:
    question_map = {
        question.id: stable_id("q", question.question) for question in ai.facts.hidden_questions
    }
    command_map = {
        command.id: stable_id("cmd", command.instruction) for command in ai.facts.hidden_commands
    }
    questions = [
        question.model_copy(update={"id": question_map[question.id]})
        for question in ai.facts.hidden_questions
    ]
    commands = [
        command.model_copy(update={"id": command_map[command.id]})
        for command in ai.facts.hidden_commands
    ]
    facts = ai.facts.model_copy(update={"hidden_questions": questions, "hidden_commands": commands})
    message = ai.message.model_copy(
        update={
            "answered_question_ids": [
                question_map.get(identifier, identifier)
                for identifier in ai.message.answered_question_ids
            ],
            "applied_command_ids": [
                command_map.get(identifier, identifier)
                for identifier in ai.message.applied_command_ids
            ],
        }
    )
    return ai.model_copy(update={"facts": facts, "message": message})


def reconcile_facts(
    prefilter: PrefilterResult,
    ai: AIAnalysis,
    config: dict[str, Any],
    photo_available: bool = False,
    listing_text: str = "",
) -> tuple[ListingFacts, dict[str, str]]:
    deterministic = prefilter.facts
    updates: dict[str, Any] = {}
    protected_bools = (
        "women_only",
        "wbs_required",
        "religion_or_confession_required",
        "religious_fraternity_or_membership_required",
        "buergschaft_required",
        "parental_guarantor_required",
        "income_or_guarantor_accepted",
        "photo_required_with_first_message",
        "indexmiete",
        "hauptmieter_liability",
    )
    for field in protected_bools:
        updates[field] = bool(getattr(deterministic, field) or getattr(ai.facts, field))
    # A model must not turn mere Studentenverbindung residence into a hard skip.
    # Only explicit membership/confession evidence extracted from the ad can do so.
    updates["religion_or_confession_required"] = deterministic.religion_or_confession_required
    updates["religious_fraternity_or_membership_required"] = (
        deterministic.religious_fraternity_or_membership_required
    )
    # Only the listing-text parser may authorize sharing a separate personal photo
    # or identify an explicitly accepted income/guarantor alternative.
    updates["photo_required_with_first_message"] = deterministic.photo_required_with_first_message
    updates["income_or_guarantor_accepted"] = deterministic.income_or_guarantor_accepted
    updates["parental_guarantor_required"] = deterministic.parental_guarantor_required
    for field in (
        "warm_rent_eur",
        "cold_rent_eur",
        "deposit_eur",
        "furniture_takeover_eur",
        "one_time_fee_eur",
        "minimum_duration_months",
        "explicit_min_age",
        "age_min",
        "age_max",
    ):
        value = getattr(deterministic, field)
        if value is not None:
            updates[field] = value
    if deterministic.housing_type != "unknown":
        updates["housing_type"] = deterministic.housing_type
    if deterministic.location:
        updates["location"] = deterministic.location
    if deterministic.furnishing != "unknown":
        updates["furnishing"] = deterministic.furnishing
    if deterministic.advertiser_type != "unknown":
        updates["advertiser_type"] = deterministic.advertiser_type
    updates["later_contract_requirements"] = deterministic.later_contract_requirements
    updates["listing_language"] = deterministic.listing_language
    if deterministic.age_requirement_strength != "unknown":
        updates["age_requirement_strength"] = deterministic.age_requirement_strength
        updates["age_mismatch"] = deterministic.age_mismatch
        updates["age_preference_only"] = deterministic.age_preference_only
    merged_questions = _union_by_id(deterministic.hidden_questions, ai.facts.hidden_questions)
    # A house rule ("Halte dich an den Putzplan") is not an exact instruction
    # for the application message. Cloud extraction called this an uncheckable
    # `other` command in DB 137, needlessly blocking an otherwise valid draft.
    ai_application_commands = [
        item
        for item in ai.facts.hidden_commands
        if item.kind != "other"
        or re.search(
            r"nachricht|bewerbung|anschreiben|betreff|message|application|"
            r"schick|send|schreib|antwort|nenn|erwähn|include|write|mention",
            item.instruction,
            re.I,
        )
    ]
    merged_commands = _union_by_id(deterministic.hidden_commands, ai_application_commands)
    # A cloud provider paraphrasing a listing sentence gets a different stable-hash id
    # than the deterministic extractor's exact-text id for the same underlying
    # question/instruction; collapse those near-duplicates so the validator does not
    # see one answered id and one unanswered "duplicate" of it.
    questions, question_remap = dedupe_similar(
        merged_questions, text_of=lambda q: q.question, id_of=lambda q: q.id
    )
    questions = [
        question.model_copy(update={"required": False})
        if optional_example_question(question.question, listing_text)
        else question
        for question in questions
    ]
    optional_facts = deterministic.model_copy(update={"hidden_questions": questions})
    commands, command_remap = dedupe_similar(
        merged_commands, text_of=lambda c: c.instruction, id_of=lambda c: c.id
    )
    dropped_house_rule_ids = {
        item.id: "" for item in ai.facts.hidden_commands if item not in ai_application_commands
    }
    id_remap = {**question_remap, **command_remap, **dropped_house_rule_ids}
    updates["hidden_questions"] = questions
    updates["hidden_commands"] = commands
    fee_threshold = float(
        config["housing_rules"]["warnings"]["one_time_fee_review_at_or_above_eur"]
    )
    # True whenever the deterministic parser independently found nothing concerning
    # about fees/deposit -- either a known total safely below the threshold, or no
    # priced fee at all (e.g. a normal, unpriced, negotiable furniture takeover). An
    # AI provider can still reach the same "unclear/unpriced amount" observation on
    # its own and misjudge it as suspicious; this lets that specific misjudgment be
    # normalized away without ever touching a genuine scam signal (those never match
    # _is_benign_fee_note's "not dangerous" check).
    accepted_deterministic_fee = bool(
        deterministic.scam_risk == "low"
        and not deterministic.odd_fee_or_payment_demands
        and (
            deterministic.one_time_fee_eur is None or deterministic.one_time_fee_eur < fee_threshold
        )
    )
    ai_scam_reasons = list(ai.facts.scam_reasons)
    ai_fee_demands = list(ai.facts.odd_fee_or_payment_demands)
    removed_benign_fee_note = False
    if accepted_deterministic_fee:
        kept_reasons = [item for item in ai_scam_reasons if not _is_benign_fee_note(item)]
        kept_demands = [item for item in ai_fee_demands if not _is_benign_fee_note(item)]
        removed_benign_fee_note = len(kept_reasons) != len(ai_scam_reasons) or len(
            kept_demands
        ) != len(ai_fee_demands)
        ai_scam_reasons = kept_reasons
        ai_fee_demands = kept_demands

    risk_rank = {"low": 0, "medium": 1, "high": 2}
    ai_scam_risk = ai.facts.scam_risk
    if (
        ai_scam_risk == "medium"
        and removed_benign_fee_note
        and not ai_scam_reasons
        and not ai_fee_demands
    ):
        ai_scam_risk = "low"
    updates["scam_risk"] = max((deterministic.scam_risk, ai_scam_risk), key=risk_rank.__getitem__)
    updates["scam_reasons"] = list(dict.fromkeys(deterministic.scam_reasons + ai_scam_reasons))
    updates["ambiguities"] = list(dict.fromkeys(deterministic.ambiguities + ai.facts.ambiguities))
    ai_critical_ambiguities = list(ai.facts.critical_ambiguities)
    if (
        ai.facts.religion_or_confession_required
        and not deterministic.religion_or_confession_required
    ):
        ai_critical_ambiguities.append(
            "AI suspected a confession requirement not confirmed by deterministic listing text"
        )
    if (
        ai.facts.religious_fraternity_or_membership_required
        and not deterministic.religious_fraternity_or_membership_required
    ):
        ai_critical_ambiguities.append(
            "AI suspected compulsory fraternity membership not confirmed by listing text"
        )
    ai_mandatory_incompatibilities = list(ai.facts.mandatory_incompatibilities)
    ai_mandatory_incompatibilities = [
        item
        for item in ai_mandatory_incompatibilities
        if not _confirmed_profile_resolves(item, config, deterministic, photo_available)
        and not (
            deterministic.parental_guarantor_required
            and re.search(r"elternbürgschaft|parental guarant", item, re.I)
        )
    ]
    if not deterministic.religious_fraternity_or_membership_required:
        speculative_membership = [
            item
            for item in ai_mandatory_incompatibilities
            if re.search(
                r"studentenverbindung|verbindungsmitglied|fraternity|membership|mitgliedschaft",
                item,
                re.I,
            )
        ]
        ai_mandatory_incompatibilities = [
            item for item in ai_mandatory_incompatibilities if item not in speculative_membership
        ]
        ai_critical_ambiguities.extend(speculative_membership)
    ai_unresolved_required_facts = list(ai.facts.unresolved_required_facts)
    ai_unresolved_required_facts = [
        item
        for item in ai_unresolved_required_facts
        if not _optional_personal_note(item, optional_facts)
    ]
    ai_mandatory_incompatibilities = [
        item
        for item in ai_mandatory_incompatibilities
        if not _optional_personal_note(item, optional_facts)
    ]
    available_documents = _configured_document_keywords(config)
    if available_documents:
        ai_critical_ambiguities = [
            item
            for item in ai_critical_ambiguities
            if not any(keyword in item.casefold() for keyword in available_documents)
        ]
    if deterministic.later_contract_requirements:
        # The listing text deterministically places these documents at contract
        # stage ("zum Mietvertrag ..."), not before/at first contact. An AI note
        # escalating "unclear if Rakshit can provide X" into a pre-contact blocker
        # for one of these same documents is simply wrong about the stage and must
        # not block APPLY; the requirement itself is still shown, just informationally
        # (see later_contract_requirements), never silently claimed as satisfied.
        later_stage_keywords = tuple(
            keyword
            for keyword, label in LATER_STAGE_DOCUMENT_LABELS
            if label in deterministic.later_contract_requirements
        )
        ai_critical_ambiguities = [
            item
            for item in ai_critical_ambiguities
            if not any(keyword in item.casefold() for keyword in later_stage_keywords)
        ]
        ai_unresolved_required_facts = [
            item
            for item in ai_unresolved_required_facts
            if not any(keyword in item.casefold() for keyword in later_stage_keywords)
        ]
        ai_mandatory_incompatibilities = [
            item
            for item in ai_mandatory_incompatibilities
            if not any(keyword in item.casefold() for keyword in later_stage_keywords)
        ]
    if deterministic.housing_type == "zwischenmiete":
        # Whether to apply to this Zwischenmiete's duration is already a
        # deterministic policy decision (rules.apply_rules); an AI note that just
        # re-asks the same question must not independently block APPLY. If the
        # duration is genuinely unacceptable, the deterministic hard skip already
        # covers that regardless of this note.
        ai_critical_ambiguities = [
            item for item in ai_critical_ambiguities if not _is_zwischenmiete_duration_note(item)
        ]
        ai_mandatory_incompatibilities = [
            item
            for item in ai_mandatory_incompatibilities
            if not _is_zwischenmiete_duration_note(item)
        ]
        ai_unresolved_required_facts = [
            item
            for item in ai_unresolved_required_facts
            if not _is_zwischenmiete_duration_note(item)
        ]
    if accepted_deterministic_fee:
        ai_critical_ambiguities = [
            item for item in ai_critical_ambiguities if not _is_benign_fee_note(item)
        ]
        ai_mandatory_incompatibilities = [
            item for item in ai_mandatory_incompatibilities if not _is_benign_fee_note(item)
        ]
        ai_unresolved_required_facts = [
            item for item in ai_unresolved_required_facts if not _is_benign_fee_note(item)
        ]
    critical_ambiguities = deterministic.critical_ambiguities + ai_critical_ambiguities
    mandatory_incompatibilities = (
        deterministic.mandatory_incompatibilities + ai_mandatory_incompatibilities
    )
    if deterministic.age_mismatch:
        critical_ambiguities = [
            item for item in critical_ambiguities if not _is_soft_age_mismatch_note(item)
        ]
        mandatory_incompatibilities = [
            item for item in mandatory_incompatibilities if not _is_soft_age_mismatch_note(item)
        ]
    updates["critical_ambiguities"] = list(dict.fromkeys(critical_ambiguities))
    updates["critical_ambiguities"] = [
        item
        for item in updates["critical_ambiguities"]
        if not _confirmed_profile_resolves(item, config, deterministic, photo_available)
        and not _optional_personal_note(item, optional_facts)
    ]
    updates["mandatory_incompatibilities"] = list(dict.fromkeys(mandatory_incompatibilities))
    updates["odd_fee_or_payment_demands"] = list(
        dict.fromkeys(deterministic.odd_fee_or_payment_demands + ai_fee_demands)
    )
    if deterministic.contact_email:
        updates["contact_email"] = deterministic.contact_email
        updates["contact_method"] = "email"
    updates["unresolved_required_facts"] = _required_blockers(
        deterministic.unresolved_required_facts + ai_unresolved_required_facts,
        config,
        deterministic.later_contract_requirements,
        optional_facts,
        photo_available,
    )
    return ai.facts.model_copy(update=updates), id_remap


def _skip_outcome(
    listing: SourceListing, prefilter: PrefilterResult, config: dict[str, Any]
) -> AnalysisOutcome:
    decision = RuleDecision(
        decision="SKIP",
        hard_skip_reasons=prefilter.hard_skip_reasons,
        warnings=prefilter.warnings,
    )
    validation = validate_message(
        listing,
        prefilter.facts,
        decision,
        None,
        AttachmentDecision(),
        config,
    )
    return AnalysisOutcome(
        listing=listing,
        facts=prefilter.facts,
        rule_decision=decision,
        status="filtered_skip",
        validation=validation,
    )


def process_listing(
    listing: SourceListing,
    *,
    config: dict[str, Any] | None = None,
    answers: dict[str, Any] | None = None,
    settings: Settings | None = None,
    database: Database | None = None,
    callers: dict[str, ProviderCaller] | None = None,
    force_fallback: bool = False,
) -> AnalysisOutcome:
    """`force_fallback=True` (the ./reprocess.sh --fallback-only diagnostic) makes
    zero cloud-provider calls and exercises exactly the same deterministic-fallback
    path production uses on a real outage, so the fallback can be tested against
    stored listings independent of live provider availability."""
    if config is None or answers is None or settings is None:
        loaded_config, loaded_answers, loaded_settings = load_all()
        config = config or loaded_config
        answers = answers or loaded_answers
        settings = settings or loaded_settings
    database = database or Database(settings.database_path)
    listing_db_id, _ = database.discover(listing)
    prefilter = deterministic_prefilter(listing, config)
    photo_available = False
    if prefilter.facts.photo_required_with_first_message:
        photo_error = validate_applicant_photo(settings.applicant_photo_path, config)
        photo_available = photo_error is None
        if photo_error:
            prefilter.facts.unresolved_required_facts.append(photo_error)
    wg_state = listing.source_metadata.get("wg_contact_state", "")
    if wg_state == "already_contacted":
        decision = RuleDecision(
            decision="REVIEW",
            warnings=["existing WG conversation detected before AI"],
        )
        outcome = AnalysisOutcome(
            listing=listing,
            facts=prefilter.facts,
            rule_decision=decision,
            status="already_contacted",
            validation=ValidationResult(
                auto_send_allowed=False, errors=["listing was already contacted"]
            ),
            router_notes=[listing.source_metadata.get("wg_contact_state_detail", "")],
            database_id=listing_db_id,
        )
        outcome.database_id = database.save_outcome(outcome)
        return outcome
    if wg_state == "unavailable":
        prefilter.hard_skip_reasons.append("WG listing is no longer available")
    if not prefilter.viable:
        outcome = _skip_outcome(listing, prefilter, config)
        outcome.database_id = database.save_outcome(outcome)
        logger.info(
            "listing filtered before AI",
            extra={
                "fields": {
                    "listing_id": listing_db_id,
                    "reasons": prefilter.hard_skip_reasons,
                }
            },
        )
        return outcome

    try:
        if force_fallback:
            raise ProviderError(
                "forced fallback diagnostic (--fallback-only): no cloud provider was called"
            )
        call, notes = route_cloud(
            SYSTEM_PROMPT,
            combined_prompt(listing, prefilter, config, answers),
            settings,
            callers,
        )
    except ProviderError as exc:
        fallback = build_deterministic_fallback(listing, prefilter, config, answers)
        draft = fallback.draft
        resolved_questions = fallback.resolved_questions
        fallback_blockers = fallback.blockers
        message_source: Literal["universal_fallback", "universal_answer_bank_fallback"] = (
            "universal_answer_bank_fallback"
            if prefilter.facts.hidden_questions or prefilter.facts.hidden_commands
            else "universal_fallback"
        )
        generation_trace = GenerationTrace(
            ai_attempted=not force_fallback,
            ai_failure_reason=None if force_fallback else str(exc),
            ai_skip_reason=str(exc) if force_fallback else None,
            fallback_used=draft is not None,
            fallback_template=fallback.template,
            fallback_scenario=fallback.scenario,
            optional_clauses=fallback.optional_clauses,
            hidden_question_answers=[
                HiddenAnswerTrace(
                    question_id=item.question_id,
                    category=item.question_category,
                    answer_source=item.answer_source,
                    answer=item.resolved_answer,
                )
                for item in resolved_questions
            ],
        )
        if draft is not None:
            facts = prefilter.facts
            decision = apply_rules(facts, config, prefilter)
            attachment = decide_attachment(listing, facts, settings, config)
            validation = validate_message(
                listing,
                facts,
                decision,
                draft,
                attachment,
                config,
                message_source=message_source,
            )
            fallback_status: Literal["drafted", "review_required", "filtered_skip"] = (
                "drafted"
                if decision.decision == "APPLY" and validation.auto_send_allowed
                else "review_required"
            )
            if decision.decision == "SKIP":
                fallback_status = "filtered_skip"
            router_notes = [str(exc), "all providers failed; approved universal fallback used"]
            if resolved_questions:
                router_notes.append(
                    "answer-bank resolved questions: "
                    + ", ".join(
                        f"{item.question_id}={item.question_category}"
                        for item in resolved_questions
                    )
                )
            outcome = AnalysisOutcome(
                listing=listing,
                facts=facts,
                rule_decision=decision,
                status=fallback_status,
                message=draft,
                attachment=attachment,
                validation=validation,
                router_notes=router_notes,
                generation_trace=generation_trace,
                message_source=message_source,
                database_id=listing_db_id,
            )
            outcome.database_id = database.save_outcome(outcome)
            logger.warning(
                "cloud providers failed; fallback validated",
                extra={
                    "fields": {
                        "listing_id": listing_db_id,
                        "status": fallback_status,
                        "message_source": message_source,
                        "answer_bank_categories": [
                            item.question_category for item in resolved_questions
                        ],
                    }
                },
            )
            return outcome
        decision = RuleDecision(
            decision="REVIEW",
            warnings=prefilter.warnings
            + ["universal fallback blocked: " + "; ".join(fallback_blockers)],
        )
        outcome = AnalysisOutcome(
            listing=listing,
            facts=prefilter.facts,
            rule_decision=decision,
            status="review_required",
            validation=ValidationResult(
                auto_send_allowed=False,
                errors=["provider failure; universal fallback safety conditions not met"],
            ),
            router_notes=[str(exc), *fallback_blockers],
            generation_trace=generation_trace,
            message_source="none",
            database_id=listing_db_id,
        )
        database.save_outcome(outcome, error=str(exc))
        logger.error(
            "all AI providers failed; universal fallback blocked",
            extra={"fields": {"listing_id": listing_db_id, "error": str(exc)}},
        )
        return outcome

    ai = canonicalize_hidden_ids(call.data)
    facts, id_remap = reconcile_facts(prefilter, ai, config, photo_available, listing.raw_text)
    decision = apply_rules(facts, config, prefilter)
    attachment = decide_attachment(listing, facts, settings, config)
    draft = ai.message.model_copy(
        update={
            "answered_question_ids": [
                id_remap.get(identifier, identifier)
                for identifier in ai.message.answered_question_ids
            ],
            "applied_command_ids": [
                id_remap.get(identifier, identifier)
                for identifier in ai.message.applied_command_ids
                if id_remap.get(identifier, identifier)
            ],
            "unresolved_required_facts": _required_blockers(
                ai.message.unresolved_required_facts,
                config,
                facts.later_contract_requirements,
                facts,
                photo_available,
            ),
        }
    )
    draft = enforce_message_policy(listing, facts, draft)
    validation = validate_message(
        listing,
        facts,
        decision,
        draft,
        attachment,
        config,
        message_source="cloud_ai",
    )
    status: Literal["drafted", "review_required", "filtered_skip"] = (
        "drafted"
        if decision.decision == "APPLY" and validation.auto_send_allowed
        else "review_required"
    )
    if decision.decision == "SKIP":
        status = "filtered_skip"
    outcome = AnalysisOutcome(
        listing=listing,
        facts=facts,
        rule_decision=decision,
        status=status,
        message=draft,
        provider=call.metadata,
        attachment=attachment,
        validation=validation,
        router_notes=notes,
        generation_trace=GenerationTrace(ai_attempted=True),
        message_source="cloud_ai",
        database_id=listing_db_id,
    )
    outcome.database_id = database.save_outcome(outcome)
    logger.info(
        "listing analyzed",
        extra={
            "fields": {
                "listing_id": listing_db_id,
                "status": outcome.status,
                "provider": call.metadata.provider,
                "model": call.metadata.model,
                "latency_ms": call.metadata.latency_ms,
            }
        },
    )
    return outcome


def analyze(text: str, platform: str = "unknown") -> AnalysisOutcome:
    return process_listing(SourceListing(platform=platform, raw_text=text))  # type: ignore[arg-type]
