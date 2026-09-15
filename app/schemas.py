from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Platform = Literal["wg_gesucht", "asta_muenster", "na_dann", "kleinanzeigen", "mock", "unknown"]
Decision = Literal["APPLY", "SKIP", "REVIEW"]
Risk = Literal["low", "medium", "high"]


def utc_now() -> datetime:
    return datetime.now(UTC)


class HiddenQuestion(BaseModel):
    id: str
    question: str
    category: str = "other"
    required: bool = True


class HiddenCommand(BaseModel):
    id: str
    instruction: str
    kind: Literal[
        "required_first_word",
        "exact_subject",
        "required_keyword",
        "length",
        "other",
    ] = "other"
    exact_text: str | None = None
    target: Literal["subject", "body", "either"] = "body"
    required: bool = True


class SourceListing(BaseModel):
    platform: Platform = "unknown"
    listing_id: str = ""
    url: str = ""
    title: str = ""
    raw_text: str
    contact_email: str | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
    source_metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("raw_text")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("listing text cannot be empty")
        return value


class ListingFacts(BaseModel):
    listing_language: Literal["de", "en", "other"] = "de"
    advertiser_type: Literal["wg", "private_landlord", "company", "unknown"] = "unknown"
    housing_type: Literal[
        "wg_room",
        "studio",
        "whole_flat",
        "zwischenmiete",
        "wohnheim",
        "nachmieter",
        "student_room",
        "other",
        "unknown",
    ] = "unknown"
    warm_rent_eur: float | None = None
    cold_rent_eur: float | None = None
    deposit_eur: float | None = None
    furniture_takeover_eur: float | None = None
    room_size_m2: float | None = None
    flat_size_m2: float | None = None
    total_rooms: float | None = None
    realistic_residents: int | None = None
    move_in: str | None = None
    end_date: str | None = None
    minimum_duration_months: int | None = None
    anmeldung: Literal["yes", "no", "unknown"] = "unknown"
    wbs_required: bool = False
    women_only: bool = False
    explicit_min_age: int | None = None
    age_preference_only: bool = False
    religion_or_confession_required: bool = False
    religious_fraternity_or_membership_required: bool = False
    income_requirement: bool = False
    schufa_required: bool = False
    buergschaft_required: bool = False
    indexmiete: bool = False
    hauptmieter_liability: bool = False
    contact_method: Literal["platform", "email", "website", "unknown"] = "unknown"
    contact_email: str | None = None
    documents_explicitly_requested: list[str] = Field(default_factory=list)
    scam_risk: Risk = "low"
    scam_reasons: list[str] = Field(default_factory=list)
    hidden_questions: list[HiddenQuestion] = Field(default_factory=list)
    hidden_commands: list[HiddenCommand] = Field(default_factory=list)
    personalization_hooks: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    critical_ambiguities: list[str] = Field(default_factory=list)
    mandatory_incompatibilities: list[str] = Field(default_factory=list)
    odd_fee_or_payment_demands: list[str] = Field(default_factory=list)
    unresolved_required_facts: list[str] = Field(default_factory=list)
    origin_explicitly_required: bool = False
    confidence: float = Field(default=0.0, ge=0, le=1)


class PrefilterResult(BaseModel):
    facts: ListingFacts
    hard_skip_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    deterministic_signals: list[str] = Field(default_factory=list)

    @property
    def viable(self) -> bool:
        return not self.hard_skip_reasons


class MessageDraft(BaseModel):
    subject: str = ""
    body: str = ""
    answered_question_ids: list[str] = Field(default_factory=list)
    applied_command_ids: list[str] = Field(default_factory=list)
    hooks_used: list[str] = Field(default_factory=list)
    unresolved_required_facts: list[str] = Field(default_factory=list)
    language: Literal["de", "en", "other"] = "de"
    address_register: Literal["du", "sie", "neutral"] = "neutral"


class AIAnalysis(BaseModel):
    facts: ListingFacts
    decision_recommendation: Decision = "REVIEW"
    decision_reasons: list[str] = Field(default_factory=list)
    message: MessageDraft
    confidence: float = Field(default=0.0, ge=0, le=1)


class RuleDecision(BaseModel):
    decision: Decision
    hard_skip_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    effective_warm_rent_per_person_eur: float | None = None


class ProviderMetadata(BaseModel):
    provider: str
    model: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None


class AttachmentDecision(BaseModel):
    allowed: bool = False
    should_attach: bool = False
    path: str | None = None
    reasons: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    auto_send_allowed: bool = False
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AnalysisOutcome(BaseModel):
    listing: SourceListing
    facts: ListingFacts
    rule_decision: RuleDecision
    status: Literal[
        "filtered_skip",
        "ai_failed",
        "review_required",
        "drafted",
        "dry_run_ready",
        "sent",
        "send_failed",
    ]
    message: MessageDraft | None = None
    provider: ProviderMetadata | None = None
    attachment: AttachmentDecision = Field(default_factory=AttachmentDecision)
    validation: ValidationResult = Field(default_factory=ValidationResult)
    router_notes: list[str] = Field(default_factory=list)
    database_id: int | None = None


class ContactResult(BaseModel):
    status: Literal["dry_run_ready", "sent", "send_failed", "review_required"]
    external_message_id: str | None = None
    screenshot_path: str | None = None
    detail: str = ""
