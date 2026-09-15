from __future__ import annotations

from dataclasses import replace

from app.analyzer import canonicalize_hidden_ids, process_listing
from app.contact import contact_listing
from app.database import Database
from app.providers import ProviderUnavailable
from app.schemas import ContactResult, HiddenQuestion
from tests.conftest import successful_call, viable_listing


def test_prefilter_skip_uses_zero_ai_calls(settings, config, answers) -> None:
    called = 0

    def must_not_run(*_args):
        nonlocal called
        called += 1
        raise AssertionError("AI was called for deterministic hard skip")

    listing = viable_listing(raw_text="WG-Zimmer, nur für Frauen, 450 € warm.")
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=settings,
        callers={"anthropic": must_not_run},
    )
    assert outcome.status == "filtered_skip"
    assert called == 0


def test_viable_listing_uses_one_cloud_call(settings, config, answers) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer 490 € warm, mindestens 12 Monate. Wir kochen zusammen und fahren Rad. "
            "Damit wir wissen, dass du die Anzeige gelesen hast: Schreib uns bitte, welches "
            "Kartenspiel du am liebsten spielst."
        )
    )
    expected = successful_call(listing, config)
    called = 0

    def provider(*_args):
        nonlocal called
        called += 1
        return expected

    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": provider},
    )
    assert called == 1
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.validation.auto_send_allowed


def test_ordinary_lease_questions_do_not_block_first_contact(settings, config, answers) -> None:
    listing = viable_listing()
    expected = successful_call(listing, config)
    ordinary = ["Kaution sowie Kaltmiete und Nebenkostenaufteilung sind noch nicht angegeben."]
    expected.data.facts.unresolved_required_facts = ordinary
    expected.data.message.unresolved_required_facts = ordinary
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": lambda *_: expected},
    )
    assert outcome.status == "drafted", outcome.validation.errors


def test_ai_hidden_ids_are_canonicalized(config) -> None:
    expected = successful_call(viable_listing(), config)
    expected.data.facts.hidden_questions = [
        HiddenQuestion(id="model_random_id", question="Was machst du am Wochenende?")
    ]
    expected.data.message.answered_question_ids = ["model_random_id"]
    first = canonicalize_hidden_ids(expected.data)
    second = canonicalize_hidden_ids(expected.data)
    assert first.facts.hidden_questions[0].id.startswith("q_")
    assert first.facts.hidden_questions[0].id == second.facts.hidden_questions[0].id
    assert first.message.answered_question_ids == [first.facts.hidden_questions[0].id]


def test_provider_failure_is_persisted(settings, config, answers) -> None:
    def unavailable(*_args):
        raise ProviderUnavailable("no quota")

    database = Database(settings.database_path)
    outcome = process_listing(
        viable_listing(),
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        database=database,
        callers={"anthropic": unavailable},
    )
    assert outcome.status == "ai_failed"
    assert database.status_counts()["ai_failed"] == 1


def test_listing_deduplicates_by_platform_and_id(settings) -> None:
    database = Database(settings.database_path)
    first = viable_listing(url="https://example.test/x?utm_source=mail")
    second = viable_listing(url="https://example.test/different")
    first_id, created = database.discover(first)
    second_id, second_created = database.discover(second)
    assert created
    assert not second_created
    assert first_id == second_id


def test_duplicate_actual_contact_is_blocked(settings, config, answers, monkeypatch) -> None:
    listing = viable_listing(
        platform="asta_muenster",
        contact_email="landlord@example.test",
        url="https://example.test/room",
    )
    expected = successful_call(listing, config)
    expected.data.facts.contact_method = "email"
    expected.data.facts.contact_email = "landlord@example.test"
    actual_settings = replace(
        settings,
        provider_order=("anthropic",),
        dry_run=False,
        auto_send=True,
    )
    database = Database(settings.database_path)
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=actual_settings,
        database=database,
        callers={"anthropic": lambda *_: expected},
    )
    monkeypatch.setattr(
        "app.contact.send_gmail",
        lambda *_args, **_kwargs: ContactResult(status="sent", external_message_id="gmail-1"),
    )
    first = contact_listing(outcome, actual_settings, database)
    second = contact_listing(outcome, actual_settings, database)
    assert first.status == "sent"
    assert second.status == "review_required"
    assert "duplicate" in second.detail
