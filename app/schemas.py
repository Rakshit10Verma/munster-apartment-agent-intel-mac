from typing import Literal, Optional
from pydantic import BaseModel, Field

class HiddenQuestion(BaseModel):
    id: str
    question: str
    category: str = "other"
    required: bool = True

class HiddenCommand(BaseModel):
    id: str
    instruction: str
    exact_text: Optional[str] = None
    required: bool = True

class ListingExtraction(BaseModel):
    platform: str = "unknown"
    listing_language: Literal["de", "en", "other"] = "de"
    advertiser_type: str = "unknown"
    housing_type: Literal["wg_room","studio","whole_flat","zwischenmiete","wohnheim","nachmieter","student_room","other","unknown"] = "unknown"
    warm_rent_eur: Optional[float] = None
    cold_rent_eur: Optional[float] = None
    deposit_eur: Optional[float] = None
    furniture_takeover_eur: Optional[float] = None
    room_size_m2: Optional[float] = None
    flat_size_m2: Optional[float] = None
    total_rooms: Optional[float] = None
    realistic_residents: Optional[int] = None
    move_in: Optional[str] = None
    end_date: Optional[str] = None
    minimum_duration_months: Optional[int] = None
    anmeldung: Literal["yes","no","unknown"] = "unknown"
    wbs_required: bool = False
    women_only: bool = False
    explicit_min_age: Optional[int] = None
    age_preference_only: bool = False
    religion_or_confession_required: bool = False
    religious_fraternity_or_membership_required: bool = False
    income_requirement: bool = False
    schufa_required: bool = False
    buergschaft_required: bool = False
    indexmiete: bool = False
    hauptmieter_liability: bool = False
    contact_method: str = "unknown"
    contact_email: Optional[str] = None
    documents_explicitly_requested: list[str] = Field(default_factory=list)
    scam_risk: Literal["low","medium","high"] = "low"
    scam_reasons: list[str] = Field(default_factory=list)
    hidden_questions: list[HiddenQuestion] = Field(default_factory=list)
    hidden_commands: list[HiddenCommand] = Field(default_factory=list)
    personalization_hooks: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    needs_cloud: bool = False

class RuleDecision(BaseModel):
    decision: Literal["APPLY","SKIP","REVIEW"]
    hard_skip_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    effective_warm_rent_per_person_eur: Optional[float] = None

class AnsweredQuestion(BaseModel):
    question_id: str
    answer_used: str

class AppliedCommand(BaseModel):
    command_id: str
    how_applied: str

class MessageDraft(BaseModel):
    subject: str = ""
    body: str
    answered_questions: list[AnsweredQuestion] = Field(default_factory=list)
    applied_commands: list[AppliedCommand] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    needs_cloud: bool = False

class FinalResult(BaseModel):
    provider_for_extraction: str
    provider_for_message: Optional[str] = None
    extraction: ListingExtraction
    rule_decision: RuleDecision
    message: Optional[MessageDraft] = None
    auto_send_allowed: bool = False
    validation_errors: list[str] = Field(default_factory=list)
