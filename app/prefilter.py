from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Literal

from .schemas import (
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


def _number(match: re.Match[str] | None) -> float | None:
    if not match:
        return None
    return float(match.group(1).replace(".", "").replace(",", "."))


def _money(text: str, labels: str) -> float | None:
    patterns = (
        rf"(?:{labels})\s*(?:beträgt|:|von|ca\.?|rund|ist)?\s*([0-9]{{2,4}}(?:[.,][0-9]{{1,2}})?)\s*(?:€|eur|euro)",
        rf"([0-9]{{2,4}}(?:[.,][0-9]{{1,2}})?)\s*(?:€|eur|euro)\s*(?:{labels})",
    )
    for pattern in patterns:
        value = _number(re.search(pattern, text, re.I))
        if value is not None:
            return value
    return None


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
    if re.search(r"\b(wg[- ]?zimmer|\d+er[- ]?wg|room in (?:a )?shared flat)\b", lower):
        return "wg_room"
    if "zwischenmiete" in lower or "sublet" in lower:
        return "zwischenmiete"
    if re.search(r"\b(studentenwohnheim|wohnheim|student room)\b", lower):
        return "student_room"
    if re.search(r"\b(studio|apartment)\b", lower):
        return "studio"
    if re.search(r"\b\d+(?:[.,]\d+)?[- ]zimmer[- ]wohnung\b|\bganze wohnung\b|whole flat", lower):
        return "whole_flat"
    if "nachmieter" in lower:
        return "nachmieter"
    return "unknown"


def _hidden_questions(text: str) -> list[HiddenQuestion]:
    candidates: list[str] = []
    for sentence in re.split(r"(?<=[?.!])\s+|[\r\n]+", normalized(text)):
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
    sentences = re.split(r"(?<=[.!?])\s+|[\r\n]+", normalized(text))
    for sentence in sentences:
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
    housing_type = _housing_type(text)
    warm = _money(lower, r"warmmiete|warm|gesamtmiete|all[- ]?in rent")
    if warm is None:
        match = re.search(r"\b([1-9][0-9]{2,3})\s*(?:€|eur|euro)\b", lower)
        warm = _number(match)
    cold = _money(lower, r"kaltmiete|kalt|net rent")
    deposit = _money(lower, r"kaution|deposit")
    takeover = _money(lower, r"ablöse|abschlag|furniture takeover")

    duration_match = re.search(
        r"(?:mindestens|minimum|min\.?|für)\s*(\d{1,2})\s*(?:monate|months)", lower
    )
    duration = int(duration_match.group(1)) if duration_match else None
    room_match = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*m[²2]", lower)
    total_rooms_match = re.search(r"\b(\d+(?:[.,]\d+)?)\s*[- ]?zimmer[- ]wohnung", lower)
    total_rooms = _number(total_rooms_match)

    women_only = bool(
        re.search(
            r"\b(?:nur|ausschließlich|only)\s+(?:für\s+)?(?:frauen|weiblich|female|women)\b"
            r"|\b(?:frau|mitbewohnerin|female tenant)\s+gesucht\b"
            r"|\b(?:keine männer|männer ausgeschlossen|no men)\b",
            lower,
        )
    )
    wbs_required = bool(
        re.search(r"\bwbs\s*(?:erforderlich|notwendig|pflicht|required)|nur mit wbs", lower)
    )
    age_match = re.search(
        r"(?:mindestens|minimum age|mindestalter|ab)\s*(\d{2})\s*(?:jahre|years|\+)?", lower
    )
    preference = bool(
        re.search(r"(?:idealerweise|bevorzugt|ungefähr|ca\.?|preferred).{0,25}\d{2}", lower)
    )
    explicit_age = int(age_match.group(1)) if age_match and not preference else None

    fraternity = bool(
        re.search(
            r"\b(?:katholisch\w*|christlich\w*|religiös\w*)\s+studentenverbindung\b"
            r"|\b(?:k\.?st\.?v\.?|unitas|burschenschaft|corps)\b",
            lower,
        )
    )
    confession = bool(
        re.search(
            r"(?:mitgliedschaft|bekenntnis|konfession|membership).{0,45}"
            r"(?:kirche|katholisch|christlich|religion|church)",
            lower,
        )
    )

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
    odd_payment_demands = [
        match.group(0)
        for match in re.finditer(
            r"\b(?:bearbeitungsgebühr|reservierungsgebühr|besichtigungsgebühr|western union|"
            r"moneygram|krypto(?:währung)?|cryptocurrency)\b",
            lower,
        )
    ]
    if odd_payment_demands and scam_risk == "low":
        scam_risk = "medium"
        scam_reasons.append("unusual fee or payment method")

    if re.search(r"\banmeldung\s+(?:nicht|unmöglich)|keine anmeldung|without registration", lower):
        anmeldung: Literal["yes", "no", "unknown"] = "no"
    elif re.search(r"anmeldung\s+(?:möglich|yes)|registration possible", lower):
        anmeldung = "yes"
    else:
        anmeldung = "unknown"

    advertiser: Literal["wg", "private_landlord", "company", "unknown"] = (
        "wg"
        if housing_type == "wg_room" or re.search(r"\bwir sind eine?\s+\d+er[- ]wg", lower)
        else "unknown"
    )
    if re.search(r"sehr geehrte|herrn?\s+[A-ZÄÖÜ]|frau\s+[A-ZÄÖÜ]|vermieter", text, re.I):
        advertiser = "private_landlord"

    facts = ListingFacts(
        listing_language=_language(text),
        advertiser_type=advertiser,
        housing_type=housing_type,
        warm_rent_eur=warm,
        cold_rent_eur=cold,
        deposit_eur=deposit,
        furniture_takeover_eur=takeover,
        room_size_m2=_number(room_match),
        total_rooms=total_rooms,
        realistic_residents=int(total_rooms)
        if housing_type == "whole_flat" and total_rooms
        else None,
        minimum_duration_months=duration,
        anmeldung=anmeldung,
        wbs_required=wbs_required,
        women_only=women_only,
        explicit_min_age=explicit_age,
        age_preference_only=preference,
        religion_or_confession_required=confession,
        religious_fraternity_or_membership_required=fraternity,
        buergschaft_required=bool(re.search(r"\bbürgschaft\b|guarantor", lower)),
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
        scam_risk=scam_risk,
        scam_reasons=scam_reasons,
        odd_fee_or_payment_demands=odd_payment_demands,
        hidden_questions=_hidden_questions(text),
        hidden_commands=_hidden_commands(text),
        confidence=0.72,
    )

    rules = config["housing_rules"]
    skips: list[str] = []
    warnings: list[str] = []
    if women_only:
        skips.append("women-only / men excluded")
    if wbs_required:
        skips.append("WBS required")
    if confession:
        skips.append("religion/confession membership required")
    if fraternity:
        skips.append("religious Studentenverbindung/fraternity obligations required")
    if explicit_age is not None and explicit_age >= 30:
        skips.append(f"mandatory minimum age {explicit_age}")
    if duration is not None and duration < int(rules["min_duration_months"]):
        skips.append(f"duration {duration} months below minimum")
    if high_scam:
        skips.append("high scam risk")
    if re.search(r"\b(?:couch|sofa|schlafplatz für wenige nächte)\b", lower):
        skips.append("temporary couch/sleeping place is not an eligible housing type")
    max_rent = float(rules["max_warm_rent_single_eur"])
    if warm is not None and housing_type != "whole_flat" and warm > max_rent:
        skips.append(f"warm rent €{warm:g} above €{max_rent:g}")
    if anmeldung == "no":
        warnings.append("no Anmeldung")
    if takeover is not None and takeover > 400:
        warnings.append(f"Ablöse €{takeover:g}")
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

    signals = [
        f"housing_type={housing_type}",
        f"language={facts.listing_language}",
        f"scam_risk={scam_risk}",
    ]
    if warm is not None:
        signals.append(f"warm_rent_eur={warm:g}")
    return PrefilterResult(
        facts=facts,
        hard_skip_reasons=skips,
        warnings=warnings,
        deterministic_signals=signals,
    )
