from __future__ import annotations

import logging
from typing import Any, Literal

from .config_loader import Settings, load_all
from .database import Database
from .documents import decide_attachment
from .prefilter import deterministic_prefilter, stable_id
from .prompts import SYSTEM_PROMPT, combined_prompt
from .providers import ProviderCaller, ProviderError, route_cloud
from .rules import apply_rules
from .schemas import (
    AIAnalysis,
    AnalysisOutcome,
    AttachmentDecision,
    ListingFacts,
    PrefilterResult,
    RuleDecision,
    SourceListing,
    ValidationResult,
)
from .validator import validate_message

logger = logging.getLogger("apartment_agent.analyzer")


def _union_by_id(first: list[Any], second: list[Any]) -> list[Any]:
    merged = {item.id: item for item in second}
    merged.update({item.id: item for item in first})
    return list(merged.values())


def _required_blockers(items: list[str]) -> list[str]:
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
    return [
        item
        for item in items
        if not any(marker in item.casefold() for marker in ordinary_due_diligence)
    ]


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


def reconcile_facts(prefilter: PrefilterResult, ai: AIAnalysis) -> ListingFacts:
    deterministic = prefilter.facts
    updates: dict[str, Any] = {}
    protected_bools = (
        "women_only",
        "wbs_required",
        "religion_or_confession_required",
        "religious_fraternity_or_membership_required",
        "buergschaft_required",
        "indexmiete",
        "hauptmieter_liability",
    )
    for field in protected_bools:
        updates[field] = bool(getattr(deterministic, field) or getattr(ai.facts, field))
    for field in (
        "warm_rent_eur",
        "cold_rent_eur",
        "deposit_eur",
        "furniture_takeover_eur",
        "minimum_duration_months",
        "explicit_min_age",
    ):
        value = getattr(deterministic, field)
        if value is not None:
            updates[field] = value
    if deterministic.housing_type != "unknown":
        updates["housing_type"] = deterministic.housing_type
    if deterministic.advertiser_type != "unknown":
        updates["advertiser_type"] = deterministic.advertiser_type
    updates["listing_language"] = deterministic.listing_language
    updates["hidden_questions"] = _union_by_id(
        deterministic.hidden_questions, ai.facts.hidden_questions
    )
    updates["hidden_commands"] = _union_by_id(
        deterministic.hidden_commands, ai.facts.hidden_commands
    )
    risk_rank = {"low": 0, "medium": 1, "high": 2}
    updates["scam_risk"] = max(
        (deterministic.scam_risk, ai.facts.scam_risk), key=risk_rank.__getitem__
    )
    updates["scam_reasons"] = list(
        dict.fromkeys(deterministic.scam_reasons + ai.facts.scam_reasons)
    )
    updates["ambiguities"] = list(dict.fromkeys(deterministic.ambiguities + ai.facts.ambiguities))
    updates["critical_ambiguities"] = list(
        dict.fromkeys(deterministic.critical_ambiguities + ai.facts.critical_ambiguities)
    )
    updates["mandatory_incompatibilities"] = list(
        dict.fromkeys(
            deterministic.mandatory_incompatibilities + ai.facts.mandatory_incompatibilities
        )
    )
    updates["odd_fee_or_payment_demands"] = list(
        dict.fromkeys(
            deterministic.odd_fee_or_payment_demands + ai.facts.odd_fee_or_payment_demands
        )
    )
    if deterministic.contact_email:
        updates["contact_email"] = deterministic.contact_email
        updates["contact_method"] = "email"
    updates["unresolved_required_facts"] = _required_blockers(
        deterministic.unresolved_required_facts + ai.facts.unresolved_required_facts
    )
    return ai.facts.model_copy(update=updates)


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
        provider_succeeded=False,
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
) -> AnalysisOutcome:
    if config is None or answers is None or settings is None:
        loaded_config, loaded_answers, loaded_settings = load_all()
        config = config or loaded_config
        answers = answers or loaded_answers
        settings = settings or loaded_settings
    database = database or Database(settings.database_path)
    listing_db_id, _ = database.discover(listing)
    prefilter = deterministic_prefilter(listing, config)
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
        call, notes = route_cloud(
            SYSTEM_PROMPT,
            combined_prompt(listing, prefilter, config, answers),
            settings,
            callers,
        )
    except ProviderError as exc:
        decision = RuleDecision(decision="REVIEW", warnings=prefilter.warnings)
        outcome = AnalysisOutcome(
            listing=listing,
            facts=prefilter.facts,
            rule_decision=decision,
            status="ai_failed",
            validation=ValidationResult(auto_send_allowed=False, errors=["provider failure"]),
            router_notes=[str(exc)],
            database_id=listing_db_id,
        )
        database.save_outcome(outcome, error=str(exc))
        logger.error(
            "all AI providers failed",
            extra={"fields": {"listing_id": listing_db_id, "error": str(exc)}},
        )
        return outcome

    ai = canonicalize_hidden_ids(call.data)
    facts = reconcile_facts(prefilter, ai)
    decision = apply_rules(facts, config, prefilter)
    attachment = decide_attachment(listing, facts, settings, config)
    draft = ai.message.model_copy(
        update={
            "unresolved_required_facts": _required_blockers(ai.message.unresolved_required_facts)
        }
    )
    validation = validate_message(
        listing,
        facts,
        decision,
        draft,
        attachment,
        config,
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
