# ruff: noqa: E501
"""Deterministic resolution of hidden listing questions/commands against the confirmed
answer bank (`answer_bank.yaml`). Used only as a constrained fallback when every cloud
AI provider has failed; never invents facts outside the confirmed answer bank.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .schemas import HiddenCommand, HiddenQuestion


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


@dataclass(frozen=True)
class KnownQuestionMatch:
    """A conservative, deterministic classification of a hidden question."""

    category: str
    verify: Callable[[str], bool]
    sentence_de: str
    sentence_en: str

    def sentence(self, language: str) -> str:
        return self.sentence_en if language == "en" else self.sentence_de


def match_known_question(question_plain: str) -> KnownQuestionMatch | None:
    """Classify an already-casefolded question against the confirmed answer bank.

    Conservative by design: returns None (unresolved) unless the question clearly
    maps to one known, confirmed category. Never guesses.
    """
    q = question_plain
    if re.search(
        r"(?:erzähl|schreib)\w*.{0,30}(?:etwas|ein bisschen|was)\s+(?:über|ueber)\s+dich"
        r"|tell (?:us|me) (?:a bit |a little )?about yourself"
        r"|wer\s+(?:du bist|ihr seid)"
        r"|sag(?:e|t)?\s+(?:uns|mir)\s+(?:etwas|ein bisschen)\s+(?:über|ueber)\s+dich",
        q,
    ):
        # A generic "tell us about yourself" request is not a probing personal
        # question needing a bespoke fact: every deterministic draft already opens
        # with name/degree/university/employer, so it is structurally always
        # answered already. This avoids blocking the whole fallback over an
        # invitation to introduce yourself that is satisfied by construction.
        return KnownQuestionMatch(
            category="self_intro",
            verify=lambda body: "information systems" in body and "werkstudent" in body,
            sentence_de=(
                "Mehr über mich habe ich oben ja schon geschrieben, aber ich erzähle gerne noch "
                "mehr, wenn ihr Fragen habt."
            ),
            sentence_en=(
                "I've already introduced myself above, but I'm happy to share more if you have "
                "questions."
            ),
        )
    if re.search(r"kartenspiel|card game|doppelkopf", q):
        if "doppelkopf" in q:
            return KnownQuestionMatch(
                category="card_game_doppelkopf",
                verify=lambda body: (
                    "doppelkopf" in body and bool(re.search(r"(?:kein|nicht|don.t|do not)", body))
                ),
                sentence_de=(
                    "Doppelkopf spiele ich ehrlich gesagt eher nicht, dafür aber richtig gerne "
                    "Skyjo, Flip 7 und ab und zu Uno."
                ),
                sentence_en=(
                    "I don't really play Doppelkopf, but I'm always up for Skyjo, Flip 7, and "
                    "sometimes Uno."
                ),
            )
        return KnownQuestionMatch(
            category="favorite_card_game",
            verify=lambda body: "skyjo" in body or "flip 7" in body,
            sentence_de=(
                "Bei Kartenspielen bin ich übrigens ganz klar bei Skyjo oder Flip 7, manchmal "
                "auch Uno."
            ),
            sentence_en=(
                "When it comes to card games, I'm clearly Team Skyjo or Flip 7, and sometimes Uno."
            ),
        )
    if re.search(r"haustier|\bpets?\b|\bhund(?:e|en)?\b|\bdogs?\b", q):
        return KnownQuestionMatch(
            category="pets",
            verify=lambda body: bool(
                re.search(r"keine\s+haustiere|no\s+pets", body)
                and re.search(r"hund|dog|haustier|pet", body)
            ),
            sentence_de="Ich habe keine Haustiere, komme mit ihnen aber gut klar und mag Hunde besonders gern.",
            sentence_en="I don't have pets, but I'm comfortable living with them and especially like dogs.",
        )
    if re.search(r"haftpflicht|liability insurance", q):
        return KnownQuestionMatch(
            category="private_liability_insurance",
            verify=lambda body: bool(
                re.search(r"haftpflichtversicherung|liability insurance", body)
            ),
            sentence_de="Eine private Haftpflichtversicherung habe ich.",
            sentence_en="I have private liability insurance.",
        )
    if re.search(
        r"(?:wie viele|wieviele|how many).{0,30}semester|(?:wie lange|how long).{0,30}(?:master|studier)",
        q,
    ):
        return KnownQuestionMatch(
            category="msc_duration",
            verify=lambda body: bool(
                re.search(r"vier\s+semester|four\s+semesters|4\s+semester", body)
            ),
            sentence_de="Mein Master dauert vier Semester, und für diese Zeit möchte ich in Münster wohnen.",
            sentence_en="My master's lasts four semesters, and I plan to live in Münster for that time.",
        )
    if re.search(r"lieblingsessen|favorite food|was isst|gericht", q):
        return KnownQuestionMatch(
            category="favorite_food",
            verify=lambda body: any(
                word in body for word in ("hähnchenpasta", "italien", "korean", "indisch")
            ),
            sentence_de="Mein aktuelles Lieblingsessen ist übrigens Hähnchenpasta mit Sahnesoße :)",
            sentence_en="My current favorite dish is chicken pasta with cream sauce, by the way :)",
        )
    if re.search(r"lieblings\w{0,10}getränk|favorite drink", q):
        return KnownQuestionMatch(
            category="favorite_drink",
            verify=lambda body: "kokoswasser" in body or "coconut water" in body,
            sentence_de="Mein Lieblingsgetränk ist ganz klar Kokoswasser.",
            sentence_en="My favorite drink is definitely coconut water.",
        )
    if re.search(r"song|musik|music", q):
        return KnownQuestionMatch(
            category="music",
            verify=lambda body: any(word in body for word in ("bollywood", "kanye west", "eminem")),
            sentence_de=(
                "Ein einzelnes Lieblingslied habe ich nicht wirklich, aber musikalisch bin ich "
                "viel bei 90er-Bollywood, Kanye West und Eminem unterwegs."
            ),
            sentence_en=(
                "I don't have one fixed favorite song, but musically I'm big into 90s Bollywood, "
                "Kanye West, and Eminem."
            ),
        )
    if re.search(r"fiktive|fictional character|filmfigur", q):
        return KnownQuestionMatch(
            category="fictional_character",
            verify=lambda body: bool(
                re.search(r"keine.{0,25}(figur|film)|ich selbst|being myself", body)
            ),
            sentence_de=(
                "Ich schaue nicht besonders viele Filme oder Serien und identifiziere mich "
                "eigentlich mit keiner bestimmten Figur - ich bin einfach gerne ich selbst."
            ),
            sentence_en=(
                "I don't watch that many films or shows and don't really identify with a "
                "fictional character - I'm happy just being myself."
            ),
        )
    if re.search(
        r"fun\s*fact|funfact|lustige\s+tatsache|interessante\s+tatsache|"
        r"etwas\s+besonderes.{0,20}(?:über|ueber)\s+dich",
        q,
    ):
        return KnownQuestionMatch(
            category="fun_fact",
            verify=lambda body: bool(
                re.search(
                    r"berlin.{0,20}ring|ringrunde|50\s*km|2[,.]5\s*stunden|"
                    r"keine\s+höhenangst|not afraid of heights",
                    body,
                )
            ),
            sentence_de=(
                "Ein Fun Fact über mich: Ich bin einmal die komplette Berliner Ringrunde mit "
                "dem Fahrrad gefahren - ungefähr 50 km in 2,5 Stunden 😄"
            ),
            sentence_en=(
                "A fun fact about me: I once cycled a complete loop around Berlin's Ring - "
                "roughly 50 km in about 2.5 hours 😄"
            ),
        )
    if re.search(r"sport|bouldern|cycling|fahrrad", q):
        return KnownQuestionMatch(
            category="sports",
            verify=lambda body: any(
                word in body
                for word in ("bould", "fahrrad", "radfahren", "cycling", "fitness", "gym")
            ),
            sentence_de=(
                "Sportlich bin ich viel unterwegs - ich fahre gerne Fahrrad, gehe bouldern und "
                "etwa viermal die Woche ins Gym, probiere aber auch gerne mal was Neues aus."
            ),
            sentence_en=(
                "I'm quite active - I enjoy cycling, bouldering, and going to the gym about four "
                "times a week, and I'm always open to trying new sports."
            ),
        )
    if re.search(r"wochenende|weekend", q):
        return KnownQuestionMatch(
            category="weekend",
            verify=lambda body: any(
                word in body for word in ("entspann", "hobb", "projekt", "relax")
            ),
            sentence_de=(
                "Am Wochenende entspanne ich meistens, kümmere mich um meine Hobbys und arbeite "
                "an eigenen kleinen Projekten."
            ),
            sentence_en=(
                "On weekends I usually relax, work on my hobbies, and spend time on personal "
                "projects."
            ),
        )
    if re.search(r"wg.aktivität|zusammen machen|together in the flat", q):
        return KnownQuestionMatch(
            category="ideal_wg_activity",
            verify=lambda body: any(
                word in body for word in ("karten", "essen", "cards", "eating")
            ),
            sentence_de=(
                "Am liebsten verbringe ich Zeit in der WG bei einem Kartenabend oder beim "
                "gemeinsamen Essen."
            ),
            sentence_en=(
                "I'd love spending time together over card games or cooking and eating together."
            ),
        )
    if re.search(r"lern|studier|study routine", q):
        return KnownQuestionMatch(
            category="study_routine",
            verify=lambda body: "bibliothek" in body or "library" in body,
            sentence_de="Zum Lernen bin ich meistens in der Bibliothek.",
            sentence_en="I mostly study in the library.",
        )
    if re.search(r"homeoffice|home office", q):
        return KnownQuestionMatch(
            category="home_office",
            verify=lambda body: bool(re.search(r"7.{0,3}8\s*stunden|7.{0,3}8\s*hours", body)),
            sentence_de=(
                "Im Homeoffice arbeite ich in der Regel 7-8 Stunden mit einer kurzen "
                "Essenspause von etwa 30-40 Minuten."
            ),
            sentence_en=(
                "In home office I usually work 7-8 hours with a short 30-40 minute food break."
            ),
        )
    if re.search(r"putz|clean", q):
        return KnownQuestionMatch(
            category="cleaning",
            verify=lambda body: "putzplan" in body or "cleaning plan" in body,
            sentence_de=(
                "Bei einem Putzplan halte ich mich ganz normal daran, sonst putze ich etwa "
                "wöchentlich."
            ),
            sentence_en=(
                "I stick to a cleaning plan if there is one, otherwise I clean roughly weekly."
            ),
        )
    if re.search(r"freund.{0,12}besuch|friends visit", q):
        return KnownQuestionMatch(
            category="friends_visiting",
            verify=lambda body: bool(re.search(r"1.{0,3}2.{0,12}(?:woche|week)", body)),
            sentence_de="Freunde sind bei mir etwa 1-2 Mal die Woche zu Besuch.",
            sentence_en="Friends visit me around 1-2 times a week.",
        )
    if re.search(r"\bfeiern\b|\bfeierst\b|\bparty\b|\bpartys\b|\bpartying\b", q) and not re.search(
        r"alkohol|alcohol", q
    ):
        return KnownQuestionMatch(
            category="party_frequency",
            verify=lambda body: any(
                word in body for word in ("monat", "month", "2-3", "gelegenheiten", "occasions")
            ),
            sentence_de=(
                "Gefeiert wird bei mir eher gelegentlich, ungefähr einmal im Monat oder alle "
                "2-3 Monate zu bestimmten Anlässen."
            ),
            sentence_en=(
                "I party occasionally, roughly once a month or every 2-3 months for specific "
                "occasions."
            ),
        )
    if re.search(r"alkohol|alcohol", q):
        return KnownQuestionMatch(
            category="alcohol",
            verify=lambda body: any(
                word in body for word in ("gelegentlich", "occasionally", "nicht regelmäßig")
            ),
            sentence_de=(
                "Alkohol trinke ich gelegentlich bei Treffen oder Feiern, aber nicht regelmäßig."
            ),
            sentence_en="I drink alcohol occasionally at gatherings or parties, but not regularly.",
        )
    if re.search(r"morgenmensch|morning or night|nachtmensch", q):
        return KnownQuestionMatch(
            category="morning_or_night",
            verify=lambda body: "morgenmensch" in body or "morning person" in body,
            sentence_de="Ich bin ganz klar Morgenmensch und mag den ersten Sonnenschein am Morgen.",
            sentence_en="I'm definitely a morning person and love the first morning light.",
        )
    if re.search(r"partner|overnight|übernacht", q):
        return KnownQuestionMatch(
            category="partner_overnight",
            verify=lambda body: any(
                word in body for word in ("berlin", "selten", "rarely", "keine übernacht")
            ),
            sentence_de="Mein Partner lebt aktuell in Berlin und würde nur selten zu Besuch kommen.",
            sentence_en="My partner currently lives in Berlin and would only visit rarely.",
        )
    if re.search(r"nachhalt|sustainab", q):
        return KnownQuestionMatch(
            category="sustainability",
            verify=lambda body: any(word in body for word in ("müll", "wasser", "waste", "water")),
            sentence_de=(
                "Nachhaltigkeit ist mir wichtig - ich versuche, Müll zu vermeiden und sparsam "
                "mit Wasser umzugehen."
            ),
            sentence_en="Sustainability matters to me - I try to reduce waste and use water carefully.",
        )
    if re.search(r"jahreszeit|season", q):
        return KnownQuestionMatch(
            category="favorite_season",
            verify=lambda body: "sommer" in body or "summer" in body,
            sentence_de="Meine liebste Jahreszeit ist ganz klar der Sommer.",
            sentence_en="My favorite season is definitely summer.",
        )
    if re.search(r"koch\w*", q):
        return KnownQuestionMatch(
            category="cooking",
            verify=lambda body: any(
                word in body for word in ("meal prep", "kocht", "koche", "schnelle", "quick meal")
            ),
            sentence_de=(
                "Manchmal koche ich richtig, ansonsten geht es bei mir eher in Richtung Meal "
                "Prep oder schnelle Gerichte."
            ),
            sentence_en="Sometimes I cook properly, otherwise I lean toward meal prep or quick meals.",
        )
    if re.search(r"politik|politic|identität|identity", q):
        return KnownQuestionMatch(
            category="sensitive_topic",
            verify=lambda body: any(
                word in body for word in ("lieber nicht", "prefer not", "möchte ich nicht")
            ),
            sentence_de="Dazu sage ich lieber nicht so viel, das behalte ich für mich.",
            sentence_en="I'd prefer not to go into that, if that's okay.",
        )
    return None


def _configured_sentence(
    match: KnownQuestionMatch,
    language: str,
    answer_bank: dict[str, Any] | None,
    applicant: dict[str, Any] | None,
) -> str | None:
    """Render known answers from configuration where possible.

    The hardcoded sentences on ``KnownQuestionMatch`` remain safe defaults for older
    callers and deterministic verification. Production fallback generation passes the
    current applicant/answer-bank configuration through this function.
    """
    bank = (answer_bank or {}).get("confirmed_answer_bank", answer_bank or {})
    if match.category == "self_intro":
        return match.sentence(language)
    if match.category == "pets":
        pets = (applicant or {}).get("pets", {})
        if pets.get("owns_pets") is False and pets.get("comfortable_living_with_pets") is True:
            likes_dogs = "dogs" in pets.get("particularly_likes", [])
            if language == "en":
                return "I don't have pets, but I'm comfortable living with them" + (
                    " and especially like dogs." if likes_dogs else "."
                )
            return "Ich habe keine Haustiere, komme mit ihnen aber gut klar" + (
                " und mag Hunde besonders gern." if likes_dogs else "."
            )
        return None
    if match.category == "private_liability_insurance":
        return (
            match.sentence(language)
            if (applicant or {}).get("private_liability_insurance") is True
            else None
        )
    if match.category == "msc_duration":
        profile = applicant or {}
        if (
            profile.get("degree_duration_semesters") == 4
            and profile.get("expected_muenster_residence_semesters") == 4
        ):
            return match.sentence(language)
        return None
    if match.category == "fun_fact":
        personal_facts = (applicant or {}).get("personal_facts", {})
        fact = personal_facts.get("preferred_fun_fact", {})
        if fact.get("kind") == "berlin_ring_cycle":
            distance = fact.get("distance_km")
            duration = fact.get("duration_hours")
            if distance is not None and duration is not None:
                duration_de = str(duration).replace(".", ",")
                if language == "en":
                    return (
                        "A fun fact about me: I once cycled a complete loop around Berlin's Ring "
                        f"- roughly {distance:g} km in about {duration:g} hours 😄"
                    )
                return (
                    "Ein Fun Fact über mich: Ich bin einmal die komplette Berliner Ringrunde mit "
                    f"dem Fahrrad gefahren - ungefähr {distance:g} km in {duration_de} Stunden 😄"
                )
        alternative = personal_facts.get("alternative_fun_fact", {})
        if alternative.get("kind") == "not_afraid_of_heights":
            return (
                "A fun fact about me: I'm not afraid of heights."
                if language == "en"
                else "Ein Fun Fact über mich: Ich habe keine Höhenangst."
            )
        return None
    if match.category == "card_game_doppelkopf":
        card_games = bank.get("card_games", {})
        if not card_games.get("doppelkopf") or not card_games.get("favorites"):
            return None
        games = " oder ".join(str(item) for item in card_games["favorites"][:2])
        return (
            f"I don't really play Doppelkopf, but I'm always up for {games}."
            if language == "en"
            else f"Doppelkopf spiele ich eher nicht, dafür aber richtig gerne {games}."
        )
    if match.category == "favorite_card_game":
        games = bank.get("card_games", {}).get("favorites", [])
        if not games:
            return None
        joined = " oder ".join(str(item) for item in games[:2])
        return (
            f"Bei Kartenspielen bin ich übrigens ganz klar bei {joined}."
            if language != "en"
            else f"When it comes to card games, my favorites are {joined}."
        )
    if match.category == "favorite_food":
        dish = bank.get("food", {}).get("german_go_to_dish" if language != "en" else "go_to_dish")
        if not dish:
            return None
        return (
            f"Mein aktuelles Lieblingsessen ist übrigens {dish} :)"
            if language != "en"
            else f"My current favorite dish is {dish}, by the way :)"
        )
    if match.category == "favorite_drink":
        drink = bank.get("favorite_drink")
        if not drink:
            return None
        rendered = "Kokoswasser" if language != "en" and drink == "coconut water" else drink
        return (
            f"Mein Lieblingsgetränk ist ganz klar {rendered}."
            if language != "en"
            else f"My favorite drink is definitely {rendered}."
        )
    if match.category == "music":
        music = bank.get("music", [])
        if not music:
            return None
        joined = ", ".join(str(item) for item in music)
        return (
            "Ein einzelnes Lieblingslied habe ich nicht wirklich, aber musikalisch höre ich "
            f"gerne {joined}."
            if language != "en"
            else f"I don't have one fixed favorite song, but I listen to {joined}."
        )
    if match.category == "fictional_character" and bank.get("fictional_character"):
        return (
            "I don't watch many films or series and don't really identify with a fictional "
            "character - I'm happy just being myself."
            if language == "en"
            else "Ich schaue nicht viele Filme oder Serien und identifiziere mich mit keiner "
            "bestimmten Figur - ich bin einfach gerne ich selbst."
        )
    if match.category == "sports" and bank.get("sports"):
        return (
            "I enjoy cycling and bouldering and go to the gym about four times a week."
            if language == "en"
            else "Ich fahre gerne Fahrrad, gehe bouldern und etwa viermal pro Woche ins Gym."
        )
    if match.category == "weekend" and bank.get("weekend"):
        return (
            "On weekends I usually relax, spend time on my hobbies, and work on personal projects."
            if language == "en"
            else "Am Wochenende entspanne ich meistens, kümmere mich um meine Hobbys und arbeite "
            "an eigenen kleinen Projekten."
        )
    if match.category == "ideal_wg_activity" and bank.get("ideal_wg_activities"):
        return (
            "I especially enjoy card evenings or eating together in a flatshare."
            if language == "en"
            else "In einer WG mag ich besonders Kartenabende oder gemeinsames Essen."
        )
    if match.category == "study_routine" and bank.get("study_routine"):
        return (
            "I mostly study in the library."
            if language == "en"
            else "Zum Lernen bin ich meistens in der Bibliothek."
        )
    if match.category == "home_office" and bank.get("home_office"):
        return (
            "In home office I usually work 7-8 hours with a short 30-40 minute food break."
            if language == "en"
            else "Im Homeoffice arbeite ich meistens 7-8 Stunden mit einer kurzen Essenspause "
            "von etwa 30-40 Minuten."
        )
    if match.category == "cleaning" and bank.get("cleaning"):
        return (
            "I follow the cleaning plan; without one, I clean roughly weekly."
            if language == "en"
            else "An einen Putzplan halte ich mich; ohne Plan putze ich ungefähr wöchentlich."
        )
    if match.category == "friends_visiting" and bank.get("friends_visiting"):
        return (
            "Friends visit me around 1-2 times a week."
            if language == "en"
            else "Freunde sind bei mir ungefähr 1-2 Mal pro Woche zu Besuch."
        )
    if match.category == "party_frequency" and bank.get("partying"):
        return (
            "I party occasionally, roughly once a month or every 2-3 months for an occasion."
            if language == "en"
            else "Ich feiere eher gelegentlich, ungefähr einmal im Monat oder alle 2-3 Monate "
            "zu einem Anlass."
        )
    if match.category == "alcohol" and bank.get("alcohol"):
        return (
            "I drink alcohol occasionally at gatherings, but not regularly."
            if language == "en"
            else "Alkohol trinke ich gelegentlich bei Treffen, aber nicht regelmäßig."
        )
    if match.category == "morning_or_night" and bank.get("morning_or_night"):
        return (
            "I'm definitely a morning person and like the first morning light."
            if language == "en"
            else "Ich bin ganz klar Morgenmensch und mag den ersten Sonnenschein am Morgen."
        )
    if match.category == "partner_overnight" and (
        bank.get("partner") or bank.get("overnight_guests")
    ):
        return (
            "My partner lives in Berlin and would only visit rarely; I generally don't have "
            "overnight guests."
            if language == "en"
            else "Mein Partner lebt in Berlin und würde nur selten zu Besuch kommen; sonst habe "
            "ich grundsätzlich keine Übernachtungsgäste."
        )
    if match.category == "sustainability" and bank.get("sustainability"):
        return (
            "Sustainability matters to me, so I try to reduce waste and use water carefully."
            if language == "en"
            else "Nachhaltigkeit ist mir wichtig; ich versuche Müll zu vermeiden und sparsam "
            "mit Wasser umzugehen."
        )
    if match.category == "favorite_season" and bank.get("favorite_season"):
        season = str(bank["favorite_season"])
        rendered = "Sommer" if language != "en" and season.casefold() == "summer" else season
        return (
            f"My favorite season is {rendered}."
            if language == "en"
            else f"Meine liebste Jahreszeit ist der {rendered}."
        )
    if match.category == "cooking" and bank.get("food", {}).get("cooking_style"):
        return (
            "Sometimes I cook properly; otherwise I meal-prep or make something quick."
            if language == "en"
            else "Manchmal koche ich richtig, sonst mache ich Meal Prep oder schnelle Gerichte."
        )
    if match.category == "sensitive_topic" and (
        bank.get("politics") or bank.get("sensitive_identity_questions")
    ):
        return (
            "I'd prefer not to go into that, if that's okay."
            if language == "en"
            else "Dazu sage ich lieber nicht so viel und behalte das für mich."
        )
    return match.sentence(language) if answer_bank is None else None


@dataclass(frozen=True)
class ResolvedQuestion:
    question_id: str
    question_category: str
    answer_source: str
    resolved: bool
    resolved_answer: str


def resolve_hidden_questions(
    questions: list[HiddenQuestion],
    language: str,
    answer_bank: dict[str, Any] | None = None,
    applicant: dict[str, Any] | None = None,
) -> tuple[list[ResolvedQuestion], list[HiddenQuestion]]:
    """Try to resolve every required hidden question from the confirmed answer bank.

    Conservative: any question that cannot be confidently classified is returned
    unresolved rather than guessed at.
    """
    resolved: list[ResolvedQuestion] = []
    unresolved: list[HiddenQuestion] = []
    for question in questions:
        if not question.required:
            continue
        match = match_known_question(_plain(question.question))
        if match is None:
            unresolved.append(question)
            continue
        answer = _configured_sentence(match, language, answer_bank, applicant)
        if not answer:
            unresolved.append(question)
            continue
        resolved.append(
            ResolvedQuestion(
                question_id=question.id,
                question_category=match.category,
                answer_source=(
                    "applicant_profile"
                    if match.category
                    in {"fun_fact", "pets", "private_liability_insurance", "msc_duration"}
                    else "structural_self_intro"
                    if match.category == "self_intro"
                    else "confirmed_answer_bank"
                ),
                resolved=True,
                resolved_answer=answer,
            )
        )
    return resolved, unresolved


def resolve_hidden_commands(
    commands: list[HiddenCommand],
) -> tuple[list[HiddenCommand], list[HiddenCommand]]:
    """Split hidden commands into those that can be mechanically/safely applied and
    verified afterwards, versus those that cannot and must block the fallback."""
    resolvable: list[HiddenCommand] = []
    unresolved: list[HiddenCommand] = []
    for command in commands:
        if not command.required:
            continue
        exact = (command.exact_text or "").strip()
        has_exact_text = command.kind in {
            "required_first_word",
            "exact_subject",
            "required_keyword",
        } and bool(exact)
        has_verifiable_length = command.kind == "length" and bool(
            re.search(r"\d+", command.instruction)
        )
        if has_exact_text or has_verifiable_length:
            resolvable.append(command)
        else:
            unresolved.append(command)
    return resolvable, unresolved
