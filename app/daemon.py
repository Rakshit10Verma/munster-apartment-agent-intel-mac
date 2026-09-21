from __future__ import annotations

import logging
import signal
import time

from .analyzer import process_listing
from .browser import fetch_full_listing, fetch_wg_listing_snapshot
from .config_loader import Settings, load_all
from .contact import contact_listing
from .database import Database
from .providers import ProviderCaller
from .schemas import SourceListing
from .sources import SourceAdapter, configured_sources
from .telegram_notify import (
    check_watcher_health,
    notify_listing_if_changed,
    record_discovery_outcome,
)

logger = logging.getLogger("apartment_agent.daemon")

# The outer loop ticks this often regardless of `poll_interval_seconds`, so a fast
# source (e.g. the WG search watcher, checked every ~30-45s) is not held back by a
# slower global cadence. Each source's own `min_interval_seconds` still gates how
# often it actually does work; sources without an explicit interval (e.g. Gmail) pin
# theirs to `poll_interval_seconds` so this does not silently speed them up too.
DAEMON_TICK_SECONDS = 5


class ApartmentDaemon:
    def __init__(
        self,
        settings: Settings,
        sources: list[SourceAdapter] | None = None,
        database: Database | None = None,
        callers: dict[str, ProviderCaller] | None = None,
    ):
        self.settings = settings
        self.config, self.answers, _ = load_all()
        self.database = database or Database(settings.database_path)
        self.sources = sources or configured_sources(settings, self.database, self.config)
        self.callers = callers
        self.running = True
        self.last_source_run: dict[str, float] = {}

    def stop(self, *_args: object) -> None:
        self.running = False

    def _enrich_browser_listing(self, listing: SourceListing) -> SourceListing:
        if listing.platform not in {"wg_gesucht", "kleinanzeigen"} or not listing.url:
            return listing
        try:
            if listing.platform == "wg_gesucht":
                full_text, inspection = fetch_wg_listing_snapshot(listing.url, self.settings)
                metadata = dict(listing.source_metadata)
                metadata.update(
                    {
                        "wg_contact_state": inspection.state,
                        "wg_contact_state_detail": inspection.detail,
                    }
                )
                return listing.model_copy(
                    update={"raw_text": full_text, "source_metadata": metadata}
                )
            full_text = fetch_full_listing(listing.url, self.settings)
            return listing.model_copy(update={"raw_text": full_text})
        except Exception as exc:
            logger.warning(
                "browser listing enrichment failed; preserving discovery text",
                extra={"fields": {"listing_id": listing.listing_id, "error": str(exc)[:300]}},
            )
            return listing

    def cycle(self) -> int:
        processed = 0
        for source in self.sources:
            now = time.monotonic()
            last = self.last_source_run.get(source.name, 0.0)
            if now - last < source.min_interval_seconds:
                continue
            self.last_source_run[source.name] = now
            try:
                discovery_started = time.monotonic()
                discovered = source.discover()
                logger.info(
                    "source discovery completed",
                    extra={
                        "fields": {
                            "source": source.name,
                            "listing_count": len(discovered),
                            "latency_ms": round((time.monotonic() - discovery_started) * 1000),
                        }
                    },
                )
            except Exception as exc:
                logger.exception(
                    "source discovery failed",
                    extra={"fields": {"source": source.name, "error": str(exc)[:300]}},
                )
                record_discovery_outcome(
                    self.database, self.settings, source.name, failed=True, detail=str(exc)
                )
                continue
            record_discovery_outcome(self.database, self.settings, source.name, failed=False)
            for listing in discovered:
                if self.database.contains(listing):
                    continue
                listing = self._enrich_browser_listing(listing)
                try:
                    outcome = process_listing(
                        listing,
                        config=self.config,
                        answers=self.answers,
                        settings=self.settings,
                        database=self.database,
                        callers=self.callers,
                    )
                    processed += 1
                    if outcome.status == "drafted" and (
                        listing.platform in {"wg_gesucht", "kleinanzeigen", "na_dann"}
                        or outcome.facts.contact_method in {"email", "website"}
                    ):
                        contact_listing(outcome, self.settings, self.database, "auto")
                    notify_listing_if_changed(self.database, self.settings, outcome.database_id)
                except Exception as exc:
                    logger.exception(
                        "listing processing failed",
                        extra={
                            "fields": {"listing_id": listing.listing_id, "error": str(exc)[:300]}
                        },
                    )
        check_watcher_health(self.database, self.settings)
        return processed

    def run(self, once: bool = False) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        while self.running:
            self.cycle()
            if once:
                return
            time.sleep(DAEMON_TICK_SECONDS)
