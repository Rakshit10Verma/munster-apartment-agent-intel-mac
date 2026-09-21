from __future__ import annotations

import hashlib
import re
import unicodedata
from calendar import monthrange
from collections.abc import Callable
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Literal, TypeVar

from .schemas import (
    AgeRequirementStrength,
    HiddenCommand,
    HiddenQuestion,
    ListingFacts,
    PrefilterResult,
    Risk,
    SourceListing,
)


def normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def stable_id(prefix: str, text: str) -> str:
    digest = hashlib.sha256(normalized(text).casefold().encode()).hexdigest()[:10]
    return f"{prefix}_{digest}"


_T = TypeVar("_T")


def dedupe_similar(
    items: list[_T],
    *,
    text_of: Callable[[_T], str],
    id_of: Callable[[_T], str],
    threshold: float = 0.88,
) -> tuple[list[_T], dict[str, str]]:
    """Collapse near-duplicate hidden questions/commands into one canonical entry per
    cluster of highly similar text.

    A single underlying listing sentence sometimes ends up extracted twice with a
    one- or two-word difference: WG-Gesucht duplicates section text into a trailing
    concatenated block, and a cloud AI provider paraphrasing the same sentence gets a
    different stable-hash id than the deterministic extractor. Exact-string dedup
    misses both. This keeps the first-seen item per cluster and returns a mapping from
    every removed duplicate's id to the surviving id, so ids already referenced (e.g.
    in `answered_question_ids`) remain valid after merging. Conservative: a real,
    different question never crosses the similarity threshold by accident.
    """
    survivors: list[_T] = []
    survivor_texts: list[str] = []
    remap: dict[str, str] = {}
    for item in items:
        text = normalized(text_of(item)).casefold()
        match_index = next(
            (
                index
                for index, existing in enumerate(survivor_texts)
                if SequenceMatcher(None, text, existing).ratio() >= threshold
            ),
            None,
        )
        if match_index is None:
            survivors.append(item)
            survivor_texts.append(text)
        else:
            remap[id_of(item)] = id_of(survivors[match_index])
    return survivors, remap


def _number(match: re.Match[str] | None) -> float | None:
    if not match:
        return None
    return float(match.group(1).replace(".", "").replace(",", "."))


# ---- requirement stage (initial contact vs. later contract) -----------------------

_CONTRACT_STAGE_TRIGGER = re.compile(
    r"zum\s+\w*vertrag\b|beim?\s+vertragsabschluss|vor\s+(?:der\s+)?unterzeichnung|"
    r"nach\s+(?:der\s+)?zusage|bei\s+vertragsunterzeichnung|für\s+den\s+mietvertrag|"
    r"bei\s+einzug\b|später\s+(?:benötig\w*|nachreich\w*|einreich\w*)",
    re.I,
)
_CONTACT_STAGE_TRIGGER = re.compile(
    r"mit\s+(?:der\s+|deiner\s+)?bewerbung|bei\s+(?:der\s+|eurer\s+|deiner\s+)?bewerbung|"
    r"in\s+der\s+ersten\s+nachricht|"
    r"bitte\s+(?:gleich\s+|direkt\s+)?mit(?:schicken|senden)|"
    r"zusammen\s+mit\s+der\s+(?:anfrage|nachricht|bewerbung)",
    re.I,
)
# (matched keyword, human-readable label) -- used both to build the informational
# later_contract_requirements list and, in analyzer.reconcile_facts, to recognize and
# strip an AI note about the same document once the stage is known to be "contract".
LATER_STAGE_DOCUMENT_LABELS: tuple[tuple[str, str], ...] = (
    ("schufa", "SCHUFA"),
    ("haftpflicht", "private Haftpflichtversicherung"),
    ("bürgschaft", "Elternbürgschaft"),
    ("buergschaft", "Elternbürgschaft"),
    ("einkommensnachweis", "Einkommensnachweis"),
    ("gehaltsnachweis", "Einkommensnachweis"),
    ("foto", "aktuelles Foto"),
    ("photo", "aktuelles Foto"),
    ("lebenslauf", "Lebenslauf"),
    ("ausweis", "Ausweis/ID"),
    ("motivationsschreiben", "Motivationsschreiben"),
)


def requirement_stage(text: str) -> Literal["initial_contact", "contract", "unclear"]:
    """Classify whether document/proof requirements mentioned in the listing apply at
    first contact or only later, at contract stage. Conservative: an explicit
    contact-stage signal always wins (never silently waive something that IS asked
    for up front), and anything not clearly one or the other stays "unclear" so the
    existing cautious review behavior is preserved."""
    if _CONTACT_STAGE_TRIGGER.search(text):
        return "initial_contact"
    if _CONTRACT_STAGE_TRIGGER.search(text):
        return "contract"
    return "unclear"


def photo_required_for_initial_contact(text: str) -> bool:
    """Require an actual photo upload only for an explicit first-message demand.

    Generic portal document lists, later-contract requests and casual mentions of
    listing photos do not authorize sharing a personal image.
    """
    for sentence in re.split(r"[.!?\n]+", text.casefold()):
        if not re.search(r"\b(?:foto|photo|bild|picture)\b", sentence):
            continue
        if re.search(
            r"(?:bewerbung(?:en)?|anfrag(?:e|en)|nachricht(?:en)?)\s+ohne\s+"
            r"(?:ein\s+|ein\s+aktuelles\s+|aktuelles\s+)?(?:foto|photo|bild|picture)",
            sentence,
        ):
            return True
        if _CONTACT_STAGE_TRIGGER.search(sentence) and re.search(
            r"\b(?:schick\w*|send\w*|anhäng\w*|beifüg\w*|einreich\w*|"
            r"erforderlich|benötig\w*|required|attach|include)\b",
            sentence,
        ):
            return True
        if re.search(
            r"(?:foto|photo|bild|picture).{0,55}(?:ersten\s+nachricht|"
            r"ersten\s+bewerbung|first\s+(?:message|application))|"
            r"(?:ersten\s+nachricht|ersten\s+bewerbung|first\s+(?:message|application))"
            r".{0,55}(?:foto|photo|bild|picture)",
            sentence,
        ) and re.search(r"bitte|muss|erforderlich|required|attach|include|send|schick", sentence):
            return True
    return False


def _income_guarantor_alternative(text: str) -> bool:
    income = (
        r"(?:einkommensnachweis|gehaltsnachweis|verdienstnachweis|"
        r"salary\s+slip|proof\s+of\s+income)"
    )
    guarantor = r"(?:elternbürgschaft|bürgschaft|buergschaft|guarant(?:ee|or))"
    alternative = r"(?:oder|or|/)\s+(?:(?:ein|eine|einen|einem)\s+)?"
    return bool(
        re.search(rf"{income}\s+{alternative}{guarantor}", text, re.I)
        or re.search(rf"{guarantor}\s+{alternative}{income}", text, re.I)
    )


def _later_contract_requirements(text: str) -> list[str]:
    found: list[str] = []
    metadata = re.search(
        r"benötigte unterlagen\b(.*?)(?:wg-gesucht\+|insights zu diesem angebot)", text, re.I
    )
    for keyword, label in LATER_STAGE_DOCUMENT_LABELS:
        if keyword not in text:
            continue
        # The portal's generic 'Benötigte Unterlagen' list does not say these
        # documents must accompany the first WG message. Treat it as a later
        # application warning unless the ad itself explicitly asks for the same
        # document with the first message/application.
        outside_metadata = text.replace(metadata.group(0), " ") if metadata else text
        explicit_initial = any(
            _CONTACT_STAGE_TRIGGER.search(outside_metadata[max(0, m.start() - 95) : m.end() + 95])
            for m in re.finditer(re.escape(keyword), outside_metadata, re.I)
        )
        later = bool(metadata and keyword in metadata.group(1) and not explicit_initial)
        if requirement_stage(text) == "contract" and not explicit_initial:
            later = True
        if later and label not in found:
            found.append(label)
    return found


# A company/agency word alone is not enough: WG members often mention the building's
# Hausverwaltung only as a third party (e.g. "du würdest mit der Hausverwaltung
# abgesprochen einen eigenen Untermietvertrag bekommen"). Require the company to
# actually self-identify as the one taking the listing action ("Hausverwaltung X
# vermietet/bietet/sucht ..."), mirroring the existing private-landlord
# self-identification check just below.
COMPANY_SELF_IDENTIFICATION_PATTERN = re.compile(
    r"\b(?:hausverwaltung|immobilien\w*|makler\w*|wohnungsbaugesellschaft|"
    r"[\w-]*gmbh|gesellschaft\s+mbh|property management)\b"
    r"[^.!?\n]{0,40}\b(?:vermietet|bietet|sucht|verwaltet|vermittelt|anbietet)\b",
    re.I,
)


def company_self_identifies(text: str) -> bool:
    return bool(COMPANY_SELF_IDENTIFICATION_PATTERN.search(text))


def _money(text: str, labels: str) -> float | None:
    # `\b` after the label group matters: without it, "777€ ablösevereinbarung: n.a."
    # (WG-Gesucht's structured Kaution/Ablösevereinbarung fields, flattened onto one
    # line by whitespace normalization) would match "ablöse" as an unanchored prefix
    # of "ablösevereinbarung" and misattribute the *deposit* figure to the unrelated
    # Ablöse field -- even though that field explicitly says "n.a." right after.
    patterns = (
        rf"(?:{labels})\b\s*(?:beträgt|:|von|ca\.?|rund|ist)?\s*"
        rf"([0-9]{{2,4}}(?:[.,][0-9]{{1,2}})?)\s*(?:€|eur|euro)",
        rf"([0-9]{{2,4}}(?:[.,][0-9]{{1,2}})?)\s*(?:€|eur|euro)\s*(?:{labels})\b",
    )
    for pattern in patterns:
        value = _number(re.search(pattern, text, re.I))
        if value is not None:
            return value
    return None


_DIRECT_FEE_LABEL = re.compile(
    r"\b(?:möbelablöse|moebelabloese|zubehörablöse|zubehoerabloese|"
    r"möbelabschlag|moebelabschlag|ablöse|abloese|abschlag|"
    r"bearbeitungsgebühr|bearbeitungsgebuehr|"
    r"reservierungsgebühr|reservierungsgebuehr|besichtigungsgebühr|"
    r"besichtigungsgebuehr|servicegebühr|servicegebuehr|processing fee|"
    r"reservation fee|viewing fee|one[- ]time fee)\b",
    re.I,
)
_PROPERTY_FEE_LABEL = re.compile(
    r"\b(?:möbel|moebel|einrichtung|inventar|zubehör|zubehoer|furniture|accessories)\b",
    re.I,
)
_PROPERTY_FEE_QUALIFIER = re.compile(
    r"\b(?:ablöse|abloese|abschlag|übernahme|uebernahme|übernehmen|uebernehmen|"
    r"aufpreis|gegen\s+preis|kosten|preis|fee)\b",
    re.I,
)
_CURRENCY_AMOUNT = re.compile(
    r"(?<!\d)([0-9]{1,4}(?:[.,][0-9]{1,2})?)\s*(?:€|eur|euro)(?!\w)",
    re.I,
)
_RISKY_PAYMENT_METHOD = re.compile(
    r"\b(?:western union|moneygram|krypto(?:währung)?|cryptocurrency|bitcoin)\b",
    re.I,
)


def _amount_value(match: re.Match[str]) -> float:
    return float(match.group(1).replace(".", "").replace(",", "."))


def _fee_policy(
    text: str, acceptable_below_eur: float
) -> tuple[float | None, float | None, list[str]]:
    """Extract one-time fees and return totals plus only review-worthy demands.

    A known combined total strictly below the configured threshold is acceptable.
    Missing prices, totals at/above the threshold, and risky payment methods remain
    review-worthy. Ordinary mentions of furniture without a price or takeover term
    are not treated as demands.
    """
    normalized_text = unicodedata.normalize("NFKC", text)
    priced_demands: dict[tuple[int, int, int], tuple[float, bool]] = {}
    unknown_demands: list[str] = []

    for segment_index, segment in enumerate(re.split(r"[\r\n;!?]+|\.(?=\s|$)", normalized_text)):
        segment = segment.strip()
        if not segment:
            continue
        amounts = list(_CURRENCY_AMOUNT.finditer(segment))
        labels: list[tuple[re.Match[str], bool]] = []
        for match in _DIRECT_FEE_LABEL.finditer(segment):
            property_related = bool(re.search(r"ablöse|abloese|abschlag", match.group(0), re.I))
            labels.append((match, property_related))
        for match in _PROPERTY_FEE_LABEL.finditer(segment):
            nearby = segment[max(0, match.start() - 35) : match.end() + 35]
            if _PROPERTY_FEE_QUALIFIER.search(nearby) or _CURRENCY_AMOUNT.search(nearby):
                labels.append((match, True))

        for label, property_related in labels:
            candidates: list[tuple[int, re.Match[str]]] = []
            for amount in amounts:
                between = segment[
                    min(label.end(), amount.end()) : max(label.start(), amount.start())
                ]
                if re.search(
                    r"\b(?:warmmiete|kaltmiete|miete|kaution|nebenkosten)\b", between, re.I
                ):
                    continue
                distance = min(abs(amount.start() - label.end()), abs(label.start() - amount.end()))
                if distance <= 45:
                    candidates.append((distance, amount))
            if not candidates:
                label_text = label.group(0).casefold()
                # A furniture/inventory takeover mentioned without a fixed price is
                # normal, negotiable WG practice ("Abschlag je nachdem was du
                # übernimmst") and never implies the applicant has agreed to pay
                # anything or that payment is due before contact/viewing. Only
                # non-property, arbitrary-sounding fees (processing/reservation/
                # viewing fees) with no stated total remain review-worthy here; real
                # advance-payment scam signals are caught separately.
                if not property_related:
                    unknown_demands.append(f"{label_text} with no clear total")
                continue
            _, amount = min(candidates, key=lambda item: item[0])
            key = (segment_index, amount.start(), amount.end())
            current = priced_demands.get(key)
            value = _amount_value(amount)
            priced_demands[key] = (value, property_related or bool(current and current[1]))

    total = sum(value for value, _ in priced_demands.values()) if priced_demands else None
    takeover_values = [
        value for value, property_related in priced_demands.values() if property_related
    ]
    takeover_total = sum(takeover_values) if takeover_values else None
    review_demands = list(dict.fromkeys(unknown_demands))
    if total is not None and total >= acceptable_below_eur:
        review_demands.append(f"one-time fee total €{total:g}")
    review_demands.extend(
        match.group(0).casefold() for match in _RISKY_PAYMENT_METHOD.finditer(text)
    )
    return total, takeover_total, list(dict.fromkeys(review_demands))


def _german_date(text: str, label: str) -> date | None:
    match = re.search(rf"\b{label}\s*:?\s*(\d{{1,2}}\.\d{{1,2}}\.\d{{4}})\b", text, re.I)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%d.%m.%Y").date()
    except ValueError:
        return None


def _move_in_date(text: str) -> date | None:
    exact = _german_date(text, r"(?:frei\s+ab|verfügbar\s+ab|einzug\s+ab|ab)")
    if exact is not None:
        return exact
    match = re.search(
        r"\b(?:(?:frei|verfügbar|einzug|zimmer|wohnung)\s+)?(?:erst\s+)?ab\s+"
        r"(januar|februar|märz|april|mai|juni|juli|august|september|oktober|november|dezember)"
        r"\s+(20\d{2})\b",
        text,
        re.I,
    )
    if match:
        months = (
            "januar",
            "februar",
            "märz",
            "april",
            "mai",
            "juni",
            "juli",
            "august",
            "september",
            "oktober",
            "november",
            "dezember",
        )
        return date(int(match.group(2)), months.index(match.group(1).casefold()) + 1, 1)
    return None


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _months_between(start: date, end_inclusive: date) -> int:
    """Whole calendar months from `start` through `end_inclusive` (both inclusive),
    matching the "01.10-31.03 is six months" convention used elsewhere. Used only to
    estimate a Zwischenmiete's duration when the listing states move-in/end dates
    instead of an explicit "für X Monate" duration."""
    target = end_inclusive + timedelta(days=1)
    months = (target.year - start.year) * 12 + (target.month - start.month)
    if target.day < start.day:
        months -= 1
    return max(months, 0)


def _age_policy(
    text: str, applicant_age: int
) -> tuple[int | None, int | None, AgeRequirementStrength, bool]:
    age_min: int | None = None
    age_max: int | None = None
    age_context = ""
    range_patterns = (
        r"\bzwischen\s+(\d{2})\s+(?:und|bis|[-–])\s*(\d{2})(?:\s+jahre[n]?)?\b",
        r"\b(\d{2})\s*[-–]\s*(\d{2})\s*(?:jahre[n]?|jährige[n]?|gesucht)\b",
        r"\b(?:am liebsten|idealerweise|bevorzugt|gesucht(?: wird)?)\D{0,30}"
        r"(\d{2})\s*[-–]\s*(\d{2})\b",
    )
    for pattern in range_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            age_min, age_max = int(match.group(1)), int(match.group(2))
            age_context = text[max(0, match.start() - 50) : match.end() + 50]
            break
    if age_min is None:
        minimum = re.search(
            r"\b(?:mindestalter\s*(\d{2})|mindestens\s*(\d{2})\s*(?:jahre[n]?|years?)|"
            r"ab\s*(\d{2})\s*(?:jahre[n]?|years?|\+)?)\b",
            text,
            re.I,
        )
        if minimum:
            age_min = int(next(group for group in minimum.groups() if group is not None))
            age_context = text[max(0, minimum.start() - 50) : minimum.end() + 50]

    preference = bool(
        re.search(
            r"\b(?:am liebsten|idealerweise|bevorzugt|ungefähr|circa|ca\.?|"
            r"around our age|preferably)\b",
            age_context,
            re.I,
        )
        or re.search(r"\b(?:in|ungefähr in) unserem alter\b", text, re.I)
    )
    strict = age_min is not None and bool(
        re.search(
            r"\b(?:nur|ausschließlich)\s+(?:(?:bewerber|personen|menschen)\s+)?"
            r"(?:ab\s*)?\d{2}\b"
            r"|\b(?:mindestalter|mindestens|ab)\s*\d{2}.{0,35}"
            r"(?:zwingend|verpflichtend|keine ausnahmen|strict(?:ly)? required)\b"
            r"|\bbitte\s+nur.{0,45}\b(?:ab|mindestalter)\s*\d{2}.{0,30}"
            r"(?:anschreiben|bewerben|apply)\b",
            text,
            re.I,
        )
    )
    if strict:
        strength: AgeRequirementStrength = "strict"
    elif age_min is not None or age_max is not None:
        strength = "preference" if preference else "soft"
    elif preference:
        strength = "preference"
    else:
        strength = "unknown"
    mismatch = bool(
        (age_min is not None and applicant_age < age_min)
        or (age_max is not None and applicant_age > age_max)
    )
    return age_min, age_max, strength, mismatch


def _language(text: str) -> Literal["de", "en", "other"]:
    lower = f" {text.casefold()} "
    german = sum(lower.count(f" {word} ") for word in ("und", "wir", "das", "mit", "zimmer"))
    english = sum(lower.count(f" {word} ") for word in ("and", "we", "the", "with", "room"))
    return "en" if english > german + 1 else "de"


def _housing_type(
    text: str,
) -> Literal[
    "wg_room",
    "studio",
    "whole_flat",
    "zwischenmiete",
    "wohnheim",
    "nachmieter",
    "student_room",
    "other",
    "unknown",
]:
    lower = text.casefold()
    if (
        "zwischenmiete" in lower
        or "sublet" in lower
        or "untermiete" in lower
        or "untervermiete" in lower
        or re.search(r"\bfrei\s+bis\s*:\s*\d{1,2}\.\d{1,2}\.\d{4}", lower)
    ):
        return "zwischenmiete"
    if re.search(r"\b(wg[- ]?zimmer|\d+er[- ]?wg|room in (?:a )?shared flat)\b", lower):
        return "wg_room"
    if re.search(r"\b(studentenwohnheim|wohnheim|student room)\b", lower):
        return "student_room"
    if re.search(r"\b(studio|apartment)\b", lower):
        return "studio"
    if re.search(r"\b\d+(?:[.,]\d+)?[- ]zimmer[- ]wohnung\b|\bganze wohnung\b|whole flat", lower):
        return "whole_flat"
    if "nachmieter" in lower:
        return "nachmieter"
    return "unknown"


def _furnishing(text: str) -> Literal["furnished", "partly_furnished", "unfurnished", "unknown"]:
    lower = text.casefold()
    if re.search(r"\b(?:unmöbliert|unmoebliert|unfurnished)\b", lower):
        return "unfurnished"
    if re.search(r"\b(?:teilmöbliert|teilmoebliert|partly furnished)\b", lower):
        return "partly_furnished"
    if re.search(r"\b(?:möbliert|moebliert|furnished)\b", lower):
        return "furnished"
    return "unknown"


def _location(text: str) -> str | None:
    for pattern in (
        r"\b(?:stadtteil|lage|ort)\s*:\s*([^\n,;|]{2,60})",
        r"\bMünster[- ]([A-ZÄÖÜ][A-Za-zÄÖÜäöüß-]{2,30})\b",
    ):
        match = re.search(pattern, text)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" .")
            return f"Münster-{value}" if pattern.startswith(r"\bMünster") else value
    if re.search(r"\bMünster\b", text, re.I):
        return "Münster"
    return None


def optional_example_question(question: str, listing_text: str) -> bool:
    """A question introduced as an example ("z.B.") is not a mandatory answer.

    Match the question to the *same* example clause; a mandatory question elsewhere
    in the ad must remain required. This does not supply or infer a personal answer.
    """
    if re.search(r"\b(?:unbedingt|zwingend|pflicht|required|must)\b", question, re.I):
        return False
    question_words = {
        word for word in re.findall(r"[\wäöüß]+", question.casefold()) if len(word) >= 3
    }
    for cue in re.finditer(
        r"(?:\bz\.\s*b\.?(?!\w)|\bzum\s+beispiel\b|\bbeispielsweise\b|"
        r"\bfor\s+example\b|\be\.\s*g\.?(?!\w))",
        listing_text,
        re.I,
    ):
        clause = listing_text[cue.end() : cue.end() + 350].split("?", 1)[0]
        if re.search(r"\b(?:unbedingt|zwingend|pflicht|required|must)\b", clause, re.I):
            continue
        clause_words = {
            word for word in re.findall(r"[\wäöüß]+", clause.casefold()) if len(word) >= 3
        }
        shared = question_words & clause_words
        if len(shared) >= 2 and any(len(word) >= 5 for word in shared):
            return True
    return False


def _hidden_questions(text: str) -> list[HiddenQuestion]:
    candidates: list[str] = []
    # Keep an optional-example clause intact: sentence splitting at "z.B." used
    # to hide its following question from deterministic extraction entirely.
    question_text = re.sub(r"\bz\.\s*b\.", "zum Beispiel", normalized(text), flags=re.I)
    question_text = re.sub(r"\bca\.", "circa", question_text, flags=re.I)
    for sentence in re.split(r"(?<=[?.!])\s+|[\r\n]+", question_text):
        sentence = sentence.strip(" -•\t")
        lower = sentence.casefold()
        question_like = "?" in sentence or bool(
            re.search(
                r"\b(?:schreib|sag|tell|write|beantworte|answer)\w*\b.{0,70}"
                r"\b(?:welch|was|wie|warum|ob|what|which|how|why|whether)\w*\b",
                lower,
            )
        )
        proof_like = bool(
            re.search(
                r"gelesen|proof|damit wir wissen|in deiner nachricht|bewerbung|anschreiben|message",
                lower,
            )
        )
        if question_like and (
            proof_like or re.search(r"\b(?:schreib|sag|tell|answer)\w*\b", lower)
        ):
            candidates.append(sentence)
    seen: set[str] = set()
    questions: list[HiddenQuestion] = []
    for candidate in candidates:
        key = normalized(candidate).casefold()
        if key not in seen:
            seen.add(key)
            questions.append(
                HiddenQuestion(id=stable_id("q", candidate), question=candidate, required=True)
            )
    return questions


def _quoted_value(text: str) -> str | None:
    match = re.search(r"[\"„“']([^\"„“']{1,80})[\"„“']", text)
    return match.group(1).strip() if match else None


def _hidden_commands(text: str) -> list[HiddenCommand]:
    commands: list[HiddenCommand] = []
    seen: set[str] = set()
    sentences = re.split(r"(?<=[.!?])\s+|[\r\n]+", normalized(text))
    for sentence in sentences:
        key = normalized(sentence).casefold()
        if key in seen:
            continue
        seen.add(key)
        lower = sentence.casefold()
        kind: (
            Literal[
                "required_first_word",
                "exact_subject",
                "required_keyword",
                "length",
                "other",
            ]
            | None
        ) = None
        target: Literal["subject", "body", "either"] = "body"
        if re.search(
            r"(?:beginne|starte|anfangen|start).{0,45}(?:nachricht|message|wort|word)", lower
        ):
            kind = "required_first_word"
        elif re.search(r"(?:betreff|subject).{0,50}(?:lauten|verwende|use|sein|:)", lower):
            kind = "exact_subject"
            target = "subject"
        elif re.search(r"(?:schlüsselwort|keyword|codewort|kennwort).{0,50}", lower):
            kind = "required_keyword"
            target = "either"
        elif re.search(
            r"(?:höchstens|max(?:imal)?|nicht mehr als)\s+\d+\s+(?:wörter|words|zeichen)", lower
        ):
            kind = "length"
        if kind:
            exact = _quoted_value(sentence)
            if exact is None and kind == "required_keyword":
                match = re.search(
                    r"(?:schlüsselwort|keyword|codewort|kennwort)\s*(?:ist|:)?\s*([\w-]+)",
                    sentence,
                    re.I,
                )
                exact = match.group(1) if match else None
            if exact is None and kind == "required_first_word":
                # Real listings overwhelmingly state the required word plainly
                # ("... mit dem Wort Sonnenblume"), not in quotes.
                match = re.search(
                    r"(?:mit\s+dem\s+wort|mit\s+dem\s+begriff|with\s+the\s+word)\s+"
                    r"([A-Za-zÄÖÜäöüß][\wÄÖÜäöüß-]*)",
                    sentence,
                    re.I,
                )
                exact = match.group(1).strip(" .,!?") if match else None
            commands.append(
                HiddenCommand(
                    id=stable_id("cmd", sentence),
                    instruction=sentence,
                    kind=kind,
                    exact_text=exact,
                    target=target,
                )
            )
    return commands


def deterministic_prefilter(listing: SourceListing, config: dict[str, Any]) -> PrefilterResult:
    text = normalized(f"{listing.title}\n{listing.raw_text}")
    lower = text.casefold()
    rules = config["housing_rules"]
    fee_threshold = float(rules["warnings"]["one_time_fee_review_at_or_above_eur"])
    housing_type = _housing_type(text)
    furnishing = _furnishing(text)
    location = _location(f"{listing.title}\n{listing.raw_text}")
    warm = _money(lower, r"warmmiete|warm|gesamtmiete|all[- ]?in rent")
    if warm is None:
        match = re.search(r"\b([1-9][0-9]{2,3})\s*(?:€|eur|euro)\b", lower)
        warm = _number(match)
    cold = _money(lower, r"kaltmiete|kalt|net rent")
    deposit = _money(lower, r"kaution|deposit")
    takeover = _money(lower, r"ablöse|abschlag|furniture takeover")
    one_time_fee, detected_takeover, review_fee_demands = _fee_policy(
        f"{listing.title}\n{listing.raw_text}", fee_threshold
    )
    if takeover is None:
        takeover = detected_takeover

    duration_match = re.search(
        r"(?:mindestens|minimum|min\.?|für)\s*(\d{1,2})\s*(?:monat(?:e)?|months?)\b",
        lower,
    )
    duration = int(duration_match.group(1)) if duration_match else None
    move_in = _move_in_date(lower)
    end_date = _german_date(lower, r"frei\s+bis")
    room_match = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*m[²2]", lower)
    total_rooms_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*[- ]?zimmer[- ]wohnung", lower)
    total_rooms = _number(total_rooms_match)

    women_only = bool(
        re.search(
            r"\b(?:nur|ausschließlich|only)\s+(?:für\s+)?(?:frauen|weiblich|female|women)\b"
            r"|\b(?:frau|mitbewohnerin|female tenant)\s+gesucht\b"
            r"|\bfrauen[- ]wg\b"
            r"|\bfrau\s+(?:zwischen|ab|bis|im alter von)\s*\d{2}\b"
            r"|\bgesucht\s+wird\s*:?\s*(?:eine\s+)?frau\b"
            r"|\b(?:keine männer|männer ausgeschlossen|no men)\b",
            lower,
        )
    )
    wbs_required = bool(
        re.search(r"\bwbs\s*(?:erforderlich|notwendig|pflicht|required)|nur mit wbs", lower)
    )
    applicant_age = int(config["applicant"]["age"])
    age_min, age_max, age_strength, age_mismatch = _age_policy(lower, applicant_age)
    income_alternative = _income_guarantor_alternative(lower)
    parental_guarantor_required = bool(
        re.search(r"\belternbürgschaft\b|\bparental\s+guarant(?:ee|or)\b", lower)
        and not income_alternative
    )

    # Merely living in a Studentenverbindung house is not proof that membership
    # or a confession is mandatory. DB 88 explicitly allows a semester without
    # joining; a broad keyword skip lost that inexpensive opportunity.
    fraternity = bool(
        re.search(
            r"(?:mitgliedschaft|beitritt|beitreten|mitglied\s+werden).{0,75}"
            r"(?:pflicht|muss|erforderlich|voraussetzung|bedingung)"
            r"|(?:pflicht|muss|erforderlich|voraussetzung|bedingung).{0,75}"
            r"(?:mitgliedschaft|beitritt|beitreten|mitglied\s+werden)"
            r"|(?:katholisch|christlich|religiös)\w*\s+studentenverbindung\s+"
            r"sucht\s+(?:ein\s+)?neues?\s+mitglied",
            lower,
        )
    ) and not bool(re.search(r"(?:ohne\s+beizutreten|ohne\s+mitglied\s+zu\s+werden)", lower))
    confession = bool(
        re.search(
            r"(?:mitgliedschaft|bekenntnis|konfession|membership).{0,45}"
            r"(?:kirche|katholisch|christlich|religion|church)"
            r"|(?:zimmer|räume?|vermiet\w*).{0,80}(?:an|für)\s+"
            r"(?:männliche,?\s+)?(?:katholische|christliche)\s+(?:studenten|personen)",
            lower,
        )
    )
    fraternity = fraternity or bool(confession and re.search(r"\bverbindung\b", lower))

    critical_process_ambiguities: list[str] = []
    if end_date is not None:
        contract_end = re.search(
            r"\bbis\s+(?:maximal\s+)?zum\s+(\d{1,2}\.\d{1,2}\.\d{4})\s+befristet",
            lower,
        )
        if contract_end:
            try:
                parsed = datetime.strptime(contract_end.group(1), "%d.%m.%Y").date()
            except ValueError:
                parsed = None
            if parsed is not None and abs((parsed - end_date).days) > 30:
                critical_process_ambiguities.append(
                    "listing availability end date conflicts with contract end date"
                )
    if re.search(
        r"bewerbungsverfahren\s+auf\s+der\s+homepage|"
        r"bewerbung(?:en)?\s+(?:nur|ausschließlich)\s+(?:über|via)\s+(?:die\s+)?"
        r"(?:website|homepage|webseite)|"
        r"(?:externen|separaten)\s+bewerbungsbogen\s+(?:ausfüllen|einreichen)",
        lower,
    ):
        critical_process_ambiguities.append("separate mandatory external application process")

    scam_reasons: list[str] = []
    if re.search(r"(?:vermieter|landlord).{0,50}(?:ausland|abroad)", lower):
        scam_reasons.append("landlord claims to be abroad")
    if re.search(r"(?:schlüssel|keys?).{0,45}(?:post|courier|mail)", lower):
        scam_reasons.append("keys offered by post")
    if re.search(
        r"(?:zahlung|überweisung|payment|deposit).{0,60}(?:vor|before).{0,30}(?:besichtigung|viewing)",
        lower,
    ):
        scam_reasons.append("payment demanded before viewing")
    if re.search(r"(?:keine|ohne|no)\s+(?:besichtigung|viewing)", lower):
        scam_reasons.append("viewing refused")
    high_scam = "payment demanded before viewing" in scam_reasons or (
        "landlord claims to be abroad" in scam_reasons and "keys offered by post" in scam_reasons
    )
    scam_risk: Risk = "high" if high_scam else ("medium" if scam_reasons else "low")
    odd_payment_demands = review_fee_demands
    if odd_payment_demands and scam_risk == "low":
        scam_risk = "medium"
        scam_reasons.append("unusual fee or payment method")

    if re.search(r"\banmeldung\s+(?:nicht|unmöglich)|keine anmeldung|without registration", lower):
        anmeldung: Literal["yes", "no", "unknown"] = "no"
    elif re.search(r"anmeldung\s+(?:möglich|yes)|registration possible", lower):
        anmeldung = "yes"
    else:
        anmeldung = "unknown"

    # WG context is detected from several independent structural/textual signals, not
    # one brittle keyword, and once found it is sticky: a stray "Herr"/"Frau" mention
    # elsewhere in the text (a roommate's name, a demographic breakdown like "1 Frau
    # und 1 Mann") must never downgrade an otherwise-clear WG listing to
    # private_landlord, since that previously forced an incorrect formal "Sie"
    # register onto a message that correctly used "ihr/euch". company_signal itself
    # requires genuine self-identification (see company_self_identifies), so a WG
    # member's listing mentioning the building's Hausverwaltung only as a third party
    # never trips it in the first place -- company_signal is checked first only
    # because, when it IS true, it is the more specific and authoritative signal
    # (e.g. an agency explicitly renting out a room that also reads like a WG room).
    company_signal = company_self_identifies(lower)
    wg_signal = bool(
        housing_type == "wg_room"
        or re.search(
            r"\bwir sind eine?\s+\d+er[- ]wg\b"
            r"|\bdie\s+wg\s*:"
            r"|\bwg[- ]leben\b|\bwg[- ]details\b|\bwg[- ]zimmer\b"
            r"|\bwir,\s*das sind\b"
            r"|\bunsere\s+wg\b|\bunser\s+wg[- ]leben\b"
            r"|\bdu\s+würdest\s+mit\b.{0,60}\bzusammenwohnen\b"
            r"|\bmitbewohner(?:in)?\b|\bmitbewohni\b"
            r"|\b\d+er[- ]wg\b",
            lower,
        )
    )
    advertiser: Literal["wg", "private_landlord", "company", "unknown"] = "unknown"
    if company_signal:
        advertiser = "company"
    elif wg_signal:
        advertiser = "wg"
    else:
        formal_salutation = bool(
            re.search(
                r"\bsehr geehrte\b|\bprivat\w*\s+vermieter\b|"
                r"\bvermieter(?:in)?\s+(?:vermietet|bietet|sucht)\b",
                lower,
            )
        )
        named_formal_person = bool(re.search(r"\b(?:Herr|Frau)\s+[A-ZÄÖÜ][a-zäöüß]{2,}\b", text))
        if formal_salutation or named_formal_person:
            advertiser = "private_landlord"

    facts = ListingFacts(
        listing_language=_language(text),
        advertiser_type=advertiser,
        housing_type=housing_type,
        location=location,
        furnishing=furnishing,
        warm_rent_eur=warm,
        cold_rent_eur=cold,
        deposit_eur=deposit,
        furniture_takeover_eur=takeover,
        one_time_fee_eur=one_time_fee,
        room_size_m2=_number(room_match),
        total_rooms=total_rooms,
        realistic_residents=int(total_rooms)
        if housing_type == "whole_flat" and total_rooms
        else None,
        move_in=move_in.isoformat() if move_in else None,
        end_date=end_date.isoformat() if end_date else None,
        minimum_duration_months=duration,
        anmeldung=anmeldung,
        wbs_required=wbs_required,
        women_only=women_only,
        age_min=age_min,
        age_max=age_max,
        age_requirement_strength=age_strength,
        age_mismatch=age_mismatch,
        explicit_min_age=age_min,
        age_preference_only=age_strength == "preference",
        religion_or_confession_required=confession,
        religious_fraternity_or_membership_required=fraternity,
        buergschaft_required=bool(re.search(r"\bbürgschaft\b|guarantor", lower)),
        parental_guarantor_required=parental_guarantor_required,
        income_or_guarantor_accepted=income_alternative,
        photo_required_with_first_message=photo_required_for_initial_contact(listing.raw_text),
        indexmiete=bool(re.search(r"\bindexmiete\b|index-linked rent", lower)),
        hauptmieter_liability=bool(re.search(r"\bhauptmieter\b|main tenant liability", lower)),
        contact_email=listing.contact_email,
        contact_method=(
            "email"
            if listing.contact_email
            else ("platform" if listing.platform in {"wg_gesucht", "kleinanzeigen"} else "unknown")
        ),
        documents_explicitly_requested=(
            ["Bewerbermappe"]
            if re.search(
                r"(?:bewerbermappe|schufa|gehaltsnachweis).{0,60}(?:mitsenden|anhängen|send|attach)",
                lower,
            )
            else []
        ),
        later_contract_requirements=_later_contract_requirements(lower),
        scam_risk=scam_risk,
        scam_reasons=scam_reasons,
        critical_ambiguities=critical_process_ambiguities,
        odd_fee_or_payment_demands=odd_payment_demands,
        hidden_questions=[
            question.model_copy(
                update={
                    "required": not optional_example_question(question.question, listing.raw_text)
                }
            )
            for question in dedupe_similar(
                _hidden_questions(text), text_of=lambda q: q.question, id_of=lambda q: q.id
            )[0]
        ],
        hidden_commands=dedupe_similar(
            _hidden_commands(text), text_of=lambda c: c.instruction, id_of=lambda c: c.id
        )[0],
        confidence=0.72,
    )
    if (
        income_alternative
        and re.search(
            r"\b(?:zwei|drei|vier|[2-9])\s+"
            r"(?:gehaltsabrechnungen|gehaltsnachweise|gehälter|salary\s+slips)\b",
            lower,
        )
        and "Einkommensnachweis" not in facts.later_contract_requirements
    ):
        facts.unresolved_required_facts.append(
            "Multiple income-proof documents requested; only one salary slip is confirmed "
            "in the Bewerbermappe"
        )

    skips: list[str] = []
    warnings: list[str] = []
    if women_only:
        skips.append("women-only / men excluded")
    unavailable_from = config.get("applicant", {}).get("move_in", {}).get("unavailable_from")
    if move_in is not None and unavailable_from:
        try:
            if move_in >= date.fromisoformat(str(unavailable_from)):
                skips.append(
                    f"availability starts {move_in.isoformat()}, after applicant's useful window"
                )
        except ValueError:
            pass
    if wbs_required:
        skips.append("WBS required")
    if confession:
        skips.append("religion/confession membership required")
    if fraternity:
        skips.append("religious Studentenverbindung/fraternity obligations required")
    # Age mismatch is never a hard skip, regardless of how strictly the listing states
    # its preferred range -- see the soft-warning handling below instead.
    duration_minimum = int(
        rules["min_zwischenmiete_months"]
        if housing_type == "zwischenmiete"
        else rules["min_duration_months"]
    )
    if duration is not None and duration < duration_minimum:
        skips.append(f"duration {duration} months below minimum")
    if housing_type == "zwischenmiete":
        # Zwischenmiete is judged by a MAXIMUM duration, not a minimum: a genuinely
        # short-term sublet is the whole point. A longer temporary sublet is only
        # accepted when the rent is cheap enough to justify the shorter commitment.
        # When no explicit "für X Monate" text is given, estimate the duration from
        # the listing's own move-in/end dates rather than skipping the check entirely.
        zwischenmiete_duration = duration
        if zwischenmiete_duration is None and move_in is not None and end_date is not None:
            zwischenmiete_duration = _months_between(move_in, end_date)
        max_months = int(rules["zwischenmiete_max_duration_months"])
        price_exception = float(rules["zwischenmiete_price_exception_below_eur"])
        if (
            zwischenmiete_duration is not None
            and zwischenmiete_duration > max_months
            and not (warm is not None and warm < price_exception)
        ):
            skips.append(
                f"Zwischenmiete duration {zwischenmiete_duration} months exceeds the "
                f"{max_months}-month limit (rent not below €{price_exception:g})"
            )
    if end_date is not None:
        preferred_text = str(config.get("applicant", {}).get("move_in", {}).get("preferred", ""))
        try:
            preferred_move_in = date.fromisoformat(preferred_text)
        except ValueError:
            preferred_move_in = None
        effective_move_in = max(
            candidate for candidate in (move_in, preferred_move_in) if candidate is not None
        )
        # Rental ranges conventionally include the stated final day: 01.10-31.03
        # is six months, while an end date earlier than 31.03 is not.
        minimum_end_date = _add_months(effective_move_in, duration_minimum) - timedelta(days=1)
        if end_date < minimum_end_date:
            skips.append(f"fixed availability ends before the {duration_minimum}-month minimum")
    if re.search(
        r"\b(?:ein\s+paar\s+tage|tage\s+oder\s+wochen|für\s+\d{1,2}\s+wochen|"
        r"wenige\s+wochen|short[- ]term.{0,20}(?:days|weeks))\b",
        lower,
    ):
        skips.append("duration of only days/weeks below minimum")
    if high_scam:
        skips.append("high scam risk")
    if re.search(
        r"\b(?:couch|sofa|schlafplatz)\b.{0,60}"
        r"(?:wenige nächte|ein paar nächte|übernacht\w*|schlafplatz|sleeping place)",
        lower,
    ):
        skips.append("temporary couch/sleeping place is not an eligible housing type")
    max_rent = float(rules["max_warm_rent_single_eur"])
    if warm is not None and housing_type != "whole_flat" and warm > max_rent:
        skips.append(f"warm rent €{warm:g} above €{max_rent:g}")
    if anmeldung == "no":
        warnings.append("no Anmeldung")
    if one_time_fee is not None and one_time_fee >= fee_threshold:
        warnings.append(f"one-time fee €{one_time_fee:g}")
    if facts.buergschaft_required:
        warnings.append("Bürgschaft required")
    if facts.indexmiete:
        warnings.append("Indexmiete")
    if facts.hauptmieter_liability:
        warnings.append("Hauptmieter liability")
    if scam_risk == "medium":
        warnings.extend(scam_reasons)
    if odd_payment_demands:
        warnings.append("odd fee/payment demand: " + ", ".join(odd_payment_demands))
    if age_mismatch:
        range_text = f"{age_min}-{age_max}" if age_max is not None else f"{age_min}+"
        warnings.append(
            f"soft age mismatch: applicant age {applicant_age} outside stated {range_text}; "
            "acknowledge maturity and fit naturally"
        )

    signals = [
        f"housing_type={housing_type}",
        f"language={facts.listing_language}",
        f"scam_risk={scam_risk}",
    ]
    if warm is not None:
        signals.append(f"warm_rent_eur={warm:g}")
    if move_in is not None:
        signals.append(f"move_in={move_in.isoformat()}")
    if end_date is not None:
        signals.append(f"end_date={end_date.isoformat()}")
    if age_strength != "unknown":
        signals.append(f"age_requirement_strength={age_strength}")
    if age_mismatch:
        signals.append("age_mismatch=true")
    if furnishing != "unknown":
        signals.append(f"furnishing={furnishing}")
    return PrefilterResult(
        facts=facts,
        hard_skip_reasons=skips,
        warnings=warnings,
        deterministic_signals=signals,
    )
