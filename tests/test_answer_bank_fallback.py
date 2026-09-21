# ruff: noqa: E501
from __future__ import annotations

from dataclasses import replace

import yaml

from app.analyzer import process_listing
from app.config_loader import ROOT
from app.database import Database
from app.providers import ProviderUnavailable
from app.schemas import SourceListing
from tests.conftest import viable_listing


def _unavailable(*_args):
    raise ProviderUnavailable("all cloud providers unavailable")


def test_real_listing_14096490_survives_full_cloud_outage_via_answer_bank(
    settings, config, answers
) -> None:
    """End-to-end regression for the real WG-Gesucht listing 14096490: a full cloud
    outage plus an age soft-mismatch plus a known hidden question must still reach a
    validated, sendable APPLY draft via the answer-bank fallback."""
    payload = yaml.safe_load((ROOT / "tests/fixtures/wg_14096490.yaml").read_text(encoding="utf-8"))
    listing = SourceListing.model_validate(payload)

    database = Database(settings.database_path)
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic", "openai", "gemini")),
        database=database,
        callers={"anthropic": _unavailable, "openai": _unavailable, "gemini": _unavailable},
    )

    assert outcome.rule_decision.decision == "APPLY"
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_answer_bank_fallback"
    assert outcome.message is not None
    body = outcome.message.body
    body_cf = body.casefold()
    # hidden question resolved from the confirmed answer bank
    assert "hähnchenpasta" in body_cf
    assert outcome.message.answered_question_ids
    # soft age mismatch personalization still present
    assert "etwas jünger" in body
    # register stays du/ihr for this WG
    assert outcome.message.address_register == "du"
    # validator, Premium, and Bewerbermappe gates all still ran and passed
    assert outcome.validation.auto_send_allowed
    assert not outcome.validation.errors
    assert outcome.attachment.should_attach
    assert outcome.attachment.source == "wg_account"
    # AUTO_SEND remains disabled throughout this test
    assert settings.auto_send is False
    assert database.status_counts()["drafted"] == 1
    with database.connect() as connection:
        row = connection.execute(
            "SELECT message_source FROM listings WHERE id=?", (outcome.database_id,)
        ).fetchone()
    assert row["message_source"] == "universal_answer_bank_fallback"


def test_provider_failure_with_doppelkopf_question_resolves_via_answer_bank(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            "Schreib uns, ob du gerne Doppelkopf spielst."
        )
    )
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": _unavailable},
    )
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_answer_bank_fallback"
    assert outcome.message is not None
    body_cf = outcome.message.body.casefold()
    assert "doppelkopf" in body_cf
    assert "nicht" in body_cf
    assert outcome.validation.auto_send_allowed


def test_provider_failure_with_music_question_resolves_via_answer_bank(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            "Sag uns, welche Musik du gerne hörst."
        )
    )
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": _unavailable},
    )
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_answer_bank_fallback"
    assert outcome.message is not None
    body_cf = outcome.message.body.casefold()
    assert any(word in body_cf for word in ("bollywood", "kanye west", "eminem"))
    assert outcome.validation.auto_send_allowed


def test_provider_failure_with_politics_question_uses_confirmed_boundary_answer(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            "Schreib uns bitte, wie du zur Politik stehst."
        )
    )
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": _unavailable},
    )
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_answer_bank_fallback"
    assert outcome.message is not None
    body_cf = outcome.message.body.casefold()
    assert "lieber nicht" in body_cf
    assert outcome.validation.auto_send_allowed


def test_provider_failure_with_unknown_favorite_movie_requires_review(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            "Sag uns, was dein Lieblingsfilm ist."
        )
    )
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": _unavailable},
    )
    assert outcome.status == "review_required"
    assert outcome.message_source == "none"
    assert outcome.message is None
    assert not outcome.validation.auto_send_allowed
    assert any("answer bank" in note for note in outcome.router_notes)


def test_provider_failure_with_two_known_questions_both_resolved(settings, config, answers) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            "Schreib uns in deiner Nachricht, welches dein Lieblingsgetränk ist. "
            "Schreib uns außerdem, welche Musik du hörst."
        )
    )
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": _unavailable},
    )
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_answer_bank_fallback"
    assert outcome.message is not None
    assert len(outcome.message.answered_question_ids) == 2
    body_cf = outcome.message.body.casefold()
    assert "kokoswasser" in body_cf
    assert any(word in body_cf for word in ("bollywood", "kanye west", "eminem"))
    assert outcome.validation.auto_send_allowed


def test_provider_failure_with_one_known_and_one_unknown_question_requires_review(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            "Schreib uns in deiner Nachricht, welches dein Lieblingsgetränk ist. "
            "Sag uns außerdem, was dein Lieblingsfilm ist."
        )
    )
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": _unavailable},
    )
    assert outcome.status == "review_required"
    assert outcome.message_source == "none"
    assert not outcome.validation.auto_send_allowed


def test_provider_failure_with_safe_keyword_command_applied_and_verified(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            'Das Schlüsselwort lautet "Sonnenblume".'
        )
    )
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": _unavailable},
    )
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_answer_bank_fallback"
    assert outcome.message is not None
    assert "sonnenblume" in outcome.message.body.casefold()
    assert outcome.message.applied_command_ids
    assert outcome.validation.auto_send_allowed


def test_hard_skip_overrides_answer_bank_fallback(settings, config, answers) -> None:
    calls = 0

    def must_not_run(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("AI must not run for a deterministic hard skip")

    listing = viable_listing(
        raw_text=(
            "WG-Zimmer nur für Frauen, 490 € warm, mindestens 12 Monate. "
            "Schreib uns, was dein Lieblingsessen ist."
        )
    )
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": must_not_run},
    )
    assert outcome.status == "filtered_skip"
    assert outcome.message_source == "none"
    assert calls == 0
