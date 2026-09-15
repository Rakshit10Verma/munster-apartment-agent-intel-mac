from __future__ import annotations

import httpx

from app.sources import PortalPageSource


def test_na_dann_cards_become_contactable_listings(monkeypatch) -> None:
    html = """
    <div class="card" id="kla-851026"><div class="card-block">
      <p>WG-Zimmer 450 € warm, mindestens 12 Monate. mail@example.test</p>
      <a href="/kleinanzeige-kontakt?uid=851026">Kontakt</a>
    </div></div>
    """
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            text=html,
            request=httpx.Request("GET", "https://www.nadann.de/example"),
        ),
    )
    source = PortalPageSource("na_dann", "na_dann", "https://www.nadann.de/list", r"x")
    listings = source.discover()
    assert len(listings) == 1
    assert listings[0].listing_id == "851026"
    assert listings[0].contact_email == "mail@example.test"
    assert "kleinanzeige-kontakt" in listings[0].url
