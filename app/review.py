from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .config_loader import ROOT
from .database import has_send_evidence
from .prefilter import optional_example_question
from .schemas import (
    AnalysisOutcome,
    AttachmentDecision,
    ContactResult,
    GenerationTrace,
    ListingFacts,
    MessageDraft,
    RuleDecision,
    SendTrigger,
    SourceListing,
    ValidationResult,
)

# Order matches the grouping requested for ./review.sh.
REVIEW_GROUPS: tuple[tuple[str, str], ...] = (
    ("review_required", "REVIEW REQUIRED"),
    ("send_state_unknown", "SEND STATE UNKNOWN"),
    ("ai_failed", "AI FAILED"),
    ("premium_boost_failed", "PREMIUM BOOST FAILED"),
    ("bewerbermappe_attachment_failed", "BEWERBERMAPPE ATTACHMENT FAILED"),
    ("drafted", "DRAFTED BUT NOT SENT"),
)

# send_state_unknown is deliberately excluded: it means a Send may already have
# happened, so it must be reconciled (./reconcile.sh), never approved as a fresh send.
APPROVABLE_STATUSES = frozenset(
    {
        "review_required",
        "ai_failed",
        "premium_boost_failed",
        "drafted",
        "bewerbermappe_attachment_failed",
    }
)

SEND_STATE_UNKNOWN_MESSAGE = (
    "This listing has an unresolved previous send attempt. "
    "Reconcile it before attempting another send."
)

# A listing whose send state is settled or ambiguous must never be silently
# reanalyzed and re-drafted, since that could otherwise be followed by a duplicate
# send attempt. send_state_unknown specifically needs reconciliation, not reanalysis.
REPROCESS_FORBIDDEN_STATUSES = frozenset({"sent", "already_contacted", "send_state_unknown"})

# A listing with a settled or ambiguous send state must never be overwritten by a
# manual rejection -- that would corrupt real send history (or hide an unresolved
# send that still needs ./reconcile.sh). Shared by ./reject.sh and the Telegram
# Reject button so neither trigger can mark a sent listing as rejected.
NON_REJECTABLE_STATUSES = frozenset({"sent", "already_contacted", "send_state_unknown"})


@dataclass
class ListingRecord:
    row: dict[str, Any]
    facts: ListingFacts
    decision: RuleDecision
    validation: ValidationResult
    attachment: AttachmentDecision
    generation_trace: GenerationTrace
    message: MessageDraft | None
    router_notes: list[str] = field(default_factory=list)

    @property
    def id(self) -> int:
        return int(self.row["id"])

    @property
    def status(self) -> str:
        return str(self.row["status"])

    @property
    def reasons(self) -> list[str]:
        """Detailed, technical reasons -- used as `review_reason_debug`."""
        reasons = list(self.validation.errors)
        if self.decision.hard_skip_reasons:
            reasons.extend(self.decision.hard_skip_reasons)
        if self.facts.unresolved_required_facts:
            reasons.append(
                "unresolved required facts: " + ", ".join(self.facts.unresolved_required_facts)
            )
        if self.facts.critical_ambiguities:
            reasons.append("critical ambiguities: " + ", ".join(self.facts.critical_ambiguities))
        reasons.extend(self.router_notes)
        if self.row.get("error"):
            reasons.append(str(self.row["error"])[:300])
        if not reasons and self.decision.decision != "APPLY":
            reasons.append(f"rule decision is {self.decision.decision}, not APPLY")
        return list(dict.fromkeys(reasons)) or ["no specific reason recorded"]

    @property
    def warnings(self) -> list[str]:
        return list(dict.fromkeys([*self.decision.warnings, *self.validation.warnings]))

    @property
    def review_reason_debug(self) -> str:
        return "; ".join(self.reasons)

    @property
    def review_reason_human(self) -> str:
        """A short, human-readable summary for review.sh/dashboard. The full
        technical detail remains available via review_reason_debug/reasons."""
        return humanize_review_reason(self.reasons)


def _safe_model(model_cls: Any, raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return model_cls.model_validate_json(raw)
    except Exception:
        return default


# Ordered (marker, human phrase) pairs: the first matching marker wins. Built from the
# actual blocker/error strings produced by universal_fallback.py and validator.py.
_HUMAN_REASON_RULES: tuple[tuple[str, str], ...] = (
    (
        "scam or unusual-payment ambiguity",
        "the payment/fee terms were classified as potentially unsafe",
    ),
    (
        "hidden question(s) cannot be resolved",
        "a listing question needs a human answer",
    ),
    (
        "hidden command(s) cannot be deterministically applied",
        "an application instruction could not be resolved",
    ),
    (
        "unclassified special application instruction",
        "an application instruction could not be resolved",
    ),
    ("unclassified listing question", "a listing question could not be classified"),
    (
        "no deterministic template scenario",
        "this type of listing isn't supported by the automatic fallback yet",
    ),
    (
        "listing type is not supported by deterministic fallback",
        "this type of listing isn't supported by the automatic fallback yet",
    ),
    (
        "hard housing eligibility is not deterministically clear",
        "the rent could not be determined from the listing",
    ),
    ("applicant profile lacks", "an internal profile configuration issue"),
    (
        "unresolved eligibility ambiguity",
        "some eligibility details are unclear or contradictory",
    ),
    (
        "deterministic fallback is currently limited to wg-gesucht",
        "automatic fallback isn't available for this platform yet",
    ),
    ("deterministic hard skip", "a hard eligibility rule was triggered"),
    ("high scam risk", "this listing was flagged as a likely scam"),
    (
        "critical listing ambiguities",
        "some listing details are unclear or contradictory",
    ),
    (
        "unresolved required listing facts",
        "some required facts about the listing are unclear",
    ),
    (
        "unresolved required personal facts",
        "the message needed a personal fact that wasn't available",
    ),
    ("wrong register", "the message used the wrong formal/informal tone for this listing"),
    (
        "already contacted",
        "WG-Gesucht shows an existing conversation with this listing",
    ),
    (
        "existing wg conversation",
        "WG-Gesucht shows an existing conversation with this listing",
    ),
)
_AI_OUTAGE_PATTERN = re.compile(
    r"all configured cloud providers failed|provider failure|temporarily skipped"
)


def humanize_review_reason(debug_parts: list[str]) -> str:
    """Turn the technical reasons into one short, human-readable sentence.

    Keeps the detailed/debug reasons available separately (`reasons`,
    `review_reason_debug`) for logs and the full ./review.sh <id> detail view.
    """
    joined = " ".join(debug_parts).casefold()
    ai_outage = bool(_AI_OUTAGE_PATTERN.search(joined))
    cause = next((human for marker, human in _HUMAN_REASON_RULES if marker in joined), None)
    if ai_outage and cause:
        return f"AI providers were unavailable. Fallback stopped because {cause}."
    if ai_outage:
        return (
            "AI providers were temporarily unavailable, and this listing could not be "
            "safely drafted automatically."
        )
    if cause:
        return cause[0].upper() + cause[1:] + "."
    return "Needs manual review; see ./review.sh <id> for the exact technical reason."


def current_status_reason(record: ListingRecord, attempt: dict[str, Any] | None = None) -> str:
    """Current-state explanation; historical generation diagnostics stay separate."""
    status = record.status
    if status == "sent":
        return "Application confirmed sent in WG-Gesucht Messages."
    if status == "already_contacted":
        return "Existing WG-Gesucht conversation detected; no duplicate sent."
    if status == "filtered_skip":
        return "; ".join(record.decision.hard_skip_reasons) or "Filtered by housing rules."
    if status == "bewerbermappe_attachment_failed":
        if has_send_evidence(attempt):
            return (
                "Send evidence conflicts with attachment-failure status; reconcile before retrying."
            )
        return "Bewerbermappe attachment failed before Send. No Send click recorded."
    if status == "send_state_unknown":
        return "Send was attempted but could not be verified; reconcile before any retry."
    if status == "manually_rejected":
        return "Rejected manually."
    if status == "review_required":
        active_facts = current_unresolved_facts(record)
        if active_facts:
            return active_facts[0]
        if (
            record.facts.parental_guarantor_required
            and not record.facts.income_or_guarantor_accepted
        ):
            return "Elternbürgschaft is required by this listing but is not confirmed."
        if record.facts.critical_ambiguities:
            return record.facts.critical_ambiguities[0]
    if status == "premium_boost_failed":
        return "WG+ priority could not be verified."
    if status == "ai_failed":
        return "AI and safe deterministic fallback could not produce a valid draft."
    return record.review_reason_human


def current_unresolved_facts(record: ListingRecord) -> list[str]:
    """Hide only obsolete optional-example prompts in old DB rows' *display*.

    Historical evidence and approval blockers remain stored and unchanged; those
    rows must be deliberately reprocessed before they can become sendable.
    """
    raw_text = str(record.row.get("raw_text") or "")
    optional_wg_experience = any(
        optional_example_question(question.question, raw_text)
        and re.search(r"\bwg\b.{0,45}gewohnt|wg[- ]?erfahrung|flatshare", question.question, re.I)
        for question in record.facts.hidden_questions
    )
    if not optional_wg_experience:
        return record.facts.unresolved_required_facts
    return [
        fact
        for fact in record.facts.unresolved_required_facts
        if not re.search(r"wg[- ]?erfahrung|in einer wg gewohnt|shared.flat.experience", fact, re.I)
    ]


def missing_fact_question(record: ListingRecord) -> str:
    """Short Telegram question, without exposing validator/internal wording."""
    unresolved = current_unresolved_facts(record)
    if not unresolved:
        return current_status_reason(record)
    fact = unresolved[0]
    lower = fact.casefold()
    if "bürgschaft" in lower or "buergschaft" in lower:
        return "Can you provide the Bürgschaft requested in this listing?"
    if "foto" in lower or "photo" in lower:
        return "Can you include a current photo with the first application message?"
    if "semester" in lower:
        return "Will your Münster MSc include the number of semesters this listing requires?"
    if "haustier" in lower or "pet" in lower:
        return "Do you have any pets?"
    return fact


def load_listing_record(row: dict[str, Any]) -> ListingRecord:
    facts = _safe_model(ListingFacts, row.get("facts_json"), ListingFacts())
    decision = _safe_model(RuleDecision, row.get("decision_json"), RuleDecision(decision="REVIEW"))
    validation = _safe_model(ValidationResult, row.get("validation_json"), ValidationResult())
    attachment = _safe_model(AttachmentDecision, row.get("attachment_json"), AttachmentDecision())
    generation_trace = _safe_model(
        GenerationTrace,
        row.get("generation_trace_json"),
        GenerationTrace(
            ai_attempted=bool(row.get("provider") or row.get("error")),
            ai_failure_reason=str(row["error"]) if row.get("error") else None,
            fallback_used=str(row.get("message_source") or "").startswith("universal_"),
        ),
    )
    message = None
    if row.get("message_body"):
        message = MessageDraft(
            subject=row.get("message_subject") or "",
            body=str(row["message_body"]),
            answered_question_ids=json.loads(row.get("message_answered_question_ids") or "[]"),
            applied_command_ids=json.loads(row.get("message_applied_command_ids") or "[]"),
        )
    try:
        router_notes = json.loads(row.get("router_notes_json") or "[]")
    except (TypeError, ValueError):
        router_notes = []
    return ListingRecord(
        row=row,
        facts=facts,
        decision=decision,
        validation=validation,
        attachment=attachment,
        generation_trace=generation_trace,
        message=message,
        router_notes=[str(item) for item in router_notes],
    )


def rows_needing_review(database: Any) -> dict[str, list[dict[str, Any]]]:
    return {status: database.listings_by_status(status) for status, _label in REVIEW_GROUPS}


def approval_blockers(record: ListingRecord, attempt: dict[str, Any] | None = None) -> list[str]:
    """Independent safety gate for ./approve.sh. A human approving a soft REVIEW state
    must never be able to override a genuine hard skip, high scam risk, or a missing
    draft -- those remain non-negotiable regardless of the human's decision."""
    if record.status not in APPROVABLE_STATUSES:
        return [
            f"status '{record.status}' is not approvable here "
            f"(only {', '.join(sorted(APPROVABLE_STATUSES))} can be approved; "
            "send_state_unknown must be reconciled instead)"
        ]
    blockers: list[str] = []
    if has_send_evidence(attempt):
        blockers.append("a previous Send may have been clicked; reconcile before retrying")
    if record.status == "bewerbermappe_attachment_failed" and (
        not attempt or attempt.get("status") != "bewerbermappe_attachment_failed"
    ):
        blockers.append("attachment failure has no matching pre-Send actual-attempt record")
    if record.decision.hard_skip_reasons:
        blockers.append(
            "deterministic hard skip exists: " + "; ".join(record.decision.hard_skip_reasons)
        )
    if record.facts.scam_risk == "high":
        blockers.append("scam risk is high: " + "; ".join(record.facts.scam_reasons))
    elif record.facts.scam_risk == "medium":
        blockers.append("scam/payment risk remains unclear")
    if record.facts.critical_ambiguities:
        blockers.append("critical listing ambiguity remains unresolved")
    if record.facts.unresolved_required_facts:
        blockers.append(
            "required facts are missing: " + ", ".join(record.facts.unresolved_required_facts)
        )
    if record.message is None or not record.message.body.strip():
        blockers.append(
            "no valid drafted message exists for this listing; "
            "re-run analyze/dry_run to regenerate a draft first"
        )
    # Human approval may override only a soft REVIEW decision, never a failed
    # deterministic validator (hidden commands/questions, document legitimacy,
    # scam or exact application instructions). This also protects retry of old rows.
    unsafe_markers = (
        "hard skip",
        "scam",
        "critical",
        "unresolved",
        "hidden",
        "command",
        "requested documents",
        "attachment",
        "answer semantics",
        "unexpected answered",
    )
    unsafe_errors = [
        error
        for error in record.validation.errors
        if any(marker in error.casefold() for marker in unsafe_markers)
    ]
    if unsafe_errors:
        blockers.append("deterministic validation failed: " + "; ".join(unsafe_errors))
    if record.row.get("platform") == "wg_gesucht" and not (
        record.attachment.allowed
        and record.attachment.should_attach
        and record.attachment.source == "wg_account"
    ):
        blockers.append("WG Bewerbermappe is blocked by document/scam policy")
    return blockers


def build_approved_outcome(record: ListingRecord) -> AnalysisOutcome:
    """Reconstruct the AnalysisOutcome the existing contact pipeline expects. The
    human's approval stands in for the soft `decision == REVIEW` gate only; every hard
    gate (Bewerbermappe/Premium/composer-match/duplicate-send) is re-checked live by
    the unchanged browser pipeline in app/browser.py and cannot be bypassed here."""
    row = record.row
    listing = SourceListing(
        platform=row.get("platform") or "unknown",
        listing_id=row.get("listing_id") or "",
        url=row.get("canonical_url") or "",
        title=row.get("title") or "",
        raw_text=row.get("raw_text") or "(stored listing text unavailable)",
        contact_email=row.get("contact_email"),
    )
    approved_decision = record.decision.model_copy(update={"decision": "APPLY"})
    approved_validation = record.validation.model_copy(
        update={"auto_send_allowed": True, "errors": []}
    )
    return AnalysisOutcome.model_validate(
        {
            "listing": listing,
            "facts": record.facts,
            "rule_decision": approved_decision,
            "status": record.status,
            "message": record.message,
            "attachment": record.attachment,
            "validation": approved_validation,
            "generation_trace": record.generation_trace,
            "message_source": row.get("message_source") or "none",
            "database_id": record.id,
        }
    )


@dataclass
class ReviewActionResult:
    ok: bool
    status: str
    detail: str
    contact_result: ContactResult | None = None
    reconciliation: Any | None = None


def send_gates(
    record: ListingRecord, settings: Any, attempt: dict[str, Any] | None
) -> dict[str, Any]:
    """Return the same persisted/live gates used by the normal contact pipeline.

    This is deliberately descriptive rather than permissive: approval still routes to
    ``contact_listing``, which re-checks the Premium, attachment, composer, and
    duplicate-send gates in the browser immediately before a possible Send click.
    """
    attempt = attempt or {}
    blockers = approval_blockers(record, attempt)
    return {
        "status_approvable": record.status in APPROVABLE_STATUSES,
        "send_state_known": record.status != "send_state_unknown",
        "hard_filters_clear": not record.decision.hard_skip_reasons,
        "scam_risk_low": record.facts.scam_risk == "low",
        "required_facts_resolved": not record.facts.unresolved_required_facts,
        "message_present": bool(record.message and record.message.body.strip()),
        "validator_allowed": record.validation.auto_send_allowed,
        "bewerbermappe_policy_allowed": record.attachment.allowed,
        "bewerbermappe_should_attach": record.attachment.should_attach,
        "bewerbermappe_live_state": attempt.get("attachment_state") or "not attempted",
        "premium_enabled": settings.wg_use_premium_boost,
        "premium_strict": settings.wg_premium_strict,
        "premium_live_state": attempt.get("premium_state") or "not attempted",
        "no_actual_attempt_recorded": not bool(attempt),
        "dry_run": settings.dry_run,
        "auto_send": settings.auto_send,
        "autonomous_send_permitted": settings.send_permitted("auto"),
        "approval_blockers": blockers,
    }


def approve_listing(
    database: Any,
    settings: Any,
    listing_db_id: int,
    *,
    confirmed: bool,
    trigger: SendTrigger,
    contact_fn: Any | None = None,
) -> ReviewActionResult:
    """Approve through the existing contact pipeline after an explicit confirmation.

    `trigger` (a SendTrigger) names who is approving -- manual_cli for ./approve.sh,
    telegram for a confirmed Telegram Send, dashboard for the web dashboard. It is
    never "auto": approve_listing is only ever reached by an explicit human decision,
    and Settings.send_permitted treats every non-"auto" trigger as permitted to send
    for real whenever DRY_RUN is false, regardless of AUTO_SEND.
    """
    if not confirmed:
        return ReviewActionResult(False, "confirmation_required", "explicit confirmation required")
    row = database.get_listing(listing_db_id)
    if row is None:
        return ReviewActionResult(False, "not_found", f"listing {listing_db_id} was not found")
    record = load_listing_record(row)
    blockers = approval_blockers(record, database.get_actual_contact_attempt(listing_db_id))
    if blockers:
        return ReviewActionResult(False, "blocked", "; ".join(blockers))
    if contact_fn is None:
        from .contact import contact_listing

        contact_fn = contact_listing
    result = contact_fn(build_approved_outcome(record), settings, database, trigger)
    return ReviewActionResult(
        result.status in {"sent", "dry_run_ready"},
        result.status,
        result.detail,
        contact_result=result,
    )


def reject_listing(database: Any, listing_db_id: int, *, confirmed: bool) -> ReviewActionResult:
    """Persist a manual rejection only after an explicit confirmation. Shared by
    ./reject.sh and the Telegram Reject button -- neither may corrupt a settled or
    ambiguous send state (see NON_REJECTABLE_STATUSES)."""
    if not confirmed:
        return ReviewActionResult(False, "confirmation_required", "explicit confirmation required")
    row = database.get_listing(listing_db_id)
    if row is None:
        return ReviewActionResult(False, "not_found", f"listing {listing_db_id} was not found")
    status = str(row["status"])
    if status in NON_REJECTABLE_STATUSES:
        return ReviewActionResult(
            False,
            status,
            f"listing is '{status}'; rejection is not allowed once a send is settled or ambiguous",
        )
    database.mark_manually_rejected(listing_db_id)
    return ReviewActionResult(True, "manually_rejected", "listing marked manually_rejected")


def reconcile_listing(
    database: Any,
    settings: Any,
    listing_db_id: int,
    *,
    reconcile_fn: Any | None = None,
) -> ReviewActionResult:
    """Inspect an existing conversation without entering any sending code path."""
    row = database.get_listing(listing_db_id)
    if row is None:
        return ReviewActionResult(False, "not_found", f"listing {listing_db_id} was not found")
    url = str(row.get("canonical_url") or "")
    if not url:
        return ReviewActionResult(False, "blocked", "listing has no stored URL")
    attempt = database.get_actual_contact_attempt(listing_db_id) or {}
    expected_body = attempt.get("message_body") or row.get("message_body") or ""
    if not expected_body:
        return ReviewActionResult(False, "blocked", "no stored message body to reconcile")
    if reconcile_fn is None:
        from .browser import reconcile_wg_url

        reconcile_fn = reconcile_wg_url
    result = reconcile_fn(url, settings, expected_body, str(row.get("listing_id") or ""))
    database.reconcile_contact(listing_db_id, result.result, result.detail)
    return ReviewActionResult(
        result.result in {"sent", "not_sent"},
        result.result,
        result.detail,
        reconciliation=result,
    )


def reprocess_listing(
    database: Any,
    settings: Any,
    config: dict[str, Any],
    answers: dict[str, Any],
    listing_db_id: int,
    *,
    force: bool = False,
    fallback_only: bool = False,
    process_fn: Any | None = None,
) -> ReviewActionResult:
    """Re-run the normal decision/generation pipeline on a listing's own stored text,
    picking up any rule/validator/fallback fix made since it was last processed.

    This only re-drafts; it never sends. Re-run ./approve.sh afterwards for a
    listing that now looks safe to send. sent/already_contacted/send_state_unknown
    are always refused (a settled or ambiguous send state must never be silently
    reanalyzed); manually_rejected requires an explicit `force=True` since the human
    already made that call once. `fallback_only=True` (independent of `force`) makes
    zero cloud-provider calls and exercises exactly the production deterministic-
    fallback path, for testing it against stored listings without depending on real
    provider outages.
    """
    row = database.get_listing(listing_db_id)
    if row is None:
        return ReviewActionResult(False, "not_found", f"listing {listing_db_id} was not found")
    status = str(row["status"])
    if status in REPROCESS_FORBIDDEN_STATUSES:
        detail = f"listing is '{status}'; reprocessing must never resend or re-claim it"
        if status == "send_state_unknown":
            detail += "; reconcile it first with ./reconcile.sh"
        return ReviewActionResult(False, status, detail)
    if status == "manually_rejected" and not force:
        return ReviewActionResult(
            False,
            status,
            "listing was manually rejected; pass force=True to reprocess it anyway",
        )
    listing = SourceListing(
        platform=row.get("platform") or "unknown",
        listing_id=row.get("listing_id") or "",
        url=row.get("canonical_url") or "",
        title=row.get("title") or "",
        raw_text=row.get("raw_text") or "",
        contact_email=row.get("contact_email"),
    )
    if process_fn is None:
        from .analyzer import process_listing

        process_fn = process_listing
    outcome = process_fn(
        listing,
        config=config,
        answers=answers,
        settings=settings,
        database=database,
        force_fallback=fallback_only,
    )
    detail = f"reprocessed; new status={outcome.status}"
    if fallback_only:
        detail += " (fallback-only diagnostic; no cloud provider was called)"
    return ReviewActionResult(True, outcome.status, detail)


def latest_screenshot(listing_id: str) -> str | None:
    if not listing_id:
        return None
    directory = ROOT / "logs" / "browser"
    if not directory.is_dir():
        return None
    safe_id = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in listing_id)
    matches = sorted(directory.glob(f"*_{safe_id}*.png"))
    return str(matches[-1]) if matches else None
