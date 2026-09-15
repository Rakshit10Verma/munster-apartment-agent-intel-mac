from .config_loader import load_all
from .schemas import ListingExtraction, MessageDraft, FinalResult
from .providers import route
from .prompts import EXTRACT_SYSTEM, MESSAGE_SYSTEM, extraction_prompt, message_prompt
from .rules import apply_rules

def validate_message(x,d):
    errors=[]
    req_q={q.id for q in x.hidden_questions if q.required}
    ans_q={q.question_id for q in d.answered_questions}
    if req_q-ans_q: errors.append(f"unanswered hidden questions: {sorted(req_q-ans_q)}")
    req_c={c.id for c in x.hidden_commands if c.required}
    app_c={c.command_id for c in d.applied_commands}
    if req_c-app_c: errors.append(f"unapplied hidden commands: {sorted(req_c-app_c)}")
    if d.unresolved_questions: errors.append(f"unresolved questions: {d.unresolved_questions}")
    low=d.body.lower()
    if "aus indien" in low or "from india" in low or "indian" in low: errors.append("national origin mentioned")
    if " lbs " in f" {low} ": errors.append("LBS abbreviation used")
    if d.confidence < .70: errors.append("message confidence below 0.70")
    if d.needs_cloud: errors.append("message requests cloud review")
    return errors

def analyze(text):
    cfg,answers=load_all()
    notes=[]
    er,n=route(EXTRACT_SYSTEM,extraction_prompt(text),ListingExtraction); notes+=n
    x=er.data
    dec=apply_rules(x,cfg)
    if dec.decision=="SKIP":
        return FinalResult(provider_for_extraction=er.provider,extraction=x,rule_decision=dec,auto_send_allowed=False),notes

    mr,n=route(MESSAGE_SYSTEM,message_prompt(x.model_dump(),dec.model_dump(),cfg,answers),MessageDraft); notes+=n
    d=mr.data
    errors=validate_message(x,d)
    auto=(dec.decision=="APPLY" and not errors and not x.needs_cloud)
    return FinalResult(provider_for_extraction=er.provider,provider_for_message=mr.provider,
                       extraction=x,rule_decision=dec,message=d,auto_send_allowed=auto,
                       validation_errors=errors),notes
