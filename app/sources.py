from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .browser import _goto_with_retry, _launch_persistent, _page_requires_user_action
from .config_loader import Settings
from .database import Database
from .gmail_client import discover_gmail_alerts
from .schemas import SourceListing

logger = logging.getLogger("apartment_agent.sources")


class SourceAdapter(ABC):
    name: str
    min_interval_seconds: int = 0

    @abstractmethod
    def discover(self) -> list[SourceListing]:
        raise NotImplementedError


class InboxDirectorySource(SourceAdapter):
    name = "inbox_directory"

    def __init__(self, directory: Path):
        self.directory = directory

    def discover(self) -> list[SourceListing]:
        self.directory.mkdir(parents=True, exist_ok=True)
        listings: list[SourceListing] = []
        for path in sorted(self.directory.glob("*")):
            if path.suffix.casefold() == ".txt":
                listings.append(
                    SourceListing(
                        platform="unknown",
                        listing_id=path.stem,
                        title=path.stem.replace("_", " "),
                        raw_text=path.read_text(encoding="utf-8"),
                        source_metadata={"file": str(path)},
                    )
                )
            elif path.suffix.casefold() == ".json":
                payload = json.loads(path.read_text(encoding="utf-8"))
                listings.append(SourceListing.model_validate(payload))
        return listings


class GmailAlertSource(SourceAdapter):
    name = "gmail_alerts"

    def __init__(self, settings: Settings):
        self.settings = settings
        # Pinned explicitly so speeding up the daemon's outer tick (for near-real-time
        # WG watching) does not also increase Gmail API call frequency: this preserves
        # the same effective cadence Gmail discovery had before that change.
        self.min_interval_seconds = settings.poll_interval_seconds

    def discover(self) -> list[SourceListing]:
        return discover_gmail_alerts(self.settings)


WG_WATCH_NAV_ERROR_DELAY_SECONDS = 60.0
WG_WATCH_REPEATED_ERROR_DELAY_SECONDS = 120.0
WG_WATCH_SEVERE_ERROR_DELAY_SECONDS = 300.0
WG_WATCH_HUMAN_VERIFICATION_DELAY_SECONDS = 600.0
WG_WATCH_MAX_CARDS_PER_SEARCH = 30

# Stable, semantic structure observed on the real, logged-in WG-Gesucht search-results
# page: each result card is `<div id="liste-details-ad-<id>" data-id="<id>">` and
# contains a `<a class="detailansicht" href="...">` to the listing. Both the element id
# prefix and the `data-id`/class are WG-Gesucht's own stable hooks, not generated CSS.
WG_SEARCH_CARD_SELECTOR = "[id^='liste-details-ad-']"
WG_SEARCH_CARD_LINK_SELECTOR = "a.detailansicht"


class WGSearchWatcherSource(SourceAdapter):
    """Watches configured WG-Gesucht search-result pages for newly listed rooms/flats
    and feeds their URLs into the existing, unchanged production pipeline via
    `daemon.cycle()`. Uses the same persistent, already-logged-in browser profile as
    every other WG-Gesucht browser action; never attempts a separate login and never
    solves/bypasses CAPTCHA or human verification.
    """

    name = "wg_search_watcher"

    def __init__(
        self,
        settings: Settings,
        database: Database,
        searches: list[tuple[str, str]],
        *,
        min_seconds: float = 30.0,
        max_seconds: float = 45.0,
    ):
        self.settings = settings
        self.database = database
        self.searches = searches
        self.min_seconds = min_seconds
        self.max_seconds = max_seconds
        self._next_delay = 0.0
        self.last_run_report: list[dict[str, Any]] = []

    @property
    def min_interval_seconds(self) -> float:  # type: ignore[override]
        return self._next_delay

    def discover(self) -> list[SourceListing]:
        if not self.searches:
            self._next_delay = random.uniform(self.min_seconds, self.max_seconds)
            return []
        from playwright.sync_api import sync_playwright

        self.settings.browser_profile_path.mkdir(parents=True, exist_ok=True)
        cycle_started = time.monotonic()
        discovered: list[SourceListing] = []
        report: list[dict[str, Any]] = []
        worst_delay = random.uniform(self.min_seconds, self.max_seconds)
        try:
            with sync_playwright() as playwright:
                context = _launch_persistent(
                    playwright.chromium, self.settings, headless=self.settings.browser_headless
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(self.settings.browser_timeout_seconds * 1000)
                try:
                    for search_name, search_url in self.searches:
                        entry_report, entry_delay, new_for_search = self._check_search(
                            page, search_name, search_url, cycle_started
                        )
                        report.append(entry_report)
                        discovered.extend(new_for_search)
                        worst_delay = max(worst_delay, entry_delay)
                finally:
                    context.close()
        except Exception as exc:
            logger.exception(
                "WG search watcher cycle failed", extra={"fields": {"error": str(exc)[:300]}}
            )
            for search_name, _url in self.searches:
                self.database.watcher_record_check(
                    search_name,
                    status="backoff",
                    detail=f"browser error: {exc}"[:300],
                    next_check_at=self._next_check_at(WG_WATCH_SEVERE_ERROR_DELAY_SECONDS),
                )
            self._next_delay = WG_WATCH_SEVERE_ERROR_DELAY_SECONDS
            self.last_run_report = [
                {"search_name": name, "status": "backoff", "detail": "browser error"}
                for name, _ in self.searches
            ]
            return []
        self.last_run_report = report
        self._next_delay = worst_delay
        return discovered

    @staticmethod
    def _next_check_at(delay_seconds: float) -> str:
        return (datetime.now(UTC) + timedelta(seconds=delay_seconds)).isoformat()

    def _check_search(
        self, page: Any, search_name: str, search_url: str, cycle_started: float
    ) -> tuple[dict[str, Any], float, list[SourceListing]]:
        try:
            _goto_with_retry(page, search_url, self.settings.browser_timeout_seconds * 1000)
            page.wait_for_timeout(500)
        except Exception as exc:
            prior = self.database.watcher_get_search_state(search_name)
            prospective_errors = (int(prior["consecutive_errors"]) if prior else 0) + 1
            delay = self._backoff_delay(prospective_errors)
            self.database.watcher_record_check(
                search_name,
                status="backoff",
                detail=f"navigation failed: {exc}"[:300],
                next_check_at=self._next_check_at(delay),
            )
            return (
                {"search_name": search_name, "status": "backoff", "detail": str(exc)[:200]},
                delay,
                [],
            )

        user_action = _page_requires_user_action(page)
        if user_action:
            logger.warning(
                "WG search watcher paused: human verification required",
                extra={"fields": {"search_name": search_name, "detail": user_action}},
            )
            self.database.watcher_record_check(
                search_name,
                status="human_verification_required",
                detail=user_action,
                next_check_at=self._next_check_at(WG_WATCH_HUMAN_VERIFICATION_DELAY_SECONDS),
            )
            return (
                {
                    "search_name": search_name,
                    "status": "human_verification_required",
                    "detail": user_action,
                },
                WG_WATCH_HUMAN_VERIFICATION_DELAY_SECONDS,
                [],
            )

        cards = page.locator(WG_SEARCH_CARD_SELECTOR)
        visible_ids: list[str] = []
        entries: list[tuple[str, str, str]] = []
        for index in range(min(cards.count(), WG_WATCH_MAX_CARDS_PER_SEARCH)):
            card = cards.nth(index)
            listing_id = (card.get_attribute("data-id") or "").strip()
            if not listing_id:
                continue
            visible_ids.append(listing_id)
            link = card.locator(WG_SEARCH_CARD_LINK_SELECTOR).first
            href = link.get_attribute("href") if link.count() else None
            if not href:
                continue
            url = urljoin(search_url, href)
            title = (link.inner_text() or "").strip()
            entries.append((listing_id, url, title))

        state = self.database.watcher_get_search_state(search_name)
        if state is None or not state["baseline_done"]:
            baseline_ids = [listing_id for listing_id, _url, _title in entries]
            self.database.watcher_mark_baseline(search_name, baseline_ids)
            detail = f"baseline captured: {len(baseline_ids)} visible listings"
            delay = random.uniform(self.min_seconds, self.max_seconds)
            self.database.watcher_record_check(
                search_name, status="ok", detail=detail, next_check_at=self._next_check_at(delay)
            )
            logger.info(
                "WG search watcher baseline captured",
                extra={"fields": {"search_name": search_name, "count": len(baseline_ids)}},
            )
            return (
                {
                    "search_name": search_name,
                    "status": "ok",
                    "visible_ids": visible_ids,
                    "baseline": baseline_ids,
                    "seen": [],
                    "new": [],
                    "detail": detail,
                },
                delay,
                [],
            )

        new_listings: list[SourceListing] = []
        new_ids: list[str] = []
        seen_ids: list[str] = []
        for listing_id, url, title in entries:
            if self.database.watcher_claim_seen(listing_id, search_name):
                new_ids.append(listing_id)
                new_listings.append(
                    SourceListing(
                        platform="wg_gesucht",
                        listing_id=listing_id,
                        url=url,
                        title=(title[:200] or "WG-Gesucht listing"),
                        raw_text=title or "pending enrichment",
                        source_metadata={
                            "search_name": search_name,
                            "watcher_cycle_latency_ms": str(
                                round((time.monotonic() - cycle_started) * 1000)
                            ),
                        },
                    )
                )
            else:
                seen_ids.append(listing_id)
        if new_listings:
            logger.info(
                "WG search watcher found new listing(s)",
                extra={
                    "fields": {
                        "search_name": search_name,
                        "new_listing_ids": new_ids,
                        "latency_ms": round((time.monotonic() - cycle_started) * 1000),
                    }
                },
            )
        delay = random.uniform(self.min_seconds, self.max_seconds)
        self.database.watcher_record_check(
            search_name,
            status="ok",
            detail=f"{len(entries)} visible, {len(new_listings)} new",
            new_listing=bool(new_listings),
            next_check_at=self._next_check_at(delay),
        )
        return (
            {
                "search_name": search_name,
                "status": "ok",
                "visible_ids": visible_ids,
                "baseline": [],
                "seen": seen_ids,
                "new": new_ids,
                "detail": f"{len(entries)} visible, {len(new_listings)} new",
            },
            delay,
            new_listings,
        )

    @staticmethod
    def _backoff_delay(consecutive_errors: int) -> float:
        if consecutive_errors <= 1:
            return WG_WATCH_NAV_ERROR_DELAY_SECONDS
        if consecutive_errors <= 3:
            return WG_WATCH_REPEATED_ERROR_DELAY_SECONDS
        return WG_WATCH_SEVERE_ERROR_DELAY_SECONDS


class KleinanzeigenBrowserSearchSource(SourceAdapter):
    name = "kleinanzeigen_browser_search"
    min_interval_seconds = 300

    def __init__(self, settings: Settings, search_url: str):
        self.settings = settings
        self.search_url = search_url

    def discover(self) -> list[SourceListing]:
        from playwright.sync_api import sync_playwright

        self.settings.browser_profile_path.mkdir(parents=True, exist_ok=True)
        listings: list[SourceListing] = []
        with sync_playwright() as playwright:
            context = _launch_persistent(
                playwright.chromium, self.settings, headless=self.settings.browser_headless
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                _goto_with_retry(
                    page,
                    self.search_url,
                    self.settings.browser_timeout_seconds * 1000,
                )
                for anchor in page.locator("a[href*='/s-anzeige/']").all()[:25]:
                    href = anchor.get_attribute("href") or ""
                    url = urljoin(self.search_url, href)
                    id_match = re.search(r"/(\d{6,})(?:-|/|$)", href)
                    title = (anchor.inner_text() or "Kleinanzeigen listing").strip()
                    listings.append(
                        SourceListing(
                            platform="kleinanzeigen",
                            listing_id=id_match.group(1) if id_match else url,
                            url=url,
                            title=title[:200],
                            raw_text=title,
                        )
                    )
            finally:
                context.close()
        return list({listing.url: listing for listing in listings}.values())


class PortalPageSource(SourceAdapter):
    min_interval_seconds = 900

    def __init__(self, name: str, platform: str, url: str, link_pattern: str):
        self.name = name
        self.platform = platform
        self.url = url
        self.link_pattern = re.compile(link_pattern, re.I)

    def discover(self) -> list[SourceListing]:
        response = httpx.get(
            self.url,
            follow_redirects=True,
            timeout=20,
            headers={"User-Agent": "MunsterApartmentAgent/1.0 (+personal low-frequency checker)"},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        listings: list[SourceListing] = []
        if self.platform == "na_dann":
            for card in soup.select("div.card[id^='kla-']")[:25]:
                identifier = str(card.get("id", "")).removeprefix("kla-")
                raw = card.get_text(" ", strip=True)
                contact = card.select_one("a[href*='kleinanzeige-kontakt']")
                if not identifier or not raw or contact is None:
                    continue
                email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw)
                listings.append(
                    SourceListing(
                        platform="na_dann",
                        listing_id=identifier,
                        url=urljoin(self.url, str(contact.get("href", ""))),
                        title=raw[:120],
                        raw_text=raw,
                        contact_email=email_match.group(0) if email_match else None,
                    )
                )
            return listings
        seen: set[str] = set()
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", ""))
            if not self.link_pattern.search(href):
                continue
            url = urljoin(self.url, href)
            if url in seen:
                continue
            seen.add(url)
            container = anchor.find_parent(["article", "li", "div"])
            raw = (container or anchor).get_text(" ", strip=True)
            title = anchor.get_text(" ", strip=True) or raw[:120]
            if self.platform == "asta_muenster":
                try:
                    detail_response = httpx.get(
                        url,
                        follow_redirects=True,
                        timeout=20,
                        headers={
                            "User-Agent": (
                                "MunsterApartmentAgent/1.0 (+personal low-frequency checker)"
                            )
                        },
                    )
                    detail_response.raise_for_status()
                    detail = BeautifulSoup(detail_response.text, "html.parser")
                    raw = (detail.select_one("main") or detail).get_text(" ", strip=True)
                except httpx.HTTPError:
                    pass
            email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", raw)
            id_match = re.search(r"(\d{4,})", href)
            listings.append(
                SourceListing(
                    platform=self.platform,  # type: ignore[arg-type]
                    listing_id=id_match.group(1) if id_match else url,
                    url=url,
                    title=title,
                    raw_text=raw,
                    contact_email=email_match.group(0) if email_match else None,
                )
            )
            if len(listings) >= 25:
                break
        return listings


def configured_wg_searches(config: dict[str, Any]) -> list[tuple[str, str]]:
    """Named (search_name, url) pairs to watch. Prefers the YAML list under
    `wg_watch.searches` in config.yaml (supports multiple named searches copied
    directly from the user's own browser); falls back to the single legacy
    `WG_SEARCH_URL` env var, kept for backward compatibility."""
    wg_watch = config.get("wg_watch") or {}
    configured = wg_watch.get("searches") or []
    searches: list[tuple[str, str]] = []
    for entry in configured:
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        if name and url:
            searches.append((name, url))
    if searches:
        return searches
    legacy_url = os.getenv(
        "WG_SEARCH_URL", "https://www.wg-gesucht.de/wg-zimmer-in-Muenster.91.0.1.0.html"
    ).strip()
    return [("wg_search_default", legacy_url)] if legacy_url else []


def configured_sources(
    settings: Settings, database: Database, config: dict[str, Any]
) -> list[SourceAdapter]:
    sources: list[SourceAdapter] = [InboxDirectorySource(settings.input_directory)]
    if settings.gmail_enabled:
        sources.append(GmailAlertSource(settings))
    wg_browser_enabled = os.getenv("ENABLE_WG_BROWSER", "true").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if wg_browser_enabled:
        searches = configured_wg_searches(config)
        if searches:
            wg_watch_config = config.get("wg_watch") or {}
            min_seconds = float(
                os.getenv(
                    "WG_WATCH_MIN_SECONDS", str(wg_watch_config.get("min_interval_seconds", 30))
                )
            )
            max_seconds = float(
                os.getenv(
                    "WG_WATCH_MAX_SECONDS", str(wg_watch_config.get("max_interval_seconds", 45))
                )
            )
            max_seconds = max(max_seconds, min_seconds)
            sources.append(
                WGSearchWatcherSource(
                    settings,
                    database,
                    searches,
                    min_seconds=min_seconds,
                    max_seconds=max_seconds,
                )
            )
    kleinanzeigen_url = os.getenv("KLEINANZEIGEN_SEARCH_URL", "").strip()
    if kleinanzeigen_url:
        sources.append(KleinanzeigenBrowserSearchSource(settings, kleinanzeigen_url))
    # WG-Gesucht is the active source for now; AStA and na dann are deliberately
    # disabled by default (not deleted) so they can be turned back on later by setting
    # ENABLE_ASTA / ENABLE_NA_DANN to true.
    if os.getenv("ENABLE_ASTA", "false").casefold() in {"1", "true", "yes", "on"}:
        sources.append(
            PortalPageSource(
                "asta_muenster",
                "asta_muenster",
                os.getenv("ASTA_LIST_URL", "https://asta.ms/wohnboerse"),
                r"/wohnboerse/(?!.*view=form).*\d+",
            )
        )
    if os.getenv("ENABLE_NA_DANN", "false").casefold() in {"1", "true", "yes", "on"}:
        sources.append(
            PortalPageSource(
                "na_dann",
                "na_dann",
                os.getenv(
                    "NA_DANN_LIST_URL",
                    "https://www.nadann.de/rubriken/kleinanzeigen/biete-wohnen/",
                ),
                r"(?:kleinanzeigen|wohnen|anzeige).*(?:\d|detail)",
            )
        )
    return sources
