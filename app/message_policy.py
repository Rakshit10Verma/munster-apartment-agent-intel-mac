# ruff: noqa: E501
from __future__ import annotations

import re

from .prefilter import company_self_identifies
from .schemas import ListingFacts, MessageDraft, SourceListing

GERMAN_WG_VIEWING_CLOSING = """Da ich aktuell noch in Berlin wohne, wäre ein erstes Kennenlernen per Video-Call für mich natürlich am einfachsten. Wenn eine Besichtigung vor Ort lieber ist, sagt mir einfach ein bisschen vorher Bescheid. Dann kann ich meine Tickets planen und in den nächsten Tagen nach Münster kommen.

Falls noch etwas offen ist, schreibt mir einfach gerne. Ich würde mich freuen, mehr über die Wohnung und das Zusammenleben zu erfahren :)

Liebe Grüße
Rakshit"""


def is_formal_application_context(listing: SourceListing, facts: ListingFacts) -> bool:
    """Classify the application context, not merely the portal account label.

    The company/agency signal requires genuine self-identification ("Hausverwaltung
    X vermietet/bietet/sucht ..."), matching prefilter.py's own advertiser
    classification -- a WG member's listing merely mentioning the building's
    Hausverwaltung as a third party (e.g. "du würdest mit der Hausverwaltung
    abgesprochen einen eigenen Untermietvertrag bekommen") never trips it, so it can
    safely stay checked before the WG signal without misreading that kind of mention
    as a company/agency listing.
    """
    text = f"{listing.title}\n{listing.raw_text}".casefold()
    if facts.advertiser_type == "company" or company_self_identifies(text):
        return True
    if (
        facts.advertiser_type == "wg"
        or facts.housing_type == "wg_room"
        or re.search(
            r"\b(?:\d+er[- ]?wg|wg[- ]?leben|mitbewohner|mitbewohni|"
            r"studenten[- ]?wg|keine zweck[- ]?wg|current flatmate|wg[- ]?zimmer)\b",
            text,
        )
    ):
        return False
    if facts.housing_type in {
        "studio",
        "whole_flat",
        "wohnheim",
        "student_room",
        "nachmieter",
    }:
        return True
    if facts.housing_type == "zwischenmiete":
        return not bool(re.search(r"\b(?:wg[- ]?zimmer|\d+er[- ]?wg|mitbewohner)\b", text))
    return facts.advertiser_type == "private_landlord"


_AGE_ACKNOWLEDGEMENT_PATTERN = re.compile(
    r"\b(?:jünger|altersunterschied|außerhalb.{0,20}alter|"
    r"auch wenn ich (?:erst )?2[12]|younger|age difference|outside.{0,20}age)\b",
    re.I,
)


def has_age_mismatch_acknowledgement(body: str) -> bool:
    plain = body.casefold()
    acknowledges_difference = bool(_AGE_ACKNOWLEDGEMENT_PATTERN.search(plain))
    reassures_fit = bool(
        re.search(
            r"\b(?:pass(?:e|en|t)|passen könnte|guter fit|good fit|fit in|fit as flatmates|"
            r"ruhig|verlässlich|zuverlässig|ordentlich|unkompliziert|entspannt|calm|reliable|"
            r"tidy|easygoing|laid-back)\b",
            plain,
        )
    )
    return acknowledges_difference and reassures_fit


def count_age_mismatch_mentions(body: str) -> int:
    """Count sentences acknowledging an age mismatch. Used to catch accidental
    repetition when a cloud provider already wrote its own age-mismatch sentence
    in addition to the deterministic one."""
    sentences = (s for s in re.split(r"(?<=[.!?])\s+", body) if s.strip())
    return sum(1 for sentence in sentences if _AGE_ACKNOWLEDGEMENT_PATTERN.search(sentence))


def _remove_age_mismatch_sentences(paragraph: str) -> str:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", paragraph) if s.strip()]
    kept = [s for s in sentences if not _AGE_ACKNOWLEDGEMENT_PATTERN.search(s)]
    return " ".join(kept).strip()


def _strip_ai_age_mismatch_mentions(body: str) -> str:
    """Remove any cloud-provider-authored age-mismatch sentence(s) so exactly one
    deterministic, canonical acknowledgement can be inserted afterward. Prevents the
    age mismatch from being mentioned twice with inconsistent wording."""
    paragraphs = re.split(r"\n\s*\n", body.strip())
    cleaned = [_remove_age_mismatch_sentences(paragraph) for paragraph in paragraphs]
    return "\n\n".join(paragraph for paragraph in cleaned if paragraph)


def _age_mismatch_note(facts: ListingFacts, *, formal: bool) -> str:
    age_range = (
        f"{facts.age_min} und {facts.age_max}"
        if facts.age_max is not None
        else f"{facts.age_min} oder älter"
    )
    if facts.listing_language == "en":
        english_range = (
            f"{facts.age_min}-{facts.age_max}" if facts.age_max is not None else f"{facts.age_min}+"
        )
        return (
            f"I'm 21, so a little younger than your preferred {english_range} age range, but I "
            "think we could still be a good fit as flatmates."
        )
    if formal:
        return (
            f"Mir ist bewusst, dass Sie eigentlich jemanden zwischen {age_range} suchen. Ich "
            "werde im Oktober 22 und bin damit etwas jünger, halte mich aber für zuverlässig und "
            "glaube, dass ich trotzdem gut zu Ihnen passen würde."
        )
    return (
        f"Mit 21 bin ich etwas jünger als eure Wunschspanne zwischen {age_range}, aber ich glaube, "
        "dass es menschlich trotzdem gut passen könnte."
    )


def enforce_message_policy(
    listing: SourceListing, facts: ListingFacts, draft: MessageDraft
) -> MessageDraft:
    """Apply deterministic wording that must not vary between cloud providers."""
    formal = is_formal_application_context(listing, facts)
    body = draft.body.strip()
    if facts.age_mismatch:
        body = _strip_ai_age_mismatch_mentions(body)
        note = _age_mismatch_note(facts, formal=formal)
        if body.endswith(GERMAN_WG_VIEWING_CLOSING):
            body = body[: -len(GERMAN_WG_VIEWING_CLOSING)].rstrip()
            body = f"{body}\n\n{note}\n\n{GERMAN_WG_VIEWING_CLOSING}"
        else:
            body = f"{body}\n\n{note}"
        draft = draft.model_copy(update={"body": body})
    if listing.platform != "wg_gesucht" or facts.listing_language != "de" or formal:
        return draft
    if draft.body.strip().endswith(GERMAN_WG_VIEWING_CLOSING):
        return draft

    paragraphs = re.split(r"\n\s*\n", draft.body.strip())
    kept: list[str] = []
    for paragraph in paragraphs:
        plain = paragraph.casefold()
        is_viewing_paragraph = bool(
            re.search(r"video[- ]?(?:call|anruf|treffen)|besichtigung|vor ort", plain)
        )
        is_signoff = bool(re.match(r"\s*(?:liebe|viele|herzliche|freundliche)\s+grüße\b", plain))
        if not is_viewing_paragraph and not is_signoff:
            kept.append(paragraph.strip())
    body = "\n\n".join([*kept, GERMAN_WG_VIEWING_CLOSING]).strip()
    return draft.model_copy(update={"body": body})
