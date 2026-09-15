import json
from .schemas import ListingExtraction, MessageDraft

EXTRACT_SYSTEM = """You are a meticulous German housing-listing parser.
Never invent facts. Distinguish preferences from hard requirements.
Read the entire listing, especially hidden proof-of-reading questions near the end.
Return only valid JSON matching the requested schema."""

MESSAGE_SYSTEM = """You write authentic housing applications for Rakshit.
Use only confirmed facts from the supplied profile/answer bank and facts in the listing.
Never invent subjective preferences. Answer every required listing question and obey exact commands.
Never mention national origin unless explicitly required.
Always write Landesbausparkasse in full, never LBS.
Offer video call and short-notice in-person viewing.
Mirror German du/Sie register and listing language.
Return only valid JSON matching the requested schema."""

def extraction_prompt(text):
    return f"""Extract this listing.

Important:
- women-only wording => women_only=true
- WBS only when actually required
- age preference is not a hard minimum
- religious/fraternity markers include Studentenverbindung, K.St.V., Unitas, Corps, Burschenschaft, confession/church membership, required traditions/membership
- find ALL proof-of-reading questions and exact commands
- set needs_cloud=true for material ambiguity

SCHEMA:
{json.dumps(ListingExtraction.model_json_schema(), ensure_ascii=False)}

LISTING:
{text}
"""

def message_prompt(extraction, decision, config, answers):
    return f"""Write the application for this listing.

DECISION:
{json.dumps(decision, ensure_ascii=False)}

EXTRACTION:
{json.dumps(extraction, ensure_ascii=False)}

APPLICANT:
{json.dumps(config["applicant"], ensure_ascii=False)}

CONFIRMED ANSWERS:
{json.dumps(answers["confirmed_answer_bank"], ensure_ascii=False)}

RULES:
- Answer every hidden question and record its id.
- Apply every hidden command and record its id.
- Unknown subjective facts go into unresolved_questions; never invent.
- Politics/sensitive questions use the confirmed boundary answer.
- WG: 100-170 words. Formal landlord: 90-140.
- Listing German => German. Listing English => English.
- Casual WG => du/ihr. Formal landlord => Sie.
- 1-2 truly listing-specific hooks.
- No generic cover-letter opening.

SCHEMA:
{json.dumps(MessageDraft.model_json_schema(), ensure_ascii=False)}
"""
