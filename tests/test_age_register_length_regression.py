# ruff: noqa: E501
from __future__ import annotations

from dataclasses import replace

import pytest
import yaml

from app.analyzer import process_listing
from app.config_loader import ROOT
from app.message_policy import (
    GERMAN_WG_VIEWING_CLOSING,
    count_age_mismatch_mentions,
    enforce_message_policy,
    is_formal_application_context,
)
from app.prefilter import deterministic_prefilter
from app.rules import apply_rules
from app.schemas import (
    AttachmentDecision,
    HiddenQuestion,
    ListingFacts,
    MessageDraft,
    RuleDecision,
    SourceListing,
)
from app.universal_fallback import build_deterministic_fallback
from app.validator import _words, validate_message
from tests.conftest import successful_call, viable_listing


@pytest.mark.parametrize(
    "wording",
    [
        "Gesucht wird: Geschlecht egal zwischen 25 und 35 Jahren.",
        "Wir suchen jemanden zwischen 25 und 35 Jahren.",
        "Am liebsten 25-35 gesucht.",
        "Am besten ungefähr in unserem Alter.",
    ],
)
def test_non_strict_age_wording_never_hard_skips(wording: str, config: dict) -> None:
    result = deterministic_prefilter(
        viable_listing(raw_text=f"WG-Zimmer 490 € warm, mindestens 12 Monate. {wording}"),
        config,
    )
    decision = apply_rules(result.facts, config, result)
    assert decision.decision == "APPLY"
    assert not decision.hard_skip_reasons


@pytest.mark.parametrize(
    "wording",
    [
        "Nur Bewerber ab 30, keine Ausnahmen.",
        "Mindestalter 30 zwingend.",
        "Bitte nur Personen ab 30 anschreiben.",
    ],
)
def test_even_strict_age_wording_never_hard_skips(wording: str, config: dict) -> None:
    """FINAL policy: age mismatch is never a hard filter, never review_required, and
    never stops autonomous sending -- regardless of how strictly the listing states its
    preferred range. `age_requirement_strength` remains a soft, informational fact."""
    result = deterministic_prefilter(
        viable_listing(raw_text=f"WG-Zimmer 490 € warm, mindestens 12 Monate. {wording}"),
        config,
    )
    assert result.facts.age_requirement_strength == "strict"
    assert result.facts.age_mismatch
    decision = apply_rules(result.facts, config, result)
    assert decision.decision == "APPLY"
    assert not decision.hard_skip_reasons
    assert any("soft age mismatch" in warning for warning in decision.warnings)


@pytest.mark.parametrize(
    ("raw_text", "facts", "formal"),
    [
        (
            "Aktuelle Mitbewohner suchen dich für unsere 3er WG. Privater Nutzer.",
            ListingFacts(housing_type="wg_room", advertiser_type="private_landlord"),
            False,
        ),
        (
            "Studenten-WG sucht neue Mitbewohnerin oder neuen Mitbewohner.",
            ListingFacts(housing_type="wg_room", advertiser_type="unknown"),
            False,
        ),
        (
            "Studio von einem privaten Vermieter. Bitte stellen Sie sich vor.",
            ListingFacts(housing_type="studio", advertiser_type="private_landlord"),
            True,
        ),
        (
            "Hausverwaltung West vermietet ein WG-Zimmer.",
            ListingFacts(housing_type="wg_room", advertiser_type="unknown"),
            True,
        ),
        (
            "Münster Wohnen GmbH vermietet ein Apartment.",
            ListingFacts(housing_type="studio", advertiser_type="company"),
            True,
        ),
    ],
)
def test_register_uses_application_context(
    raw_text: str, facts: ListingFacts, formal: bool
) -> None:
    listing = SourceListing(platform="wg_gesucht", raw_text=raw_text)
    assert is_formal_application_context(listing, facts) is formal


def _wg_draft_with_words(target: int) -> MessageDraft:
    prefix = (
        "Hallo zusammen, eure gemeinsamen Kochabende und das Radfahren passen sehr gut zu mir. "
        "Ich bin Rakshit und suche ein ruhiges langfristiges Zuhause in Münster. "
    )
    missing = target - len(_words(prefix)) - len(_words(GERMAN_WG_VIEWING_CLOSING))
    assert missing >= 0
    body = f"{prefix}{'wirklich ' * missing}\n\n{GERMAN_WG_VIEWING_CLOSING}"
    assert len(_words(body)) == target
    return MessageDraft(
        body=body,
        hooks_used=["gemeinsamen Kochabende und Radfahren"],
        language="de",
        address_register="du",
    )


@pytest.mark.parametrize("words", [130, 165])
def test_normal_wg_lengths_pass(words: int, config: dict) -> None:
    result = validate_message(
        viable_listing(),
        ListingFacts(listing_language="de", housing_type="wg_room", advertiser_type="wg"),
        RuleDecision(decision="APPLY"),
        _wg_draft_with_words(words),
        AttachmentDecision(),
        config,
    )
    assert result.auto_send_allowed, result.errors


def test_185_word_wg_is_warning_not_block(config: dict) -> None:
    result = validate_message(
        viable_listing(),
        ListingFacts(listing_language="de", housing_type="wg_room", advertiser_type="wg"),
        RuleDecision(decision="APPLY"),
        _wg_draft_with_words(185),
        AttachmentDecision(),
        config,
    )
    assert result.auto_send_allowed, result.errors
    assert any("preferred length" in warning for warning in result.warnings)


def test_wg_over_220_words_is_blocked(config: dict) -> None:
    result = validate_message(
        viable_listing(),
        ListingFacts(listing_language="de", housing_type="wg_room", advertiser_type="wg"),
        RuleDecision(decision="APPLY"),
        _wg_draft_with_words(221),
        AttachmentDecision(),
        config,
    )
    assert not result.auto_send_allowed
    assert any("excessively long" in error for error in result.errors)


def test_207_word_wg_message_passes_with_only_a_mild_warning(config: dict) -> None:
    """Naturalness matters more than a rigid word count: 207 words is not blocked
    just for being long when nothing else is wrong with the draft."""
    result = validate_message(
        viable_listing(),
        ListingFacts(listing_language="de", housing_type="wg_room", advertiser_type="wg"),
        RuleDecision(decision="APPLY"),
        _wg_draft_with_words(207),
        AttachmentDecision(),
        config,
    )
    assert result.auto_send_allowed, result.errors
    assert any("above preferred length" in warning for warning in result.warnings)


def test_195_word_message_with_quality_content_gets_no_length_warning(config: dict) -> None:
    """A message up to roughly 195 words is fully acceptable, with no warning at all,
    when it is earning its length: a real hook, a hidden-question answer, and an
    age/fit clarification (plus the mandatory viewing block, always present)."""
    facts = ListingFacts(
        listing_language="de",
        housing_type="wg_room",
        advertiser_type="wg",
        age_min=25,
        age_max=35,
        age_requirement_strength="soft",
        age_mismatch=True,
        hidden_questions=[HiddenQuestion(id="q1", question="Was ist dein Lieblingsessen?")],
    )
    extra = (
        "Hähnchenpasta mit Sahnesoße. Ich weiß, dass ihr eigentlich jemanden zwischen 25 und 35 "
        "sucht, bin da als 21-Jähriger etwas jünger, aber ziemlich unkompliziert."
    )
    draft = _wg_draft_with_words(195 - len(_words(extra)))
    draft.body = draft.body.replace(
        GERMAN_WG_VIEWING_CLOSING, f"{extra}\n\n{GERMAN_WG_VIEWING_CLOSING}"
    )
    draft.answered_question_ids = ["q1"]
    assert len(_words(draft.body)) <= 195
    result = validate_message(
        viable_listing(), facts, RuleDecision(decision="APPLY"), draft, AttachmentDecision(), config
    )
    assert result.auto_send_allowed, result.errors
    assert not any(
        "preferred length" in warning or "long" in warning for warning in result.warnings
    )


def test_212_word_message_with_hidden_questions_gets_a_long_warning(config: dict) -> None:
    facts = ListingFacts(
        listing_language="de",
        housing_type="wg_room",
        advertiser_type="wg",
        hidden_questions=[
            HiddenQuestion(id="q1", question="Was ist dein Lieblingsessen?"),
            HiddenQuestion(id="q2", question="Was ist dein Lieblingsgetränk?"),
        ],
    )
    draft = _wg_draft_with_words(207)
    draft.body = draft.body.replace(
        GERMAN_WG_VIEWING_CLOSING,
        f"Hähnchenpasta mit Sahnesoße und Kokoswasser.\n\n{GERMAN_WG_VIEWING_CLOSING}",
    )
    draft.answered_question_ids = ["q1", "q2"]
    result = validate_message(
        viable_listing(), facts, RuleDecision(decision="APPLY"), draft, AttachmentDecision(), config
    )
    assert result.auto_send_allowed, result.errors
    assert any("WG message long" in warning for warning in result.warnings)


def test_wg_message_above_240_blocked_even_with_detailed_answers_exception(config: dict) -> None:
    facts = ListingFacts(
        listing_language="de",
        housing_type="wg_room",
        advertiser_type="wg",
        hidden_questions=[
            HiddenQuestion(id="q1", question="Was ist dein Lieblingsessen?"),
            HiddenQuestion(id="q2", question="Was ist dein Lieblingsgetränk?"),
        ],
    )
    result = validate_message(
        viable_listing(),
        facts,
        RuleDecision(decision="APPLY"),
        _wg_draft_with_words(245),
        AttachmentDecision(),
        config,
    )
    assert not result.auto_send_allowed
    assert any("excessively long" in error for error in result.errors)


def test_age_mismatch_single_mention_passes(config: dict) -> None:
    facts = ListingFacts(
        listing_language="de",
        housing_type="wg_room",
        advertiser_type="wg",
        age_min=25,
        age_max=35,
        age_requirement_strength="soft",
        age_mismatch=True,
    )
    draft = _wg_draft_with_words(130)
    draft.body = draft.body.replace(
        GERMAN_WG_VIEWING_CLOSING,
        "Ich weiß, dass ihr eigentlich jemanden zwischen 25 und 35 sucht. Ich werde im Oktober "
        "22 und bin damit etwas jünger, glaube aber, dass ich trotzdem gut zu euch passen "
        "könnte. Ich bin eher ruhig, verlässlich und ordentlich und bringe die nötige Reife "
        f"für ein entspanntes Zusammenleben mit.\n\n{GERMAN_WG_VIEWING_CLOSING}",
    )
    result = validate_message(
        viable_listing(), facts, RuleDecision(decision="APPLY"), draft, AttachmentDecision(), config
    )
    assert result.auto_send_allowed, result.errors
    assert not any("repeated" in warning for warning in result.warnings)


def test_repeated_age_mismatch_mention_is_blocked(config: dict) -> None:
    facts = ListingFacts(
        listing_language="de",
        housing_type="wg_room",
        advertiser_type="wg",
        age_min=25,
        age_max=35,
        age_requirement_strength="soft",
        age_mismatch=True,
    )
    draft = _wg_draft_with_words(130)
    draft.body = draft.body.replace(
        GERMAN_WG_VIEWING_CLOSING,
        "Mit 21 bin ich zwar etwas jünger als eure gesuchte Altersspanne, aber das dürfte kein "
        "Problem sein. Ich weiß, dass ihr eigentlich jemanden zwischen 25 und 35 sucht. Ich "
        "werde im Oktober 22 und bin damit etwas jünger, glaube aber, dass ich trotzdem gut zu "
        "euch passen könnte. Ich bin eher ruhig, verlässlich und ordentlich und bringe die "
        f"nötige Reife für ein entspanntes Zusammenleben mit.\n\n{GERMAN_WG_VIEWING_CLOSING}",
    )
    result = validate_message(
        viable_listing(), facts, RuleDecision(decision="APPLY"), draft, AttachmentDecision(), config
    )
    assert not result.auto_send_allowed
    assert any("repeated" in error and "age mismatch" in error for error in result.errors)


def test_formal_landlord_180_words_is_a_soft_warning_not_a_block(config: dict) -> None:
    """A reasonable word-count target is a soft style guideline: an otherwise-normal
    180-word formal message must not be hard-blocked just for landing outside the
    90-140 preferred range (only genuinely absurd/broken lengths should)."""
    listing = SourceListing(
        platform="unknown", raw_text="Studio von privatem Vermieter in Münster."
    )
    body = "Guten Tag, ich interessiere mich sehr für Ihr Studio in Münster. "
    body += "Die Lage und die Ausstattung gefallen mir außerordentlich gut. " * (
        (180 - len(_words(body))) // 10 + 1
    )
    draft = MessageDraft(
        body=body,
        hooks_used=["Studio in Münster"],
        language="de",
        address_register="sie",
    )
    result = validate_message(
        listing,
        ListingFacts(
            listing_language="de", housing_type="studio", advertiser_type="private_landlord"
        ),
        RuleDecision(decision="APPLY"),
        draft,
        AttachmentDecision(),
        config,
    )
    assert not any("formal message length" in error for error in result.errors)
    assert any("formal message length" in warning for warning in result.warnings)


def test_real_wg_14096490_is_apply_with_soft_age_warning(settings, config, answers) -> None:
    payload = yaml.safe_load((ROOT / "tests/fixtures/wg_14096490.yaml").read_text(encoding="utf-8"))
    listing = SourceListing.model_validate(payload)
    prefilter = deterministic_prefilter(listing, config)
    assert prefilter.facts.age_min == 25
    assert prefilter.facts.age_max == 35
    assert prefilter.facts.age_requirement_strength == "soft"
    assert prefilter.facts.age_mismatch
    assert prefilter.facts.advertiser_type == "wg"
    assert len(prefilter.facts.hidden_questions) == 1
    assert apply_rules(prefilter.facts, config, prefilter).decision == "APPLY"

    body = f"""Hallo Jana und Laurenz,

euer gemütliches Wohnzimmer gefällt mir sehr. Ich starte im Oktober meinen MSc Information Systems an der Uni Münster und arbeite als Werkstudent bei der Landesbausparkasse vollständig remote. Ich fahre gerne Fahrrad und gehe bouldern. Beim Lieblingsessen bin ich aktuell ganz klar bei Hähnchenpasta mit Sahnesoße :)

Ich weiß, dass ihr eigentlich jemanden zwischen 25 und 35 sucht. Ich werde im Oktober 22 und bin damit etwas jünger, aber ich glaube, dass ich trotzdem gut zu euch passen könnte. Ich bin eher ruhig, verlässlich und ordentlich und bringe die nötige Reife für ein entspanntes Zusammenleben mit.

{GERMAN_WG_VIEWING_CLOSING}"""
    expected = successful_call(listing, config, body=body)
    expected.data.facts.advertiser_type = "private_landlord"
    expected.data.facts.mandatory_incompatibilities = [
        "applicant age 21 is outside the requested age range 25-35"
    ]
    expected.data.facts.critical_ambiguities = [
        "age 21 does not match the stated 25-35 search range"
    ]
    expected.data.message.hooks_used = ["gemütliches Wohnzimmer"]
    expected.data.facts.personalization_hooks = ["gemütliches Wohnzimmer"]

    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": lambda *_: expected},
    )

    assert outcome.rule_decision.decision == "APPLY"
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message and outcome.message.address_register == "du"
    assert not any("register" in error for error in outcome.validation.errors)
    assert not any("length" in error for error in outcome.validation.errors)
    # A real hook, an answered hidden question, and an age/fit clarification together
    # justify a message in this range: no length warning is expected at all.
    assert not any(
        "preferred length" in warning or "long" in warning
        for warning in outcome.validation.warnings
    )
    assert 150 < len(_words(outcome.message.body)) <= 210
    assert count_age_mismatch_mentions(outcome.message.body) == 1
    assert outcome.attachment.should_attach
    assert outcome.attachment.source == "wg_account"


# ---- FINAL age policy: never a hard filter, never review_required, never blocks send ---


def test_applicant_below_age_range_is_not_hard_skip(config: dict) -> None:
    facts = ListingFacts(
        age_min=25, age_max=35, age_requirement_strength="strict", age_mismatch=True
    )
    decision = apply_rules(facts, config)
    assert decision.decision != "SKIP"
    assert not decision.hard_skip_reasons


def test_applicant_above_age_range_is_not_hard_skip(config: dict) -> None:
    """Constructed directly (config always has a fixed applicant age): confirms the
    rule engine never hard-skips on age_mismatch regardless of which side of the
    range the applicant falls on."""
    facts = ListingFacts(age_max=18, age_requirement_strength="strict", age_mismatch=True)
    decision = apply_rules(facts, config)
    assert decision.decision != "SKIP"
    assert not decision.hard_skip_reasons


def test_age_mismatch_alone_results_in_apply(config: dict) -> None:
    facts = ListingFacts(
        warm_rent_eur=490,
        cold_rent_eur=400,
        minimum_duration_months=12,
        advertiser_type="wg",
        housing_type="wg_room",
        confidence=0.9,
        age_min=30,
        age_max=40,
        age_requirement_strength="strict",
        age_mismatch=True,
    )
    decision = apply_rules(facts, config)
    assert decision.decision == "APPLY"


def test_age_mismatch_never_creates_review_required(config: dict) -> None:
    facts = ListingFacts(
        warm_rent_eur=490,
        cold_rent_eur=400,
        minimum_duration_months=12,
        advertiser_type="wg",
        housing_type="wg_room",
        confidence=0.9,
        age_min=30,
        age_max=40,
        age_requirement_strength="strict",
        age_mismatch=True,
    )
    decision = apply_rules(facts, config)
    assert decision.decision != "REVIEW"


def test_deterministic_fallback_with_age_mismatch_produces_valid_draft(
    config: dict, answers: dict
) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer 490 € warm, mindestens 12 Monate. Nur Bewerber ab 30, keine Ausnahmen."
        )
    )
    prefilter = deterministic_prefilter(listing, config)
    assert prefilter.facts.age_requirement_strength == "strict"
    assert prefilter.facts.age_mismatch
    fallback = build_deterministic_fallback(listing, prefilter, config, answers)
    assert fallback.draft is not None, fallback.blockers
    assert count_age_mismatch_mentions(fallback.draft.body) == 1


def test_message_mentions_age_mismatch_exactly_once_even_when_strict(config: dict) -> None:
    listing = SourceListing(platform="wg_gesucht", raw_text="WG-Zimmer 490 € warm.")
    facts = ListingFacts(
        listing_language="de",
        housing_type="wg_room",
        advertiser_type="wg",
        age_min=30,
        age_max=40,
        age_requirement_strength="strict",
        age_mismatch=True,
    )
    draft = MessageDraft(
        body="Ich mag Fahrradfahren und Kochen sehr.",
        language="de",
        address_register="du",
    )
    result = enforce_message_policy(listing, facts, draft)
    assert count_age_mismatch_mentions(result.body) == 1


def test_validator_allows_strict_age_mismatch_when_acknowledged(config: dict) -> None:
    """The validator must not reintroduce age as a blocking error for any mismatch
    strength once it is acknowledged with a single natural mention."""
    facts = ListingFacts(
        listing_language="de",
        housing_type="wg_room",
        advertiser_type="wg",
        age_min=30,
        age_max=40,
        age_requirement_strength="strict",
        age_mismatch=True,
    )
    draft = _wg_draft_with_words(130)
    draft.body = draft.body.replace(
        GERMAN_WG_VIEWING_CLOSING,
        "Mit 21 bin ich etwas jünger als eure Wunschspanne zwischen 30 und 40, aber ich glaube, "
        f"dass es menschlich trotzdem gut passen könnte.\n\n{GERMAN_WG_VIEWING_CLOSING}",
    )
    result = validate_message(
        viable_listing(), facts, RuleDecision(decision="APPLY"), draft, AttachmentDecision(), config
    )
    assert result.auto_send_allowed, result.errors
