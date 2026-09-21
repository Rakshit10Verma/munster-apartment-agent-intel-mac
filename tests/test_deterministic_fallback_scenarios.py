from __future__ import annotations

import sqlite3
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from app.analyzer import process_listing
from app.database import Database
from app.providers import ProviderUnavailable
from app.schemas import SourceListing


def _provider_failure(message: str = "all providers unavailable"):
    def fail(*_args):
        raise ProviderUnavailable(message)

    return fail


def _listing(listing_id: str, title: str, text: str) -> SourceListing:
    return SourceListing(
        platform="wg_gesucht",
        listing_id=listing_id,
        url=f"https://www.wg-gesucht.de/angebot.{listing_id}.html",
        title=title,
        raw_text=text,
    )


def _outage_outcome(listing, settings, config, answers, *, failure="outage"):
    return process_listing(
        listing,
        settings=replace(settings, provider_order=("anthropic",)),
        config=config,
        answers=answers,
        callers={"anthropic": _provider_failure(failure)},
    )


@pytest.mark.parametrize(
    ("listing", "scenario", "expected_phrase"),
    [
        (
            _listing(
                "9100001",
                "Zimmer in entspannter 3er-WG",
                "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. Wir kochen gerne.",
            ),
            "wg_room",
            "WG-Leben",
        ),
        (
            _listing(
                "9100002",
                "Helles Studio",
                "Studio in Münster, 550 € warm, mindestens 12 Monate, möbliert.",
            ),
            "studio",
            "eigenes Zuhause",
        ),
        (
            _listing(
                "9100003",
                "Ruhige 2-Zimmer-Wohnung",
                "2-Zimmer-Wohnung in Münster, 580 € warm, mindestens 12 Monate, unmöbliert.",
            ),
            "whole_apartment",
            "langfristiges Zuhause",
        ),
        (
            _listing(
                "9100004",
                "Zwischenmiete für zwei Monate",
                "Zwischenmiete in Münster für 2 Monate, 500 € warm, möbliert.",
            ),
            "zwischenmiete",
            "Zwischenmietzeitraum",
        ),
        (
            _listing(
                "9100005",
                "Große 3-Zimmer-Wohnung",
                "3-Zimmer-Wohnung in Münster, 1100 € warm, mindestens 12 Monate. "
                "WG-Gründung ist möglich.",
            ),
            "whole_apartment_shared_later",
            "später mit Mitbewohnern teilen",
        ),
        (
            _listing(
                "9100006",
                "Zimmer im Studentenwohnheim",
                "Studentenwohnheim in Münster, 420 € warm, mindestens 12 Monate.",
            ),
            "student_dorm",
            "studentische Wohnangebot",
        ),
    ],
)
def test_cloud_outage_uses_scenario_fallback(
    listing, scenario, expected_phrase, settings, config, answers
) -> None:
    outcome = _outage_outcome(listing, settings, config, answers)

    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.validation.auto_send_allowed
    assert outcome.message is not None
    assert expected_phrase in outcome.message.body
    assert outcome.generation_trace.ai_attempted
    assert outcome.generation_trace.fallback_used
    assert outcome.generation_trace.fallback_scenario == scenario
    assert outcome.generation_trace.fallback_template


def test_zero_credit_failure_is_equivalent_to_an_outage(settings, config, answers) -> None:
    listing = _listing(
        "9100010",
        "WG-Zimmer mit Balkon",
        "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate, mit Balkon.",
    )
    outcome = _outage_outcome(
        listing,
        settings,
        config,
        answers,
        failure="provider rejected request: insufficient credits",
    )

    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message_source == "universal_fallback"
    assert "insufficient credits" in (outcome.generation_trace.ai_failure_reason or "")


def test_furnished_and_unfurnished_clauses_are_selected(settings, config, answers) -> None:
    furnished = _outage_outcome(
        _listing(
            "9100011",
            "Möbliertes Studio",
            "Studio in Münster, 530 € warm, mindestens 12 Monate, möbliert.",
        ),
        settings,
        config,
        answers,
    )
    unfurnished = _outage_outcome(
        _listing(
            "9100012",
            "Unmöblierte Wohnung",
            "2-Zimmer-Wohnung in Münster, 590 € warm, mindestens 12 Monate, unmöbliert.",
        ),
        settings,
        config,
        answers,
    )

    assert "furnishing:furnished" in furnished.generation_trace.optional_clauses
    assert "furnishing:unfurnished" in unfurnished.generation_trace.optional_clauses


def test_landlord_and_wg_use_different_registers_and_closings(settings, config, answers) -> None:
    landlord = _outage_outcome(
        _listing(
            "9100013",
            "Studio von privater Vermieterin",
            "Private Vermieterin bietet ein Studio in Münster für 540 € warm und mindestens "
            "12 Monate an.",
        ),
        settings,
        config,
        answers,
    )
    wg = _outage_outcome(
        _listing(
            "9100014",
            "Zimmer in 4er-WG",
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. Wir sind vier Leute.",
        ),
        settings,
        config,
        answers,
    )

    assert landlord.message and landlord.message.address_register == "sie"
    assert landlord.message.body.endswith("Freundliche Grüße\nRakshit Verma")
    assert wg.message and wg.message.address_register == "du"
    assert wg.message.body.endswith("Liebe Grüße\nRakshit")


def test_soft_age_mismatch_does_not_block_fallback(settings, config, answers) -> None:
    listing = _listing(
        "9100015",
        "WG sucht Person zwischen 24 und 29",
        "WG-Zimmer in Münster, 480 € warm, mindestens 12 Monate. Wir suchen jemanden "
        "zwischen 24 und 29 Jahren.",
    )
    outcome = _outage_outcome(listing, settings, config, answers)

    assert outcome.facts.age_mismatch
    assert outcome.facts.age_requirement_strength != "strict"
    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message and "etwas jünger" in outcome.message.body


def test_fun_fact_is_answered_from_applicant_profile(settings, config, answers) -> None:
    listing = _listing(
        "9100016",
        "WG-Zimmer mit versteckter Frage",
        "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. Schreib uns bitte: "
        "Was ist ein Fun Fact über dich?",
    )
    outcome = _outage_outcome(listing, settings, config, answers)

    assert outcome.status == "drafted", outcome.validation.errors
    assert outcome.message and "50 km" in outcome.message.body
    assert outcome.message_source == "universal_answer_bank_fallback"
    assert outcome.generation_trace.hidden_question_answers[0].answer_source == "applicant_profile"


def test_unknown_hidden_question_requires_review(settings, config, answers) -> None:
    listing = _listing(
        "9100017",
        "WG-Zimmer mit Frage",
        "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. Schreib uns bitte: "
        "Welcher ist dein Lieblingsfilm?",
    )
    outcome = _outage_outcome(listing, settings, config, answers)

    assert outcome.status == "review_required"
    assert outcome.message is None
    assert not outcome.generation_trace.fallback_used


def test_missing_profile_fact_is_omitted_never_fabricated(settings, config, answers) -> None:
    reduced_config = deepcopy(config)
    reduced_config["applicant"].pop("employer")
    reduced_config["applicant"].pop("work_mode")
    listing = _listing(
        "9100018",
        "WG-Zimmer",
        "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. Wir kochen gerne zusammen.",
    )
    outcome = _outage_outcome(listing, settings, reduced_config, answers)

    assert outcome.message is not None
    assert "Landesbausparkasse" not in outcome.message.body
    assert "Einkommen" not in outcome.message.body


def test_generation_trace_is_persisted(settings, config, answers) -> None:
    database = Database(settings.database_path)
    listing = _listing(
        "9100019",
        "WG-Zimmer",
        "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate.",
    )
    outcome = process_listing(
        listing,
        settings=replace(settings, provider_order=("anthropic",)),
        config=config,
        answers=answers,
        database=database,
        callers={"anthropic": _provider_failure()},
    )

    row = database.get_listing(outcome.database_id)
    assert row is not None
    assert '"fallback_used":true' in row["generation_trace_json"]
    assert '"fallback_scenario":"wg_room"' in row["generation_trace_json"]


def test_generation_trace_column_is_added_to_an_existing_database(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            "CREATE TABLE listings (id INTEGER PRIMARY KEY, dedup_key TEXT UNIQUE, "
            "status TEXT NOT NULL DEFAULT 'discovered')"
        )

    database = Database(legacy_path)
    with database.connect() as connection:
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(listings)").fetchall()
        }

    assert "generation_trace_json" in columns
