from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .answer_bank_resolver import (
    ResolvedQuestion,
    resolve_hidden_commands,
    resolve_hidden_questions,
)
from .message_policy import (
    GERMAN_WG_VIEWING_CLOSING,
    enforce_message_policy,
    is_formal_application_context,
)
from .schemas import HiddenCommand, MessageDraft, PrefilterResult, SourceListing

# Backwards-compatible browser-test fixture. Production fallback messages are built
# from config by ``build_deterministic_fallback`` below; this constant is not selected
# by the analyzer.
GERMAN_WG_TEMPLATE = (
    "Hey zusammen,\n\n"
    "ich bin Rakshit, 21, und starte im Oktober meinen Master in Münster. Ich mag ein "
    "entspanntes WG-Leben, koche gerne, fahre Fahrrad und bin bei Kartenabenden dabei. "
    "Gleichzeitig respektiere ich den Alltag und Rückzugsraum der anderen, bin ordentlich "
    f"und halte mich an einen Putzplan.\n\n{GERMAN_WG_VIEWING_CLOSING}"
)


@dataclass
class FallbackBuild:
    draft: MessageDraft | None = None
    blockers: list[str] = field(default_factory=list)
    scenario: str | None = None
    template: str | None = None
    optional_clauses: list[str] = field(default_factory=list)
    resolved_questions: list[ResolvedQuestion] = field(default_factory=list)


def _base_fallback_blockers(listing: SourceListing, prefilter: PrefilterResult) -> list[str]:
    facts = prefilter.facts
    blockers: list[str] = []
    if listing.platform != "wg_gesucht":
        blockers.append("deterministic fallback is currently limited to WG-Gesucht")
    if prefilter.hard_skip_reasons:
        blockers.append("deterministic hard skip")
    if facts.scam_risk != "low" or facts.scam_reasons or facts.odd_fee_or_payment_demands:
        blockers.append("scam or unusual-payment ambiguity")
    if (
        facts.critical_ambiguities
        or facts.unresolved_required_facts
        or facts.mandatory_incompatibilities
    ):
        blockers.append("unresolved eligibility ambiguity")
    if facts.listing_language not in {"de", "en"}:
        blockers.append("listing language is not deterministically clear")
    if facts.housing_type not in {
        "wg_room",
        "studio",
        "whole_flat",
        "zwischenmiete",
        "wohnheim",
        "student_room",
        "nachmieter",
    }:
        blockers.append("listing type is not supported by deterministic fallback")
    if facts.warm_rent_eur is None:
        blockers.append("hard housing eligibility is not deterministically clear")

    text = f"{listing.title}\n{listing.raw_text}"
    if "?" in text and not facts.hidden_questions:
        blockers.append("unclassified listing question detected")
    application_instruction = re.compile(
        r"(?:bewerbung|anschreiben|nachricht|kontaktaufnahme|application|message).{0,90}"
        r"(?:schreib|sag|nenn|erwähn|beginn|starte|betreff|subject|write|tell|mention|start|"
        r"wörter|words|zeichen|characters|codewort|keyword)",
        re.I | re.S,
    )
    if application_instruction.search(text) and not (
        facts.hidden_questions or facts.hidden_commands
    ):
        blockers.append("unclassified special application instruction detected")
    return list(dict.fromkeys(blockers))


def _scenario(listing: SourceListing, prefilter: PrefilterResult, config: dict[str, Any]) -> str:
    facts = prefilter.facts
    text = f"{listing.title}\n{listing.raw_text}".casefold()
    if facts.housing_type == "wg_room":
        return "wg_room"
    if facts.housing_type == "zwischenmiete":
        return "wg_zwischenmiete" if re.search(r"\b(?:wg|mitbewohner)\b", text) else "zwischenmiete"
    if facts.housing_type in {"wohnheim", "student_room"}:
        return "student_dorm"
    if facts.housing_type == "studio":
        return "studio"
    if facts.housing_type == "whole_flat":
        max_single = float(config["housing_rules"]["max_warm_rent_single_eur"])
        if facts.warm_rent_eur and facts.warm_rent_eur > max_single and facts.realistic_residents:
            return "whole_apartment_shared_later"
        return "whole_apartment"
    if facts.housing_type == "nachmieter":
        return "whole_apartment"
    return "unsupported"


def _month_de(value: Any) -> str:
    return {
        "january": "Januar",
        "february": "Februar",
        "march": "März",
        "april": "April",
        "may": "Mai",
        "june": "Juni",
        "july": "Juli",
        "august": "August",
        "september": "September",
        "october": "Oktober",
        "november": "November",
        "december": "Dezember",
    }.get(str(value).casefold(), str(value))


def _profile_intro(profile: dict[str, Any], language: str) -> str | None:
    name = str(profile.get("name") or "").strip()
    age = profile.get("age")
    degree = str(profile.get("degree") or "").strip()
    university = str(profile.get("university") or "").strip()
    month = str(profile.get("turns_22_month") or "").strip()
    if not name or age is None or not degree or not university:
        return None
    first_name = name.split()[0]
    if language == "en":
        birthday = f", and I'll turn {int(age) + 1} in {month}" if month else ""
        return (
            f"I'm {first_name}, {age}{birthday}. I will start my {degree} at {university} in "
            f"{month or 'the coming semester'}."
        )
    birthday = f" und werde im {_month_de(month)} {int(age) + 1}" if month else ""
    university_de = "Uni Münster" if university == "University of Münster" else university
    return (
        f"ich bin {first_name}, {age}{birthday}. Ab {_month_de(month) or 'dem kommenden Semester'} "
        f"starte ich meinen {degree} an der {university_de}."
    )


def _work_clause(profile: dict[str, Any], language: str) -> str | None:
    employer = str(profile.get("employer") or "").strip()
    work_mode = str(profile.get("work_mode") or "").strip()
    if (
        not employer
        or work_mode.casefold() not in {"fully remote", "komplett remote"}
        or profile.get("regular_own_income") is not True
    ):
        return None
    if language == "en":
        return (
            f"Alongside my studies, I work fully remotely as a working student at {employer}, "
            "so I have a regular income."
        )
    return (
        f"Neben dem Studium arbeite ich komplett remote als Werkstudent bei der {employer}. "
        "Dadurch habe ich ein regelmäßiges Einkommen."
    )


def _listing_hook(listing: SourceListing, language: str) -> tuple[str | None, str | None]:
    text = f"{listing.title}\n{listing.raw_text}".casefold()
    hooks = (
        (r"\bbalkon\b|balcony", "der Balkon", "the balcony"),
        (r"\bgarten\b|garden", "der Garten", "the garden"),
        (r"\bwohnzimmer\b|living room", "das Wohnzimmer", "the living room"),
        (r"\bzentral\w*\b|central", "die zentrale Lage", "the central location"),
        (r"\bruhig\w*\b|quiet", "die ruhige Lage", "the quiet location"),
        (r"\bhell\w*\b|bright", "das helle Zimmer", "the bright room"),
    )
    for pattern, german, english in hooks:
        if re.search(pattern, text):
            if language == "en":
                return f"I especially like {english}.", f"listing_hook:{english}"
            return f"Besonders gut gefällt mir {german}.", f"listing_hook:{german}"
    return None, None


def _furnishing_clause(furnishing: str, language: str) -> tuple[str | None, str | None]:
    values = {
        "furnished": (
            "Dass die Unterkunft möbliert ist, passt für meinen Umzug aus Berlin sehr gut.",
            "Being furnished works very well for my move from Berlin.",
        ),
        "partly_furnished": (
            "Die Teilmöblierung passt für meinen Umzug aus Berlin gut.",
            "The partial furnishing works well for my move from Berlin.",
        ),
        "unfurnished": (
            "Dass die Unterkunft unmöbliert ist, ist für mich völlig in Ordnung.",
            "An unfurnished place is completely fine for me.",
        ),
    }
    if furnishing not in values:
        return None, None
    german, english = values[furnishing]
    return (english if language == "en" else german), f"furnishing:{furnishing}"


def _wg_life_clause(answer_bank: dict[str, Any], language: str) -> str:
    bank = answer_bank.get("confirmed_answer_bank", answer_bank)
    games = bank.get("card_games", {}).get("favorites", [])
    games_text = " oder ".join(str(item) for item in games[:2]) or "Kartenspiele"
    if language == "en":
        return (
            "I like a relaxed flatshare where people sometimes cook, eat, or play cards together, "
            f"and I'd always be up for {games_text}. At the same time, everyone's own routine and "
            "space matter to me. I am tidy and stick to a cleaning schedule."
        )
    return (
        "Ich mag ein entspanntes WG-Leben, bei dem man auch mal zusammen kocht, isst oder Karten "
        f"spielt - bei {games_text} wäre ich sofort dabei. Gleichzeitig finde ich es wichtig, "
        "dass jeder seinen eigenen Alltag und Rückzugsraum hat. Ich bin ordentlich und halte mich "
        "ganz normal an einen Putzplan."
    )


def _scenario_clause(
    scenario: str,
    profile: dict[str, Any],
    answer_bank: dict[str, Any],
    language: str,
) -> str:
    intended = profile.get("intended_stay_months")
    years = round(float(intended) / 12) if intended else None
    if scenario in {"wg_room", "wg_zwischenmiete"}:
        return _wg_life_clause(answer_bank, language)
    if language == "en":
        return {
            "studio": "I am looking for a quiet place of my own and would treat it reliably.",
            "whole_apartment": (
                f"I am a quiet, reliable tenant looking for a stable home for around {years} years."
                if years
                else "I am a quiet, reliable tenant looking for a stable, long-term home."
            ),
            "whole_apartment_shared_later": (
                "As a quiet, tidy tenant, I would use the larger apartment as a stable home and "
                "could later share it with flatmates."
            ),
            "zwischenmiete": (
                "The offered sublet period works for me, and I would treat the apartment quietly "
                "and reliably."
            ),
            "student_dorm": (
                "The student accommodation fits my master's start well; I live quietly and tidily."
            ),
        }.get(scenario, "The accommodation looks like a good fit for my move to Münster.")
    return {
        "studio": (
            "Ich suche ein ruhiges eigenes Zuhause und gehe damit ordentlich und zuverlässig um."
        ),
        "whole_apartment": (
            f"Ich bin ein ruhiger, zuverlässiger Mieter und suche für ungefähr {years} Jahre "
            "ein langfristiges Zuhause."
            if years
            else "Ich bin ein ruhiger, zuverlässiger Mieter und suche ein langfristiges Zuhause."
        ),
        "whole_apartment_shared_later": (
            "Als ruhiger, ordentlicher Mieter würde ich die größere Wohnung langfristig nutzen "
            "und könnte sie später mit Mitbewohnern teilen."
        ),
        "zwischenmiete": (
            "Der Zwischenmietzeitraum passt für mich; ich würde ruhig wohnen und zuverlässig "
            "mit der Wohnung umgehen."
        ),
        "student_dorm": (
            "Das studentische Wohnangebot passt gut zu meinem Masterstart; ich lebe ruhig und "
            "ordentlich."
        ),
    }.get(scenario, "Die Unterkunft passt gut zu meinem Start in Münster.")


def _closing(formal: bool, language: str, name: str) -> str:
    if language == "en":
        if formal:
            return (
                "As I currently live in Berlin, a first video call would be easiest. If you prefer "
                "an in-person viewing, please let me know a little in advance so I can arrange my "
                f"tickets to Münster.\n\nKind regards\n{name}"
            )
        return (
            "As I currently live in Berlin, a first video call would be easiest. If you prefer an "
            "in-person viewing, let me know a little in advance and I can arrange my tickets to "
            f"Münster.\n\nBest wishes\n{name.split()[0]}"
        )
    if not formal:
        return GERMAN_WG_VIEWING_CLOSING
    return (
        "Da ich noch in Berlin wohne, wäre ein erstes Kennenlernen per Video-Call am einfachsten. "
        "Wenn Sie eine Besichtigung vor Ort bevorzugen, geben Sie mir bitte etwas "
        "vorher Bescheid. Dann kann ich meine Tickets planen und in den nächsten Tagen nach "
        "Münster kommen.\n\nFalls noch etwas offen ist, schreiben Sie mir gerne. Ich würde mich "
        f"freuen, mehr über die Wohnung zu erfahren.\n\nFreundliche Grüße\n{name}"
    )


def _insert_paragraph_before_closing(body: str, paragraph: str) -> str:
    if body.endswith(GERMAN_WG_VIEWING_CLOSING):
        prefix = body[: -len(GERMAN_WG_VIEWING_CLOSING)].rstrip()
        return f"{prefix}\n\n{paragraph}\n\n{GERMAN_WG_VIEWING_CLOSING}"
    paragraphs = re.split(r"\n\s*\n", body.strip())
    if paragraphs and re.search(r"(?:grüße|regards|wishes)\s*\n", paragraphs[-1], re.I):
        paragraphs.insert(-1, paragraph)
        return "\n\n".join(paragraphs)
    return f"{body}\n\n{paragraph}"


def _keyword_sentence(exact: str, language: str) -> str:
    return (
        f"The requested keyword is: {exact}."
        if language == "en"
        else f"Das gewünschte Stichwort ist: {exact}."
    )


def _length_command_ok(command: HiddenCommand, body: str) -> bool:
    number = re.search(r"(\d+)", command.instruction)
    if not number:
        return False
    maximum = int(number.group(1))
    count = (
        len(body)
        if re.search(r"zeichen|characters", command.instruction, re.I)
        else len(re.findall(r"\b[\wÄÖÜäöüß'-]+\b", body, re.UNICODE))
    )
    return count <= maximum


def apply_hidden_commands(
    draft: MessageDraft, commands: list[HiddenCommand]
) -> tuple[MessageDraft, list[str]]:
    subject = draft.subject
    body = draft.body
    applied: list[str] = []
    for command in commands:
        exact = (command.exact_text or "").strip()
        if command.kind == "required_first_word":
            body = f"{exact}\n\n{body}"
            applied.append(command.id)
        elif command.kind == "exact_subject":
            subject = exact
            applied.append(command.id)
        elif command.kind == "required_keyword":
            body = _insert_paragraph_before_closing(body, _keyword_sentence(exact, draft.language))
            applied.append(command.id)
    errors: list[str] = []
    for command in commands:
        if command.kind == "length":
            if _length_command_ok(command, body):
                applied.append(command.id)
            else:
                errors.append(
                    f"length command {command.id} could not be satisfied deterministically"
                )
    return draft.model_copy(
        update={
            "subject": subject,
            "body": body,
            "applied_command_ids": list(dict.fromkeys([*draft.applied_command_ids, *applied])),
        }
    ), errors


def build_deterministic_fallback(
    listing: SourceListing,
    prefilter: PrefilterResult,
    config: dict[str, Any],
    answers: dict[str, Any],
) -> FallbackBuild:
    blockers = _base_fallback_blockers(listing, prefilter)
    facts = prefilter.facts
    resolved, unresolved_questions = resolve_hidden_questions(
        facts.hidden_questions,
        facts.listing_language,
        answers,
        config.get("applicant", {}),
    )
    if unresolved_questions:
        blockers.append(
            "hidden question(s) cannot be resolved from the confirmed answer bank/profile: "
            + "; ".join(question.question for question in unresolved_questions)
        )
    commands, unresolved_commands = resolve_hidden_commands(facts.hidden_commands)
    if unresolved_commands:
        blockers.append(
            "hidden command(s) cannot be deterministically applied: "
            + "; ".join(command.instruction for command in unresolved_commands)
        )
    scenario = _scenario(listing, prefilter, config)
    if scenario == "unsupported":
        blockers.append("no deterministic template scenario is available")

    profile = config.get("applicant", {})
    intro = _profile_intro(profile, facts.listing_language)
    if intro is None:
        blockers.append("applicant profile lacks name, age, degree, or university")
    if blockers:
        return FallbackBuild(
            blockers=list(dict.fromkeys(blockers)),
            scenario=scenario,
            resolved_questions=resolved,
        )

    formal = is_formal_application_context(listing, facts)
    language = facts.listing_language
    name = str(profile["name"])
    if language == "en":
        greeting = "Hello," if formal else "Hey everyone,"
    else:
        greeting = "Guten Tag," if formal else "Hey zusammen,"
    optional_clauses: list[str] = []
    body_parts = [greeting, intro or ""]

    hook, hook_id = _listing_hook(listing, language)
    work = _work_clause(profile, language)
    if work:
        body_parts.append(work)
        optional_clauses.append("work_and_income")
    body_parts.append(_scenario_clause(scenario, profile, answers, language))
    if hook:
        body_parts.append(hook)
        if hook_id:
            optional_clauses.append(hook_id)
    pets = profile.get("pets", {})
    if (
        re.search(r"\b(?:hund|hunde|dog|dogs|haustier|pets?)\b", listing.raw_text, re.I)
        and (pets.get("owns_pets") is False and pets.get("comfortable_living_with_pets") is True)
        and not any(item.question_category == "pets" for item in resolved)
    ):
        likes_dogs = "dogs" in pets.get("particularly_likes", [])
        if language == "en":
            pet_clause = "I don't have pets, but I'm happy living with them"
            pet_clause += " and especially like dogs." if likes_dogs else "."
        else:
            pet_clause = "Ich habe selbst keine Haustiere, komme mit ihnen aber gut klar"
            pet_clause += " und mag Hunde besonders gern." if likes_dogs else "."
        body_parts.append(pet_clause)
        optional_clauses.append("pets")
    furnished, furnishing_id = _furnishing_clause(facts.furnishing, language)
    if furnished:
        body_parts.append(furnished)
        if furnishing_id:
            optional_clauses.append(furnishing_id)
    if resolved:
        body_parts.append(" ".join(item.resolved_answer for item in resolved))
        optional_clauses.extend(f"hidden_answer:{item.question_category}" for item in resolved)
    body_parts.append(_closing(formal, language, name))
    body = "\n\n".join(part.strip() for part in body_parts if part.strip())
    template = f"{language}_{'formal' if formal else 'wg'}_{scenario}"
    draft = MessageDraft(
        subject="",
        body=body,
        language=language,
        address_register="sie" if formal else "du",
        answered_question_ids=[item.question_id for item in resolved],
    )
    draft = enforce_message_policy(listing, facts, draft)
    draft, command_errors = apply_hidden_commands(draft, commands)
    if command_errors:
        return FallbackBuild(
            blockers=command_errors,
            scenario=scenario,
            template=template,
            optional_clauses=optional_clauses,
            resolved_questions=resolved,
        )
    if commands:
        optional_clauses.extend(f"hidden_command:{item.kind}" for item in commands)
    if facts.age_mismatch:
        optional_clauses.append("soft_age_mismatch")
    return FallbackBuild(
        draft=draft,
        scenario=scenario,
        template=template,
        optional_clauses=list(dict.fromkeys(optional_clauses)),
        resolved_questions=resolved,
    )


def build_universal_fallback(
    listing: SourceListing,
    prefilter: PrefilterResult,
    config: dict[str, Any],
    answers: dict[str, Any],
) -> tuple[MessageDraft | None, list[str]]:
    result = build_deterministic_fallback(listing, prefilter, config, answers)
    if prefilter.facts.hidden_questions or prefilter.facts.hidden_commands:
        return None, ["hidden question or command requires answer-bank fallback"]
    return result.draft, result.blockers


def build_answer_bank_fallback(
    listing: SourceListing,
    prefilter: PrefilterResult,
    config: dict[str, Any],
    answers: dict[str, Any],
) -> tuple[MessageDraft | None, list[ResolvedQuestion], list[str]]:
    result = build_deterministic_fallback(listing, prefilter, config, answers)
    return result.draft, result.resolved_questions, result.blockers
