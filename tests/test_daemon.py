from __future__ import annotations

from app.daemon import ApartmentDaemon
from app.database import Database
from app.schemas import ContactResult
from app.sources import SourceAdapter
from tests.conftest import successful_call, viable_listing


class BrokenSource(SourceAdapter):
    name = "broken"

    def discover(self):
        raise RuntimeError("temporary source outage")


def test_daemon_logs_source_failure_instead_of_crashing(settings) -> None:
    daemon = ApartmentDaemon(
        settings,
        sources=[BrokenSource()],
        database=Database(settings.database_path),
    )
    assert daemon.cycle() == 0


class MockSource(SourceAdapter):
    name = "mock"

    def discover(self):
        return [viable_listing(platform="mock")]


def test_end_to_end_mock_source_ai_validator_sender_db(settings, config, monkeypatch) -> None:
    expected = successful_call(viable_listing(platform="mock"), config)
    expected.data.facts.contact_method = "email"
    expected.data.facts.contact_email = "owner@example.test"
    contacted = []

    def sender(outcome, _settings, database):
        contacted.append(outcome.database_id)
        database.record_contact(
            outcome.database_id,
            "dry_run",
            "platform",
            "dry_run_ready",
            detail="mocked sender",
        )
        return ContactResult(status="dry_run_ready", detail="mocked sender")

    monkeypatch.setattr("app.daemon.contact_listing", sender)
    database = Database(settings.database_path)
    daemon = ApartmentDaemon(
        settings,
        sources=[MockSource()],
        database=database,
        callers={"anthropic": lambda *_: expected},
    )
    assert daemon.cycle() == 1
    assert contacted
    assert database.status_counts() == {"dry_run_ready": 1}
    assert daemon.cycle() == 0
