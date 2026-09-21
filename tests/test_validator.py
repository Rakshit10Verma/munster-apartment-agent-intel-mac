from __future__ import annotations

import re

import pytest

from app.schemas import (
    AttachmentDecision,
    HiddenCommand,
    HiddenQuestion,
    ListingFacts,
    MessageDraft,
    RuleDecision,
)
from app.validator import validate_message
from tests.conftest import NATURAL_WG_BODY, viable_listing


def valid_facts() -> ListingFacts:
    return ListingFacts(
        listing_language="de",
        advertiser_type="wg",
        housing_type="wg_room",
        warm_rent_eur=490,
        minimum_duration_months=12,
        confidence=0.95,
    )


def valid_draft() -> MessageDraft:
    return MessageDraft(
        subject="WG-Zimmer",
        body=NATURAL_WG_BODY,
        hooks_used=["gemeinsamen Kochabenden und Radfahren"],
        language="de",
        address_register="du",
    )


def errors_for(config: dict, facts: ListingFacts, draft: MessageDraft) -> list[str]:
    return validate_message(
        viable_listing(),
        facts,
        RuleDecision(decision="APPLY"),
        draft,
        AttachmentDecision(),
        config,
    ).errors


def test_valid_natural_message_passes(config: dict) -> None:
    result = validate_message(
        viable_listing(),
        valid_facts(),
        RuleDecision(decision="APPLY"),
        valid_draft(),
        AttachmentDecision(),
        config,
    )
    assert result.auto_send_allowed, result.errors


def test_changed_required_german_wg_closing_is_blocked(config: dict) -> None:
    draft = valid_draft()
    draft.body = draft.body.replace("natürlich am einfachsten", "am besten")
    assert any("viewing/closing" in item for item in errors_for(config, valid_facts(), draft))


def test_wg_message_must_not_mention_bewerbermappe(config: dict) -> None:
    draft = valid_draft()
    draft.body = "Meine Bewerbermappe ist angehängt.\n\n" + draft.body
    assert any("Bewerbermappe" in item for item in errors_for(config, valid_facts(), draft))


@pytest.mark.parametrize(
    ("bad_text", "expected"),
    [
        ("LBS", "LBS"),
        ("Remote-Mitarbeiter", "Remote-Mitarbeiter"),
        ("Ich komme aus Indien", "nationality"),
    ],
)
def test_forbidden_personal_wording(bad_text: str, expected: str, config: dict) -> None:
    draft = valid_draft()
    draft.body += f" {bad_text}."
    assert any(
        expected.casefold() in item.casefold() for item in errors_for(config, valid_facts(), draft)
    )


def test_indian_food_is_not_misclassified_as_national_origin(config: dict) -> None:
    draft = valid_draft()
    draft.body += " Ich koche außerdem gern indisch."
    assert not any("nationality" in item for item in errors_for(config, valid_facts(), draft))


@pytest.mark.parametrize(
    ("replacement", "expected"),
    [
        ("studiere im Master Information Systems", "timing"),
        ("starte im Oktober meinen Master in Information Systems", "none"),
    ],
)
def test_masters_start_timing(replacement: str, expected: str, config: dict) -> None:
    draft = valid_draft()
    draft.body = re.sub(
        r"starte im Oktober meinen\s+Master in Information Systems", replacement, draft.body
    )
    errors = errors_for(config, valid_facts(), draft)
    assert any("timing" in item for item in errors) is (expected == "timing")


def test_mostly_remote_contradiction_is_blocked(config: dict) -> None:
    draft = valid_draft()
    draft.body = draft.body.replace("vollständig remote", "überwiegend remote")
    assert any("fully remote" in item for item in errors_for(config, valid_facts(), draft))


def test_known_bad_card_sentence_is_blocked(config: dict) -> None:
    draft = valid_draft()
    draft.body = re.sub(
        r"Bei Kartenspielen.*?Flip 7\.",
        "Welches Kartenspiel du am liebsten spielst, ist Skyjo oder Flip 7.",
        draft.body,
        flags=re.S,
    )
    assert any("card-game" in item for item in errors_for(config, valid_facts(), draft))


def test_unnecessary_favorite_food_self_cooking_add_on_is_blocked(config: dict) -> None:
    draft = valid_draft()
    draft.body = draft.body.replace(
        "Ich suche ein langfristiges Zuhause für ungefähr zwei Jahre.",
        "Mein Lieblingsessen ist Hähnchenpasta mit Sahnesoße; das würde ich zum Einzug "
        "natürlich auch gerne selbst kochen.",
    )
    assert any("self-cooking" in item for item in errors_for(config, valid_facts(), draft))


def test_proof_of_reading_instruction_restatement_is_blocked(config: dict) -> None:
    draft = valid_draft()
    draft.body = draft.body.replace(
        "Bei Kartenspielen bin ich übrigens ganz klar",
        "Damit ihr wisst, dass ich die Anzeige gelesen habe: Bei Kartenspielen bin ich",
    )
    assert any("awkwardly restated" in item for item in errors_for(config, valid_facts(), draft))


def test_missing_hidden_answer_is_blocked(config: dict) -> None:
    facts = valid_facts().model_copy(
        update={
            "hidden_questions": [
                HiddenQuestion(id="q_card", question="Welches Kartenspiel spielst du am liebsten?")
            ]
        }
    )
    assert any("unanswered" in item for item in errors_for(config, facts, valid_draft()))


def test_claimed_hidden_answer_must_appear(config: dict) -> None:
    facts = valid_facts().model_copy(
        update={
            "hidden_questions": [
                HiddenQuestion(id="q_drink", question="Was ist dein Lieblingsgetränk?")
            ]
        }
    )
    draft = valid_draft().model_copy(update={"answered_question_ids": ["q_drink"]})
    assert any("not verifiable" in item for item in errors_for(config, facts, draft))


def test_unknown_subjective_answer_cannot_be_auto_verified(config: dict) -> None:
    facts = valid_facts().model_copy(
        update={"hidden_questions": [HiddenQuestion(id="q_color", question="Lieblingsfarbe?")]}
    )
    draft = valid_draft().model_copy(update={"answered_question_ids": ["q_color"]})
    assert any(
        "not deterministically verifiable" in item for item in errors_for(config, facts, draft)
    )


def test_exact_subject_and_first_word_verified(config: dict) -> None:
    commands = [
        HiddenCommand(
            id="cmd_subject",
            instruction='Der Betreff soll "Sonnenblume" lauten.',
            kind="exact_subject",
            exact_text="Sonnenblume",
            target="subject",
        ),
        HiddenCommand(
            id="cmd_word",
            instruction='Beginne die Nachricht mit "Moin".',
            kind="required_first_word",
            exact_text="Moin",
        ),
    ]
    facts = valid_facts().model_copy(update={"hidden_commands": commands})
    draft = valid_draft().model_copy(update={"applied_command_ids": ["cmd_subject", "cmd_word"]})
    errors = errors_for(config, facts, draft)
    assert any("exact subject" in item for item in errors)
    assert any("first word" in item for item in errors)


def test_command_passes_when_exactly_applied(config: dict) -> None:
    command = HiddenCommand(
        id="cmd_keyword",
        instruction='Nutze das Schlüsselwort "Sonnenblume".',
        kind="required_keyword",
        exact_text="Sonnenblume",
        target="either",
    )
    facts = valid_facts().model_copy(update={"hidden_commands": [command]})
    draft = valid_draft().model_copy(
        update={
            "body": valid_draft().body + " Sonnenblume.",
            "applied_command_ids": ["cmd_keyword"],
        }
    )
    assert not any("command" in item for item in errors_for(config, facts, draft))


def test_wrong_language_and_register_are_blocked(config: dict) -> None:
    draft = valid_draft().model_copy(update={"language": "en", "address_register": "sie"})
    errors = errors_for(config, valid_facts(), draft)
    assert any("language" in item for item in errors)
    assert any("register" in item for item in errors)


def test_generic_opener_and_no_hook_are_blocked(config: dict) -> None:
    draft = valid_draft()
    draft.body = "Ich bewerbe mich sehr gerne. " + draft.body
    draft.hooks_used = []
    errors = errors_for(config, valid_facts(), draft)
    assert any("generic" in item for item in errors)
    assert any("hook" in item for item in errors)


def test_unresolved_question_is_blocked(config: dict) -> None:
    draft = valid_draft().model_copy(update={"unresolved_required_facts": ["favorite color"]})
    assert any("unresolved" in item for item in errors_for(config, valid_facts(), draft))


def test_email_requires_subject(config: dict) -> None:
    facts = valid_facts().model_copy(update={"contact_method": "email"})
    draft = valid_draft().model_copy(update={"subject": ""})
    assert any("email subject" in item for item in errors_for(config, facts, draft))


def test_document_policy_violation_is_blocked(config: dict) -> None:
    result = validate_message(
        viable_listing(),
        valid_facts(),
        RuleDecision(decision="APPLY"),
        valid_draft(),
        AttachmentDecision(allowed=False, should_attach=True, path="/tmp/private.pdf"),
        config,
    )
    assert any("document policy" in item for item in result.errors)


@pytest.mark.parametrize(
    "phrase",
    [
        "Ich bringe die nötige Reife für ein entspanntes Zusammenleben mit.",
        "Ich erfülle die Anforderungen des Inserats vollständig.",
        "Ich bin überzeugt, gut zu euch zu passen.",
    ],
)
def test_generic_application_phrasing_is_blocked(phrase: str, config: dict) -> None:
    draft = valid_draft()
    draft.body += f" {phrase}"
    assert any(
        "generic" in item and "natural" in item for item in errors_for(config, valid_facts(), draft)
    )


def test_generic_application_phrasing_allowed_for_formal_landlord(config: dict) -> None:
    listing = viable_listing(
        raw_text="Studio von einem privaten Vermieter. Bitte stellen Sie sich vor."
    )
    facts = valid_facts().model_copy(
        update={"housing_type": "studio", "advertiser_type": "private_landlord"}
    )
    draft = valid_draft().model_copy(update={"address_register": "sie"})
    draft.body += " Ich erfülle die Anforderungen des Inserats vollständig."
    result = validate_message(
        listing, facts, RuleDecision(decision="APPLY"), draft, AttachmentDecision(), config
    )
    assert not any("generic" in item and "natural" in item for item in result.errors)


def test_repeated_personality_trait_listing_is_blocked(config: dict) -> None:
    draft = valid_draft()
    draft.body += (
        " Ich bin eher ruhig, verlässlich und ordentlich und passe deshalb gut in eine "
        "entspannte WG. Insgesamt bin ich im Alltag ziemlich ruhig, zuverlässig und ordentlich."
    )
    errors = errors_for(config, valid_facts(), draft)
    assert any("trait listing" in item or "repeated" in item for item in errors)


def test_single_personality_trait_mention_is_fine(config: dict) -> None:
    draft = valid_draft()
    draft.body += " Ich bin ziemlich unkompliziert und komme gut mit anderen klar."
    errors = errors_for(config, valid_facts(), draft)
    assert not any("trait listing" in item for item in errors)
