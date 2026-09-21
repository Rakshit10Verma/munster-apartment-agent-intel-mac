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


def test_ai_cannot_turn_accepted_fee_below_500_into_scam_review(settings, config, answers) -> None:
    listing = viable_listing(
        raw_text="WG-Zimmer 490 € warm, mindestens 12 Monate. Ablöse für Möbel: 350 €."
    )
    expected = successful_call(listing, config)
    expected.data.facts.scam_risk = "medium"
    expected.data.facts.scam_reasons = ["unusual furniture fee"]
    expected.data.facts.odd_fee_or_payment_demands = ["Ablöse for furniture: 350 EUR"]
    expected.data.facts.critical_ambiguities = ["furniture fee of 350 EUR may be unusual"]

    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": lambda *_: expected},
    )

    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.facts.one_time_fee_eur == 350
    assert outcome.facts.scam_risk == "low"
    assert not outcome.facts.scam_reasons
    assert not outcome.facts.odd_fee_or_payment_demands
    assert not outcome.facts.critical_ambiguities


def test_provider_failure_can_use_fallback_with_accepted_fee_below_500(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text="WG-Zimmer 490 € warm, mindestens 12 Monate. Möbelablöse: 499 €."
    )

    def unavailable(*_args):
        raise ProviderUnavailable("timeout")

    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": unavailable},
    )

    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_fallback"
    assert outcome.facts.scam_risk == "low"


def test_ai_payment_method_concern_is_preserved_even_with_fee_below_500(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text="WG-Zimmer 490 € warm, mindestens 12 Monate. Möbelablöse: 350 €."
    )
    expected = successful_call(listing, config)
    expected.data.facts.scam_risk = "medium"
    expected.data.facts.scam_reasons = ["unusual payment method requires verification"]
    expected.data.facts.odd_fee_or_payment_demands = ["payment via an unverified channel"]

    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": lambda *_: expected},
    )

    assert outcome.status == "review_required"
    assert outcome.facts.scam_risk == "medium"
    assert outcome.facts.scam_reasons
    assert outcome.facts.odd_fee_or_payment_demands


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


def test_provider_failure_uses_universal_fallback_and_is_persisted(
    settings, config, answers
) -> None:
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
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_fallback"
    assert outcome.message and "Bewerbermappe" not in outcome.message.body
    assert database.status_counts()["drafted"] == 1
    with database.connect() as connection:
        row = connection.execute(
            "SELECT message_source FROM listings WHERE id=?", (outcome.database_id,)
        ).fetchone()
    assert row["message_source"] == "universal_fallback"


def test_provider_failure_with_known_card_game_question_resolves_via_answer_bank(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            "Schreib uns in deiner Nachricht: Welches Kartenspiel spielst du am liebsten?"
        )
    )

    def unavailable(*_args):
        raise ProviderUnavailable("timeout")

    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": unavailable},
    )
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_answer_bank_fallback"
    assert outcome.message is not None
    assert "skyjo" in outcome.message.body.casefold() or "flip 7" in outcome.message.body.casefold()
    assert outcome.message.answered_question_ids
    assert outcome.validation.auto_send_allowed


def test_provider_failure_with_soft_age_range_uses_personalized_fallback(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            "Gesucht wird: Geschlecht egal zwischen 25 und 35 Jahren."
        )
    )

    def unavailable(*_args):
        raise ProviderUnavailable("timeout")

    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": unavailable},
    )
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_fallback"
    assert outcome.message and "etwas jünger" in outcome.message.body
    assert any("soft age mismatch" in warning for warning in outcome.rule_decision.warnings)


def test_provider_failure_with_exact_subject_command_resolves_via_answer_bank(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            'Der Betreff soll "Sonnenschein" lauten.'
        )
    )

    def unavailable(*_args):
        raise ProviderUnavailable("timeout")

    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": unavailable},
    )
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_answer_bank_fallback"
    assert outcome.message is not None
    assert outcome.message.subject == "Sonnenschein"
    assert outcome.validation.auto_send_allowed


def test_provider_failure_with_unresolvable_exact_command_requires_review(
    settings, config, answers
) -> None:
    listing = viable_listing(
        raw_text=(
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            "Der Betreff soll euren WG-Spitznamen enthalten und lauten."
        )
    )

    def unavailable(*_args):
        raise ProviderUnavailable("timeout")

    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": unavailable},
    )
    assert outcome.status == "review_required"
    assert outcome.message_source == "none"
    assert any("command" in note for note in outcome.router_notes)


def test_universal_fallback_never_bypasses_hard_skip(settings, config, answers) -> None:
    calls = 0

    def unavailable(*_args):
        nonlocal calls
        calls += 1
        raise ProviderUnavailable("timeout")

    outcome = process_listing(
        viable_listing(raw_text="WG-Zimmer nur für Frauen, 490 € warm."),
        config=config,
        answers=answers,
        settings=replace(settings, provider_order=("anthropic",)),
        callers={"anthropic": unavailable},
    )
    assert outcome.status == "filtered_skip"
    assert outcome.message_source == "none"
    assert calls == 0


def test_existing_wg_conversation_skips_ai_and_is_persisted(settings, config, answers) -> None:
    calls = 0

    def must_not_run(*_args):
        nonlocal calls
        calls += 1
        raise AssertionError("AI must not run for an existing conversation")

    listing = viable_listing(
        source_metadata={
            "wg_contact_state": "already_contacted",
            "wg_contact_state_detail": "conversation href found",
        }
    )
    database = Database(settings.database_path)
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=settings,
        database=database,
        callers={"anthropic": must_not_run},
    )
    assert outcome.status == "already_contacted"
    assert calls == 0
    assert database.status_counts() == {"already_contacted": 1}


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
    first = contact_listing(outcome, actual_settings, database, "auto")
    second = contact_listing(outcome, actual_settings, database, "auto")
    assert first.status == "sent"
    assert second.status == "already_contacted"
    assert "duplicate" in second.detail
