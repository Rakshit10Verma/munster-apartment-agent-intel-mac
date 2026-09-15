from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .browser import _goto_with_retry, _launch_persistent
from .config_loader import Settings
from .gmail_client import discover_gmail_alerts
from .schemas import SourceListing


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

    def discover(self) -> list[SourceListing]:
        return discover_gmail_alerts(self.settings)


class WGBrowserSearchSource(SourceAdapter):
    name = "wg_browser_search"
    min_interval_seconds = 120

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
                for anchor in page.locator("a[href*='.html']").all():
                    href = anchor.get_attribute("href") or ""
                    if not re.search(r"wg-zimmer|wohnungen|angebote", href, re.I):
                        continue
                    url = urljoin(self.search_url, href)
                    identifier = re.search(r"(?:\.|/)(\d{5,})\.html", url)
                    if identifier is None:
                        continue
                    title = (anchor.inner_text() or "WG-Gesucht listing").strip()
                    listings.append(
                        SourceListing(
                            platform="wg_gesucht",
                            listing_id=identifier.group(1),
                            url=url,
                            title=title[:200],
                            raw_text=title,
                        )
                    )
                    if len(listings) >= 25:
                        break
            finally:
                context.close()
        unique = {listing.url: listing for listing in listings}
        return list(unique.values())


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


def configured_sources(settings: Settings) -> list[SourceAdapter]:
    sources: list[SourceAdapter] = [InboxDirectorySource(settings.input_directory)]
    if settings.gmail_enabled:
        sources.append(GmailAlertSource(settings))
    wg_browser_enabled = os.getenv("ENABLE_WG_BROWSER", "true").casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    wg_search_url = os.getenv(
        "WG_SEARCH_URL", "https://www.wg-gesucht.de/wg-zimmer-in-Muenster.91.0.1.0.html"
    ).strip()
    if wg_browser_enabled and wg_search_url:
        sources.append(WGBrowserSearchSource(settings, wg_search_url))
    kleinanzeigen_url = os.getenv("KLEINANZEIGEN_SEARCH_URL", "").strip()
    if kleinanzeigen_url:
        sources.append(KleinanzeigenBrowserSearchSource(settings, kleinanzeigen_url))
    if os.getenv("ENABLE_ASTA", "true").casefold() in {"1", "true", "yes", "on"}:
        sources.append(
            PortalPageSource(
                "asta_muenster",
                "asta_muenster",
                os.getenv("ASTA_LIST_URL", "https://asta.ms/wohnboerse"),
                r"/wohnboerse/(?!.*view=form).*\d+",
            )
        )
    if os.getenv("ENABLE_NA_DANN", "true").casefold() in {"1", "true", "yes", "on"}:
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
