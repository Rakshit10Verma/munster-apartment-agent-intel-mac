from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.cli import _watch_once
from app.daemon import ApartmentDaemon
from app.database import Database
from app.sources import WGSearchWatcherSource, configured_sources


def _write_search_fixture(path: Path, listing_ids: list[str]) -> None:
    """A minimal reconstruction of the real, logged-in WG-Gesucht search-results DOM
    (verified live): each result card is `<div id="liste-details-ad-<id>" data-id="<id>">`
    containing `<a class="detailansicht" href="...">`."""
    cards = "\n".join(
        f'<div id="liste-details-ad-{listing_id}" class="wgg_card offer_list_item" '
        f'data-id="{listing_id}">'
        f'<a class="detailansicht" href="/wg-zimmer-in-Muenster-Test.{listing_id}.html">'
        f"<b>Test listing {listing_id}</b></a>"
        f"</div>"
        for listing_id in listing_ids
    )
    path.write_text(f"<!doctype html><html><body>{cards}</body></html>", encoding="utf-8")


def _watcher(settings, database: Database, *searches: tuple[str, str]) -> WGSearchWatcherSource:
    return WGSearchWatcherSource(settings, database, list(searches), min_seconds=1, max_seconds=2)


# ---- baseline and dedup behavior --------------------------------------------------


def test_first_run_creates_baseline_and_enqueues_nothing(settings, tmp_path) -> None:
    html = tmp_path / "search.html"
    _write_search_fixture(html, ["100001", "100002", "100003"])
    database = Database(settings.database_path)
    watcher = _watcher(settings, database, ("test_search", html.as_uri()))

    new_listings = watcher.discover()

    assert new_listings == []
    state = database.watcher_get_search_state("test_search")
    assert state is not None
    assert state["baseline_done"] == 1
    assert watcher.last_run_report[0]["baseline"] == ["100001", "100002", "100003"]


def test_new_listing_after_baseline_is_enqueued_once(settings, tmp_path) -> None:
    html = tmp_path / "search.html"
    _write_search_fixture(html, ["100001", "100002"])
    database = Database(settings.database_path)
    watcher = _watcher(settings, database, ("test_search", html.as_uri()))
    watcher.discover()

    _write_search_fixture(html, ["100003", "100001", "100002"])
    new_listings = watcher.discover()

    assert [listing.listing_id for listing in new_listings] == ["100003"]
    assert new_listings[0].platform == "wg_gesucht"
    assert new_listings[0].url.endswith("wg-zimmer-in-Muenster-Test.100003.html")


def test_same_new_listing_in_two_searches_is_enqueued_once(settings, tmp_path) -> None:
    html_a = tmp_path / "search_a.html"
    html_b = tmp_path / "search_b.html"
    _write_search_fixture(html_a, ["200001"])
    _write_search_fixture(html_b, ["200001"])
    database = Database(settings.database_path)
    watcher = _watcher(
        settings, database, ("search_a", html_a.as_uri()), ("search_b", html_b.as_uri())
    )
    watcher.discover()  # baseline for both

    _write_search_fixture(html_a, ["200002", "200001"])
    _write_search_fixture(html_b, ["200002", "200001"])
    new_listings = watcher.discover()

    assert [listing.listing_id for listing in new_listings] == ["200002"]


def test_reordered_listing_is_not_reenqueued(settings, tmp_path) -> None:
    html = tmp_path / "search.html"
    _write_search_fixture(html, ["300001", "300002"])
    database = Database(settings.database_path)
    watcher = _watcher(settings, database, ("test_search", html.as_uri()))
    watcher.discover()

    _write_search_fixture(html, ["300003", "300001", "300002"])
    first = watcher.discover()
    assert [listing.listing_id for listing in first] == ["300003"]

    _write_search_fixture(html, ["300002", "300003", "300001"])  # same set, reordered
    second = watcher.discover()
    assert second == []


def test_previously_claimed_listing_never_reenqueued(settings, tmp_path) -> None:
    """A listing already contacted or skipped by the pipeline must not resurface just
    because it moves back to the top of the search results."""
    html = tmp_path / "search.html"
    _write_search_fixture(html, [])
    database = Database(settings.database_path)
    watcher = _watcher(settings, database, ("test_search", html.as_uri()))
    watcher.discover()  # empty baseline
    assert database.watcher_claim_seen("400001", "test_search")

    _write_search_fixture(html, ["400001"])
    new_listings = watcher.discover()

    assert new_listings == []


def test_multiple_new_listings_in_one_cycle(settings, tmp_path) -> None:
    html = tmp_path / "search.html"
    _write_search_fixture(html, ["500001"])
    database = Database(settings.database_path)
    watcher = _watcher(settings, database, ("test_search", html.as_uri()))
    watcher.discover()

    _write_search_fixture(html, ["500004", "500003", "500002", "500001"])
    new_listings = watcher.discover()

    assert sorted(listing.listing_id for listing in new_listings) == [
        "500002",
        "500003",
        "500004",
    ]


def test_watcher_claim_seen_is_race_safe(settings) -> None:
    database = Database(settings.database_path)
    results = [database.watcher_claim_seen("600001", "search_a") for _ in range(5)]
    assert results.count(True) == 1
    assert results.count(False) == 4


# ---- human verification / backoff -------------------------------------------------


def test_captcha_detected_pauses_the_search(settings, tmp_path) -> None:
    html = tmp_path / "search.html"
    html.write_text(
        "<!doctype html><html><body><main>Bitte löse das Captcha, um fortzufahren."
        "</main></body></html>",
        encoding="utf-8",
    )
    database = Database(settings.database_path)
    watcher = _watcher(settings, database, ("test_search", html.as_uri()))

    new_listings = watcher.discover()

    assert new_listings == []
    state = database.watcher_get_search_state("test_search")
    assert state is not None
    assert state["status"] == "human_verification_required"
    assert watcher.min_interval_seconds >= 600


def test_session_expired_marks_human_verification_required(settings, tmp_path) -> None:
    html = tmp_path / "anmelden.html"
    html.write_text("<!doctype html><html><body>Login required</body></html>", encoding="utf-8")
    database = Database(settings.database_path)
    watcher = _watcher(settings, database, ("test_search", html.as_uri()))

    watcher.discover()

    state = database.watcher_get_search_state("test_search")
    assert state is not None
    assert state["status"] == "human_verification_required"


def test_recovery_after_session_restored(settings, tmp_path) -> None:
    login_page = tmp_path / "anmelden.html"
    login_page.write_text(
        "<!doctype html><html><body>Login required</body></html>", encoding="utf-8"
    )
    search_page = tmp_path / "search.html"
    _write_search_fixture(search_page, ["700001"])

    database = Database(settings.database_path)
    watcher = _watcher(settings, database, ("test_search", login_page.as_uri()))
    watcher.discover()
    assert database.watcher_get_search_state("test_search")["status"] == (
        "human_verification_required"
    )

    # User resolves the login manually; the next check reaches the real search page.
    watcher.searches = [("test_search", search_page.as_uri())]
    new_listings = watcher.discover()

    state = database.watcher_get_search_state("test_search")
    assert state["status"] == "ok"
    # Nothing was missed while paused: the first successful check safely establishes
    # baseline rather than guessing at what might have appeared during the pause.
    assert new_listings == []
    assert state["baseline_done"] == 1


def test_navigation_failure_triggers_backoff(settings) -> None:
    database = Database(settings.database_path)
    watcher = _watcher(
        settings,
        database,
        ("test_search", "https://this-domain-should-not-resolve.invalid/search.html"),
    )

    new_listings = watcher.discover()

    assert new_listings == []
    state = database.watcher_get_search_state("test_search")
    assert state is not None
    assert state["status"] == "backoff"
    assert watcher.min_interval_seconds >= 60


def test_repeated_errors_escalate_backoff(settings) -> None:
    database = Database(settings.database_path)
    bad_url = "https://this-domain-should-not-resolve.invalid/search.html"
    watcher = _watcher(settings, database, ("test_search", bad_url))

    watcher.discover()
    first_delay = watcher.min_interval_seconds
    watcher.discover()
    watcher.discover()
    later_delay = watcher.min_interval_seconds

    assert later_delay >= first_delay


# ---- daemon/config integration -----------------------------------------------------


def test_watcher_included_when_gmail_disabled(settings) -> None:
    config = {"wg_watch": {"searches": [{"name": "test", "url": "https://example.test/x.html"}]}}
    database = Database(settings.database_path)
    disabled_gmail_settings = replace(settings, gmail_enabled=False)

    sources = configured_sources(disabled_gmail_settings, database, config)

    names = [source.name for source in sources]
    assert "wg_search_watcher" in names
    assert "gmail_alerts" not in names


def test_daemon_cycle_enqueues_watcher_listing_into_existing_pipeline(settings, tmp_path) -> None:
    """End-to-end wiring: the watcher discovers via the daemon's normal cycle() call,
    and the existing, unmodified pipeline (process_listing) picks it up automatically
    in the same cycle -- no separate sender process."""
    html = tmp_path / "search.html"
    _write_search_fixture(html, [])
    database = Database(settings.database_path)
    watcher = _watcher(settings, database, ("test_search", html.as_uri()))
    daemon = ApartmentDaemon(settings, sources=[watcher], database=database, callers={})

    baseline_processed = daemon.cycle()
    assert baseline_processed == 0

    _write_search_fixture(html, ["900001"])
    processed = daemon.cycle()

    assert processed == 1
    assert sum(database.status_counts().values()) == 1


def test_watch_once_never_calls_the_pipeline(settings, tmp_path, monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("watch-once must never analyze or contact a listing")

    monkeypatch.setattr("app.cli.process_listing", fail)
    monkeypatch.setattr("app.cli.contact_listing", fail)
    html = tmp_path / "search.html"
    _write_search_fixture(html, ["800001"])
    config = {"wg_watch": {"searches": [{"name": "test_search", "url": html.as_uri()}]}}
    database = Database(settings.database_path)

    code = _watch_once(settings, database, config)

    assert code == 0
