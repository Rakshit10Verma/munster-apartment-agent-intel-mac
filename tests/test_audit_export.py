from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from app.audit_export import export_audit
from app.database import Database
from app.schemas import (
    AnalysisOutcome,
    AttachmentDecision,
    GenerationTrace,
    HiddenAnswerTrace,
    HiddenQuestion,
    ListingFacts,
    MessageDraft,
    RuleDecision,
    ValidationResult,
)
from tests.conftest import viable_listing

MESSAGE = (
    "Hey zusammen,\n\n"
    "ich bin Rakshit und interessiere mich für euer WG-Zimmer. Eure gemeinsamen Kochabende "
    "klingen sehr sympathisch. Bei Kartenspielen bin ich gerne bei Skyjo dabei.\n\n"
    "Liebe Grüße\nRakshit"
)


def _fixture_root(tmp_path: Path, database_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "logs").mkdir(parents=True)
    (root / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "applicant": {
                    "name": "Rakshit Verma",
                    "age": 21,
                    "personal_facts": {"preferred_fun_fact": {"kind": "berlin_ring_cycle"}},
                },
                "housing_rules": {
                    "max_warm_rent_single_eur": 600,
                    "min_duration_months": 6,
                    "min_zwischenmiete_months": 1,
                },
                "generation": {"never_invent_personal_facts": True},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (root / "answer_bank.yaml").write_text(
        yaml.safe_dump({"confirmed_answer_bank": {"card_games": ["Skyjo"]}}),
        encoding="utf-8",
    )
    (root / ".env").write_text(
        f"DATABASE_PATH={database_path}\n"
        "AUTO_SEND=false\n"
        "DRY_RUN=true\n"
        "AI_MAX_RETRIES=1\n"
        "OPENAI_API_KEY=sk-test-secret-value\n"
        "BEWERBERMAPPE_PATH=/private/secret/dossier.pdf\n",
        encoding="utf-8",
    )
    (root / "logs" / "agent.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-09-17T10:00:00+00:00",
                "logger": "apartment_agent.analyzer",
                "message": "listing analyzed",
                "listing_id": 1,
                "api_key": "sk-test-secret-value",
                "authorization": "Bearer bearer-secret-token",
                "browser_url": "https://example.invalid/conversation?session=browser-secret",
            }
        )
        + "\n"
        + json.dumps(
            {
                "timestamp": "2026-09-17T10:01:00+00:00",
                "logger": "apartment_agent.analyzer",
                "message": "other listing",
                "listing_id": 999,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "logs" / "daemon-console.log").write_text(
        "password=hunter2\nBearer bearer-secret-token\n",
        encoding="utf-8",
    )
    return root


def _representative_listing(database: Database) -> int:
    listing = viable_listing(
        listing_id="15000001",
        url="https://www.wg-gesucht.de/wg-zimmer.15000001.html",
        title="WG-Zimmer mit Balkon",
        raw_text=(
            "WG-Zimmer in Münster-Hafen, 490 € warm, mindestens 12 Monate. "
            "Schreib uns, welches Kartenspiel du magst."
        ),
    )
    question = HiddenQuestion(id="q_cards", question="Welches Kartenspiel magst du?")
    outcome = AnalysisOutcome(
        listing=listing,
        facts=ListingFacts(
            housing_type="wg_room",
            advertiser_type="wg",
            location="Münster-Hafen",
            warm_rent_eur=490,
            room_size_m2=18,
            minimum_duration_months=12,
            hidden_questions=[question],
        ),
        rule_decision=RuleDecision(decision="APPLY"),
        status="drafted",
        message=MessageDraft(
            body=MESSAGE,
            language="de",
            address_register="du",
            answered_question_ids=[question.id],
        ),
        attachment=AttachmentDecision(
            allowed=True,
            should_attach=True,
            path="/private/secret/dossier.pdf",
            source="wg_account",
        ),
        validation=ValidationResult(auto_send_allowed=True),
        generation_trace=GenerationTrace(
            ai_attempted=True,
            ai_failure_reason="quota exhausted",
            fallback_used=True,
            fallback_scenario="wg_room",
            fallback_template="de_wg_wg_room",
            hidden_question_answers=[
                HiddenAnswerTrace(
                    question_id=question.id,
                    category="favorite_card_game",
                    answer_source="confirmed_answer_bank",
                    answer="Skyjo",
                )
            ],
        ),
        message_source="universal_answer_bank_fallback",
    )
    return database.save_outcome(outcome)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_export_reconstructs_listing_and_never_mutates_database(tmp_path: Path) -> None:
    database_path = tmp_path / "agent.sqlite"
    database = Database(database_path)
    listing_db_id = _representative_listing(database)
    root = _fixture_root(tmp_path, database_path)
    before_hash = _sha256(database_path)
    before_rows = sqlite3.connect(database_path).execute("SELECT COUNT(*) FROM listings").fetchone()

    result = export_audit(
        root=root,
        database_path=database_path,
        output_root=tmp_path / "exports",
        create_zip=False,
        now=datetime(2026, 9, 17, 12, 45, tzinfo=UTC),
    )

    after_rows = sqlite3.connect(database_path).execute("SELECT COUNT(*) FROM listings").fetchone()
    assert _sha256(database_path) == before_hash
    assert after_rows == before_rows
    assert result.listing_count == 1
    detail = (result.directory / "listings" / f"{listing_db_id}.md").read_text(encoding="utf-8")
    for heading in (
        "A. Original listing",
        "B. Extracted facts",
        "C. Filtering decision",
        "D. Reasons",
        "E. Hidden questions",
        "F. AI and fallback generation",
        "G. Exact final application message",
        "H. Send gates",
        "I. Send attempts and result",
        "J. Errors and related lifecycle entries",
    ):
        assert heading in detail
    assert "Münster-Hafen" in detail
    for line in (line for line in MESSAGE.splitlines() if line):
        assert f"    {line}" in detail
    assert "favorite_card_game" in detail
    assert (result.directory / "status.txt").is_file()
    assert (result.directory / "database_schema.sql").is_file()


def test_single_listing_export_is_compact_and_filters_structured_logs(tmp_path: Path) -> None:
    database_path = tmp_path / "agent.sqlite"
    database = Database(database_path)
    target_id = _representative_listing(database)
    other = viable_listing(
        listing_id="15000002",
        url="https://www.wg-gesucht.de/wg-zimmer.15000002.html",
    )
    database.discover(other)
    root = _fixture_root(tmp_path, database_path)

    result = export_audit(
        root=root,
        database_path=database_path,
        listing_db_id=target_id,
        output_root=tmp_path / "exports",
        create_zip=False,
    )

    assert result.listing_count == 1
    assert [path.name for path in (result.directory / "listings").glob("*.md")] == [
        f"{target_id}.md"
    ]
    log_text = (result.directory / "logs" / "agent.jsonl").read_text(encoding="utf-8")
    assert "listing analyzed" in log_text
    assert "other listing" not in log_text
    assert not (result.directory / "logs" / "daemon-console.log").exists()


def test_secret_values_and_secret_looking_fields_are_redacted(tmp_path: Path) -> None:
    database_path = tmp_path / "agent.sqlite"
    database = Database(database_path)
    _representative_listing(database)
    root = _fixture_root(tmp_path, database_path)

    result = export_audit(
        root=root,
        database_path=database_path,
        output_root=tmp_path / "exports",
        create_zip=True,
    )

    forbidden = (
        "sk-test-secret-value",
        "/private/secret/dossier.pdf",
        "bearer-secret-token",
        "browser-secret",
        "hunter2",
    )
    for path in result.directory.rglob("*"):
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            assert all(secret not in content for secret in forbidden), path
    runtime = json.loads(
        (result.directory / "config_snapshot" / "runtime_gates.json").read_text(encoding="utf-8")
    )
    assert runtime == {"AUTO_SEND": False, "DRY_RUN": True, "AI_MAX_RETRIES": 1}
    assert result.zip_path is not None
    with zipfile.ZipFile(result.zip_path) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            content = archive.read(name)
            assert all(secret.encode() not in content for secret in forbidden), name


def test_exporter_does_not_import_or_call_sending_or_reconciliation_paths(
    tmp_path: Path, monkeypatch
) -> None:
    database_path = tmp_path / "agent.sqlite"
    database = Database(database_path)
    _representative_listing(database)
    root = _fixture_root(tmp_path, database_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("audit export must never enter a contact or reconciliation path")

    monkeypatch.setattr("app.contact.contact_listing", forbidden)
    monkeypatch.setattr("app.browser.reconcile_wg_url", forbidden)
    monkeypatch.setattr("app.browser.prepare_platform_contact", forbidden)

    result = export_audit(
        root=root,
        database_path=database_path,
        output_root=tmp_path / "exports",
        create_zip=False,
    )

    assert result.listing_count == 1
    assert database.status_counts() == {"drafted": 1}
