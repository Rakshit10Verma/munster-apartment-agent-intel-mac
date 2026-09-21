from __future__ import annotations

import re
import unicodedata
from typing import Any

from .answer_bank_resolver import match_known_question
from .message_policy import (
    GERMAN_WG_VIEWING_CLOSING,
    count_age_mismatch_mentions,
    has_age_mismatch_acknowledgement,
    is_formal_application_context,
)
from .schemas import (
    AttachmentDecision,
    HiddenCommand,
    ListingFacts,
    MessageDraft,
    RuleDecision,
    SourceListing,
    ValidationResult,
)


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _words(text: str) -> list[str]:
    return re.findall(r"\b[\wÄÖÜäöüß'-]+\b", text, re.UNICODE)


def _detect_language(text: str) -> str:
    lower = f" {_plain(text)} "
    de = sum(lower.count(f" {word} ") for word in ("ich", "und", "mein", "euch", "gerne", "zimmer"))
    en = sum(lower.count(f" {word} ") for word in ("i", "and", "my", "you", "room", "would"))
    return "en" if en > de + 1 else "de"


def _command_error(command: HiddenCommand, draft: MessageDraft) -> str | None:
    exact = (command.exact_text or "").strip()
    subject = draft.subject.strip()
    body = draft.body.strip()
    if command.kind in {"required_first_word", "exact_subject", "required_keyword"} and not exact:
        return f"command {command.id} has no deterministically verifiable exact text"
    if command.kind == "required_first_word":
        first = _words(body)
        expected = _words(exact)
        if first[: len(expected)] != expected:
            return f"required first word/text missing for command {command.id}"
    elif command.kind == "exact_subject" and _plain(subject) != _plain(exact):
        return f"exact subject missing for command {command.id}"
    elif command.kind == "required_keyword":
        haystack = subject if command.target == "subject" else body
        if command.target == "either":
            haystack = f"{subject}\n{body}"
        if _plain(exact) not in _plain(haystack):
            return f"required keyword missing for command {command.id}"
    elif command.kind == "length":
        number = re.search(r"(\d+)", command.instruction)
        if number:
            maximum = int(number.group(1))
            count = (
                len(body)
                if re.search(r"zeichen|characters", command.instruction, re.I)
                else len(_words(body))
            )
            if count > maximum:
                return f"length command {command.id} exceeded ({count}>{maximum})"
    elif command.kind == "other":
        return f"command {command.id} cannot be deterministically verified"
    return None


def _known_answer_present(question: str, body: str) -> bool | None:
    question = _plain(question)
    body = _plain(body)
    if re.search(r"über dich|about yourself|alltag|daily life", question):
        return "information systems" in body and "werkstudent" in body
    match = match_known_question(question)
    if match is None:
        return None
    return match.verify(body)


_TRAIT_WORD = (
    r"(?:ruhig\w*|entspannt\w*|gelassen\w*|verlässlich\w*|zuverlässig\w*|"
    r"ordentlich\w*|reif\w*|unkompliziert\w*)"
)
_TRAIT_LISTING_PATTERN = re.compile(
    rf"\b{_TRAIT_WORD}\b(?:[,\s]+(?:und\s+)?\b{_TRAIT_WORD}\b){{1,}}", re.I
)
_GENERIC_APPLICATION_PHRASE_PATTERN = re.compile(
    r"\bich bringe die nötige reife\b"
    r"|\bich erfülle (?:alle )?(?:die )?anforderungen\b"
    r"|\bich bin überzeugt\b.{0,40}\bpass(?:e|en)\b",
    re.I,
)


def _hook_present(hook: str, body: str) -> bool:
    stop = {
        "und",
        "oder",
        "der",
        "die",
        "das",
        "ein",
        "eine",
        "with",
        "the",
        "and",
        "room",
        "zimmer",
    }
    tokens = [token for token in _words(_plain(hook)) if len(token) >= 4 and token not in stop]
    body_plain = _plain(body)
    return bool(tokens) and any(token in body_plain for token in tokens)


def validate_message(
    listing: SourceListing,
    facts: ListingFacts,
    decision: RuleDecision,
    draft: MessageDraft | None,
    attachment: AttachmentDecision,
    config: dict[str, Any],
    provider_succeeded: bool = True,
    message_source: str = "cloud_ai",
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if not provider_succeeded:
        errors.append("provider failure")
    if decision.decision == "SKIP" or decision.hard_skip_reasons:
        errors.append("hard skip")
    if facts.scam_risk == "high":
        errors.append("high scam risk")
    elif facts.scam_risk == "medium":
        errors.append("ambiguous/medium scam risk requires review")
    if facts.critical_ambiguities:
        errors.append("critical listing ambiguities require review")
    elif facts.ambiguities:
        warnings.append("non-critical listing ambiguities recorded")
    if facts.unresolved_required_facts:
        errors.append("unresolved required listing facts")
    if facts.parental_guarantor_required and not config.get("applicant", {}).get(
        "parental_guarantor_confirmed", False
    ):
        errors.append("unresolved Elternbürgschaft requirement")
    if draft is None:
        errors.append("message missing")
        return ValidationResult(auto_send_allowed=False, errors=errors, warnings=warnings)

    required_questions = {item.id for item in facts.hidden_questions if item.required}
    known_questions = {item.id for item in facts.hidden_questions}
    answered = set(draft.answered_question_ids)
    missing_questions = required_questions - answered
    extra_answers = answered - known_questions
    if missing_questions:
        errors.append(f"unanswered hidden questions: {sorted(missing_questions)}")
    if extra_answers:
        errors.append(f"unexpected answered question ids: {sorted(extra_answers)}")
    for question in facts.hidden_questions:
        if not question.required or question.id not in answered:
            continue
        present = _known_answer_present(question.question, draft.body)
        if present is False:
            errors.append(f"answer not verifiable in body for question {question.id}")
        elif present is None:
            errors.append(f"answer semantics not deterministically verifiable for {question.id}")

    required_commands = {item.id for item in facts.hidden_commands if item.required}
    applied = set(draft.applied_command_ids)
    missing_commands = required_commands - applied
    extra_commands = applied - required_commands
    if missing_commands:
        errors.append(f"unapplied hidden commands: {sorted(missing_commands)}")
    if extra_commands:
        errors.append(f"unexpected applied command ids: {sorted(extra_commands)}")
    for command in facts.hidden_commands:
        if command.required and command.id in applied:
            error = _command_error(command, draft)
            if error:
                errors.append(error)

    if draft.unresolved_required_facts:
        errors.append("message has unresolved required personal facts")
    body = draft.body.strip()
    plain = _plain(body)
    formal = is_formal_application_context(listing, facts)
    if not body:
        errors.append("message body is empty")
    if facts.contact_method == "email" and not draft.subject.strip():
        errors.append("email subject is empty")
    if re.search(r"\b(?:remote[- ]mitarbeiter(?:in)?|remote employee)\b", plain):
        errors.append("forbidden Remote-Mitarbeiter wording")
    if re.search(r"\b(?:überwiegend|mostly|mainly)\s+remote\b", plain):
        errors.append("remote-work status contradicted: work is fully remote")
    if re.search(r"\blbs\b", plain):
        errors.append("forbidden LBS abbreviation")
    if not config.get("applicant", {}).get("parental_guarantor_confirmed", False) and re.search(
        r"\b(?:elternbürgschaft|bürgschaft|buergschaft|guarant(?:ee|or))\b", plain
    ):
        errors.append("unconfirmed guarantor mentioned in outgoing message")
    if listing.platform == "wg_gesucht" and "bewerbermappe" in plain:
        errors.append("WG message must not mention the separately attached Bewerbermappe")
    origin_pattern = (
        r"\b(?:ich (?:komme|stamme) aus indien|ich bin (?:ein )?inder|i am indian|from india)\b"
        r"|\b(?:herkunft|nationalit(?:ät|y)|origin).{0,35}(?:indien|india|indian)\b"
    )
    if not facts.origin_explicitly_required and re.search(origin_pattern, plain):
        errors.append("nationality/origin mentioned")
    if re.search(r"welches kartenspiel du am liebsten spielst.{0,12}ist", plain):
        errors.append("known unnatural card-game sentence")
    if re.search(
        r"lieblingsessen.{0,100}(?:zum einzug).{0,60}(?:selbst|auch).{0,25}koch",
        plain,
    ):
        errors.append("favorite-food answer adds an unnecessary self-cooking offer")
    if re.search(
        r"(?:damit (?:ihr|sie) (?:wisst|wissen).{0,55}(?:anzeige|inserat).{0,25}gelesen"
        r"|so (?:you|they) know i (?:have )?read.{0,25}(?:listing|ad))",
        plain,
    ):
        errors.append("proof-of-reading instruction awkwardly restated")
    claims_current_master = bool(
        re.search(r"\b(?:studiere|study(?:ing)?)\b.{0,45}\b(?:master|information systems)\b", plain)
    )
    says_starting = bool(
        re.search(
            r"\b(?:starte|beginne|werde).{0,45}\b(?:master|studium|studieren)\b"
            r"|\b(?:starting|will start|beginning).{0,45}\b(?:master|msc)\b",
            plain,
        )
    )
    if claims_current_master and not says_starting:
        errors.append("master's start timing contradicted")
    if re.search(
        r"^(?:ich bewerbe mich (?:sehr )?gerne|mit großem interesse|"
        r"hiermit bewerbe ich mich|dear sir or madam)",
        plain,
    ):
        errors.append("generic cover-letter opening")
    if re.search(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", body):
        errors.append("bullet points are not allowed in outgoing message")
    if not formal and _GENERIC_APPLICATION_PHRASE_PATTERN.search(plain):
        errors.append("generic/cover-letter-style phrasing detected; use natural spoken wording")
    trait_listings = _TRAIT_LISTING_PATTERN.findall(plain)
    if len(trait_listings) > 1:
        errors.append(
            "personality-trait listing (e.g. 'ruhig, verlässlich und ordentlich') is repeated "
            "more than once; keep it to a single natural mention"
        )

    word_count = len(_words(body))
    if word_count < 60:
        errors.append(f"message too short ({word_count} words)")
    if word_count > 300:
        errors.append(f"message absurdly long ({word_count} words)")
    generation = config["generation"]
    if formal:
        # A reasonable word-count target is a soft style guideline, not a hard gate:
        # naturalness matters more than hitting an exact range, so only note it as a
        # warning here. The unconditional too-short/absurdly-long checks above already
        # catch messages that are genuinely broken.
        minimum = int(generation["formal_words_min"])
        maximum = int(generation["formal_words_max"])
        if not minimum <= word_count <= maximum:
            warnings.append(
                f"formal message length {word_count} outside preferred {minimum}-{maximum}"
            )
    elif message_source in {"universal_fallback", "universal_answer_bank_fallback"}:
        if word_count > 300:
            errors.append(f"universal WG message excessively long ({word_count}>300)")
    else:
        # Naturalness matters more than a rigid word count: a longer message is fine
        # when it is genuinely earning its length (a real hook, a hidden-question
        # answer, an age/fit clarification), rather than being padded or repetitive.
        wg_min = int(generation["wg_words_min"])
        preferred_max = int(generation["wg_words_preferred_max"])
        soft_max = int(generation["wg_words_soft_max"])
        hard_max = int(generation["wg_words_hard_max"])
        hard_max_with_reason = int(generation["wg_words_hard_max_with_reason"])
        has_good_reason = bool(
            facts.age_mismatch or any(item.required for item in facts.hidden_questions)
        )
        effective_max = hard_max_with_reason if has_good_reason else hard_max
        has_quality_content = bool(draft.hooks_used) and (
            bool(draft.answered_question_ids) or has_good_reason
        )
        if word_count < wg_min:
            errors.append(f"WG message too short ({word_count}<{wg_min})")
        elif word_count > effective_max:
            errors.append(
                f"WG message excessively long ({word_count}>{effective_max}); trim redundant "
                "or generic content rather than just cutting length"
            )
        elif word_count > soft_max:
            warnings.append(
                f"WG message long ({word_count}>{soft_max}); consider trimming if not every "
                "sentence is necessary"
            )
        elif word_count > preferred_max and not has_quality_content:
            warnings.append(f"WG message above preferred length ({word_count}>{preferred_max})")

    detected = _detect_language(body)
    if facts.listing_language in {"de", "en"} and detected != facts.listing_language:
        errors.append("message/listing language mismatch")
    if draft.language != facts.listing_language:
        errors.append("declared message language mismatch")
    expected_register = "sie" if formal else "du"
    if facts.listing_language == "de" and draft.address_register != expected_register:
        errors.append(f"wrong register: expected {expected_register}")
    if expected_register == "du" and re.search(r"\b(?:Ihnen|Ihre[rmns]?|Sie)\b", body):
        errors.append("formal Sie wording in casual WG message")
    if expected_register == "sie" and re.search(r"\b(?:du|dir|dich|euch)\b", body, re.I):
        errors.append("informal wording in formal landlord message")
    if facts.age_mismatch:
        age_mentions = count_age_mismatch_mentions(body)
        if not has_age_mismatch_acknowledgement(body):
            errors.append("soft age mismatch is not acknowledged with maturity/fit reassurance")
        elif age_mentions > 1:
            errors.append(
                f"age mismatch personalization repeated {age_mentions} times; "
                "regenerate with a single mention"
            )
    required_closing = " ".join(_plain(GERMAN_WG_VIEWING_CLOSING).split())
    normalized_body = " ".join(plain.split())
    if (
        listing.platform == "wg_gesucht"
        and facts.listing_language == "de"
        and not formal
        and not normalized_body.endswith(required_closing)
    ):
        errors.append("required German WG viewing/closing block is missing or changed")

    if message_source not in {"universal_fallback", "universal_answer_bank_fallback"}:
        if not draft.hooks_used:
            errors.append("no listing-specific hook declared")
        elif not any(_hook_present(hook, body) for hook in draft.hooks_used):
            errors.append("listing-specific hook not verifiable in body")

    if attachment.should_attach and not attachment.allowed:
        errors.append("document policy violation")
    if facts.documents_explicitly_requested and not attachment.should_attach:
        errors.append("requested documents unavailable or blocked")
    if (
        listing.platform != "wg_gesucht"
        and facts.contact_method == "email"
        and attachment.should_attach
        and not facts.documents_explicitly_requested
    ):
        errors.append("email attachment was not explicitly requested")
    return ValidationResult(auto_send_allowed=not errors, errors=errors, warnings=warnings)
