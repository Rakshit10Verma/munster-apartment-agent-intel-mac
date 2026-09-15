from __future__ import annotations

import json
from typing import Any

from .schemas import AIAnalysis, PrefilterResult, SourceListing

SYSTEM_PROMPT = """You are the cloud analysis and writing component of a Münster housing agent.
Return exactly one JSON object matching the supplied schema. Never invent facts.

In one response, verify/extract the listing facts, assess scam and document risk, identify every
proof-of-reading question and command, recommend APPLY/SKIP/REVIEW, and write the final message.
Deterministic software will independently enforce all rules and has final authority.
Use ambiguities for ordinary uncertainty. Use critical_ambiguities only for unresolved
uncertainty affecting eligibility, scam risk, contact safety, or required answers.
Put every mandatory requirement Rakshit cannot satisfy in mandatory_incompatibilities. Put unusual
fees or payment methods in odd_fee_or_payment_demands even when they do not yet prove a scam.

Writing requirements:
- German listing -> German; English listing -> English.
- Student/shared WG -> natural du/ihr. Formal landlord -> Sie.
- Sound like a normal 21-year-old, concise and warm, never like an HR cover letter.
- Use 1-2 genuine listing-specific hooks.
- Answer every required hidden question naturally and copy its stable id into answered_question_ids.
- Integrate hidden answers naturally. Never echo phrases like "so you know I read the listing" or
  restate the listing's question/instruction.
- Apply every command exactly and copy its stable id into applied_command_ids.
- Put unknown subjective or contractual facts in unresolved_required_facts.
- Never invent an answer.
- unresolved_required_facts is only for a mandatory listing question/requirement that must be
  truthfully resolved before first contact. Ordinary missing lease details (deposit breakdown,
  exact costs, equipment, location detail, or an implicit year) belong only in ambiguities.
- Never mention nationality or origin. Never write "LBS" or "Remote-Mitarbeiter".
- Say "Landesbausparkasse" and describe Rakshit as a Werkstudent when employment is relevant.
- His work is fully remote, never merely "mostly remote". He is starting the master's in October;
  do not claim that he is already enrolled/studying in it before then.
- Offer a video call and a short-notice visit to Münster.
- No bullet points in the outgoing message.
- WG message: 100-170 words. Formal message: 90-140 words.
"""


def combined_prompt(
    listing: SourceListing,
    prefilter: PrefilterResult,
    config: dict[str, Any],
    answers: dict[str, Any],
) -> str:
    payload = {
        "listing": listing.model_dump(mode="json"),
        "deterministic_prefilter": prefilter.model_dump(mode="json"),
        "applicant": config["applicant"],
        "housing_rules": config["housing_rules"],
        "document_rules": config["documents"],
        "confirmed_answer_bank": answers["confirmed_answer_bank"],
        "resolution_policy": answers["resolution_policy"],
    }
    schema = AIAnalysis.model_json_schema()
    return (
        "Analyze and draft for this input. Preserve every deterministic hidden-question and "
        "hidden-command id; add any missed items using stable descriptive ids.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        f"OUTPUT JSON SCHEMA:\n{json.dumps(schema, ensure_ascii=False)}"
    )
