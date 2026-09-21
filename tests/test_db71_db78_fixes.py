from __future__ import annotations

from dataclasses import replace

import pytest

from app.analyzer import process_listing, reconcile_facts
from app.database import Database
from app.message_policy import is_formal_application_context
from app.prefilter import deterministic_prefilter, requirement_stage
from app.providers import ProviderUnavailable
from app.review import reprocess_listing
from app.rules import apply_rules, zwischenmiete_duration_months
from app.schemas import AIAnalysis, ListingFacts, MessageDraft, SourceListing
from tests.conftest import viable_listing

# Real DB 71 text (WG-Zimmer am Cheruskerring, Marc/Julius/Dunja), reproduced verbatim
# where it matters for these regressions.
DB71_TEXT = """Zimmergröße : 16m²
Gesamtmiete : 450€
Kosten
Miete:
300€
Nebenkosten:
150€
Sonstige Kosten:
n.a.
Kaution:
777€
Ablösevereinbarung:
n.a.
SCHUFA-Auskunft:
In 3 Minuten bereit 1

Moin ich bin Marc! Zum 01.10. werde ich nach langer Zeit aus meiner lieben WG ausziehen.
Wenn du Möbel von mir übernehmen möchtest, können wir da gerne drüber reden :)

Du würdest mit Julius und Dunja zusammenwohnen. Unsere WG ist locker und entspannt.

Wenn du Interesse hast, schreib uns gerne und erzähl ein bisschen was über dich und
beginne deine Nachricht mit dem Wort Sonnenblume.

WG-Details
Die WG:
16m² Zimmer in 3er WG
3er WG ( 1 Frau und 1 Mann )

Sonstiges
Noch ein kleiner Hinweis: Vom Vermieter wird zum Mitvertrag 1. eine Schufa (jetzt
beantragen) -Auskunft, 2. ein Nachweis über das Bestehen einer privaten
Haftpflichtversicherung und 3. eine Elternbürgschaft oder ein Einkommensnachweis
über drei Gehälter gefordert.
Die Miete in Höhe von 450€ ist inklusive Strom, Gas, Internet, etc."""

# Real DB 78 text (Zwischenmiete WG-Zimmer, Lenny/Jule/Niklas).
DB78_TEXT = """Gesamtmiete : 550€
Kosten
Miete:
550€
Kaution:
n.a.
Ablösevereinbarung:
n.a.
Adresse
Edith-Stein-Straße 4
Verfügbarkeit
frei ab:
01.10.2026
frei bis:
31.03.2027

Hi, ich (Lenny) suche nach einer Zwischenmiete für mein WG-Zimmer ab dem 01.10.
Das Zimmer ist ca. 20 m^2 groß.

Ich würde außerdem einige Möbel hier lassen, da mein Zimmer in Köln schon möbliert
ist. Was genau drin bleibt, muss ich ehrlich gesagt selber nochmal gucken, aber da
könnten wir dann im Casting/danach drüber quatschen.

Deine Mitbewohner wären Jule (22) und Niklas (22).

Ich würde gerne eine Kaution + einen Abschlag von dir als Absicherung für meine
ganzen Möbel (was genau drin bleibt, können wir dann klären). Du würdest auch mit
der Hausverwaltung abgesprochen einen eigenen Untermietvertrag bekommen, sodass
du auch ne Absicherung hättest."""


def _listing(listing_id: str, text: str, title: str = "") -> SourceListing:
    return SourceListing(
        platform="wg_gesucht",
        listing_id=listing_id,
        url=f"https://www.wg-gesucht.de/zimmer.{listing_id}.html",
        title=title,
        raw_text=text,
    )


def _outage(listing, settings, config, answers):
    def fail(*_args):
        raise ProviderUnavailable("all providers unavailable")

    return process_listing(
        listing,
        settings=replace(settings, provider_order=("anthropic",)),
        config=config,
        answers=answers,
        callers={"anthropic": fail},
    )


# ---- DB 71 bug 1: €777 deposit leaking into furniture_takeover_eur ----------------


def test_deposit_777_does_not_leak_into_furniture_takeover(config: dict) -> None:
    listing = _listing("db71-1", DB71_TEXT)
    result = deterministic_prefilter(listing, config)
    assert result.facts.deposit_eur == 777
    assert result.facts.furniture_takeover_eur is None
    assert result.facts.one_time_fee_eur is None
    assert result.facts.scam_risk == "low"


def test_explicit_ablosevereinbarung_na_does_not_block_fallback(settings, config, answers) -> None:
    outcome = _outage(_listing("db71-1b", DB71_TEXT), settings, config, answers)
    assert outcome.status == "drafted", (outcome.validation.errors, outcome.router_notes)
    assert outcome.facts.furniture_takeover_eur is None


# ---- DB 71 bug 2: requirement stage (contact vs. contract) -------------------------


def test_zum_mietvertrag_is_classified_contract_stage(config: dict) -> None:
    listing = _listing("stage-1", DB71_TEXT)
    text = f"{listing.title}\n{listing.raw_text}".casefold()
    assert requirement_stage(text) == "contract"
    result = deterministic_prefilter(listing, config)
    assert set(result.facts.later_contract_requirements) == {
        "SCHUFA",
        "private Haftpflichtversicherung",
        "Elternbürgschaft",
        "Einkommensnachweis",
    }


def test_document_requested_with_application_is_contact_stage(config: dict) -> None:
    listing = _listing(
        "stage-2",
        "WG-Zimmer, 490 € warm, mindestens 12 Monate. "
        "Bitte schick die SCHUFA direkt mit deiner Bewerbung mit.",
    )
    text = f"{listing.title}\n{listing.raw_text}".casefold()
    assert requirement_stage(text) == "initial_contact"
    result = deterministic_prefilter(listing, config)
    assert result.facts.later_contract_requirements == []


def test_unclear_requirement_timing_stays_unclear(config: dict) -> None:
    listing = _listing(
        "stage-3",
        "WG-Zimmer, 490 € warm, mindestens 12 Monate. Es wird eine SCHUFA benötigt.",
    )
    text = f"{listing.title}\n{listing.raw_text}".casefold()
    assert requirement_stage(text) == "unclear"
    result = deterministic_prefilter(listing, config)
    assert result.facts.later_contract_requirements == []


def test_contract_stage_document_ambiguity_does_not_block_apply(settings, config, answers) -> None:
    """An AI note escalating a contract-stage document into a pre-contact blocker
    must be stripped; the requirement is still shown, just informationally."""
    listing = _listing("stage-4", DB71_TEXT)
    prefilter = deterministic_prefilter(listing, config)
    ai_facts = ListingFacts(
        advertiser_type="wg",
        housing_type="wg_room",
        warm_rent_eur=450,
        buergschaft_required=True,
        critical_ambiguities=[
            "Unklar ist, ob Rakshit eine private Haftpflichtversicherung nachweisen kann."
        ],
        unresolved_required_facts=[
            "Vor dem Erstkontakt muss geklärt werden, ob eine Elternbürgschaft vorliegt."
        ],
        confidence=0.9,
    )
    ai = AIAnalysis(
        facts=ai_facts,
        decision_recommendation="REVIEW",
        message=MessageDraft(body="x", language="de", address_register="du"),
    )
    facts, _remap = reconcile_facts(prefilter, ai, config)
    assert facts.critical_ambiguities == []
    assert facts.unresolved_required_facts == []
    assert "SCHUFA" in facts.later_contract_requirements
    decision = apply_rules(facts, config, prefilter)
    assert decision.decision == "APPLY"


# ---- DB 71 complete regression ------------------------------------------------------


def test_db71_complete_regression_applies_with_sonnenblume(settings, config, answers) -> None:
    outcome = _outage(
        _listing("db71-full", DB71_TEXT, "WG-Zimmer am Ring"), settings, config, answers
    )
    assert outcome.status == "drafted", (outcome.validation.errors, outcome.router_notes)
    assert outcome.facts.advertiser_type == "wg"
    assert outcome.facts.scam_risk == "low"
    assert outcome.facts.deposit_eur == 777
    assert outcome.facts.furniture_takeover_eur is None
    assert set(outcome.facts.later_contract_requirements) >= {"SCHUFA", "Elternbürgschaft"}
    assert outcome.message is not None
    assert outcome.message.body.strip().split()[0] == "Sonnenblume"
    assert is_formal_application_context(outcome.listing, outcome.facts) is False


# ---- DB 78 bug 3: Zwischenmiete duration from exact dates --------------------------


def test_db78_exact_dates_pass_zwischenmiete_rule(config: dict) -> None:
    facts = ListingFacts(
        housing_type="zwischenmiete", move_in="2026-10-01", end_date="2027-03-31", warm_rent_eur=550
    )
    assert zwischenmiete_duration_months(facts) == 6
    prefilter_like = deterministic_prefilter(_listing("db78-dates", DB78_TEXT), config)
    decision = apply_rules(prefilter_like.facts, config, prefilter_like)
    assert decision.decision != "SKIP"


# ---- DB 78 bug 1: normal furniture Abschlag vs. real scam signals -----------------


def test_db78_negotiable_furniture_abschlag_does_not_block_fallback(
    settings, config, answers
) -> None:
    outcome = _outage(
        _listing("db78-1", DB78_TEXT, "tolle WG mit Balkon"), settings, config, answers
    )
    assert outcome.status == "drafted", (outcome.validation.errors, outcome.router_notes)
    assert outcome.facts.scam_risk == "low"


def test_ai_independently_flagging_unpriced_fee_is_normalized(settings, config, answers) -> None:
    """Regression for DB 78: even when the AI (not just deterministic extraction)
    independently judges an unpriced Kaution/Abschlag as suspicious, that specific
    misjudgment must be normalized away without weakening real scam detection."""
    listing = _listing("db78-2", DB78_TEXT, "tolle WG mit Balkon")
    prefilter = deterministic_prefilter(listing, config)
    ai_facts = ListingFacts(
        advertiser_type="unknown",
        housing_type="zwischenmiete",
        warm_rent_eur=550,
        move_in="2026-10-01",
        end_date="2027-03-31",
        scam_risk="medium",
        scam_reasons=[
            "Die Höhe der verlangten Kaution ist nicht angegeben.",
            "Zusätzlich wird ein Abschlag für Möbel verlangt, dessen Höhe noch nicht feststeht.",
        ],
        odd_fee_or_payment_demands=["Die verlangte Kaution ist ebenfalls nicht beziffert."],
        critical_ambiguities=[
            "Es ist zu klären, ob Rakshit die auf sechs Monate begrenzte Zwischenmiete "
            "wahrheitsgemäß akzeptieren kann, da er längerfristig sucht."
        ],
        confidence=0.88,
    )
    ai = AIAnalysis(
        facts=ai_facts,
        decision_recommendation="REVIEW",
        message=MessageDraft(body="x", language="de", address_register="du"),
    )
    facts, _remap = reconcile_facts(prefilter, ai, config)
    assert facts.scam_risk == "low"
    assert facts.scam_reasons == []
    assert facts.odd_fee_or_payment_demands == []
    assert facts.critical_ambiguities == []
    decision = apply_rules(facts, config, prefilter)
    assert decision.decision == "APPLY"


def test_real_advance_payment_scam_still_blocked(settings, config, answers) -> None:
    """Must never weaken genuine scam/payment-risk detection."""
    listing = _listing(
        "db78-scam",
        "WG-Zimmer, 490 € warm, mindestens 6 Monate. "
        "Bitte die Zahlung von 300 € Kaution vor der Besichtigung per Western Union.",
    )
    result = deterministic_prefilter(listing, config)
    assert result.facts.scam_risk == "high"
    decision = apply_rules(result.facts, config, result)
    assert decision.decision == "SKIP"


def test_ai_cannot_normalize_away_real_scam_signal(settings, config, answers) -> None:
    listing = _listing(
        "db78-scam-ai",
        "WG-Zimmer, 490 € warm, mindestens 6 Monate. "
        "Bitte die Zahlung von 300 € Kaution vor der Besichtigung per Western Union.",
    )
    prefilter = deterministic_prefilter(listing, config)
    ai_facts = ListingFacts(
        advertiser_type="wg",
        housing_type="wg_room",
        warm_rent_eur=490,
        scam_risk="low",
        confidence=0.9,
    )
    ai = AIAnalysis(
        facts=ai_facts,
        decision_recommendation="APPLY",
        message=MessageDraft(body="x", language="de", address_register="du"),
    )
    facts, _remap = reconcile_facts(prefilter, ai, config)
    assert facts.scam_risk == "high"
    decision = apply_rules(facts, config, prefilter)
    assert decision.decision == "SKIP"


# ---- DB 78 bug 2: WG classification despite incidental Hausverwaltung mention -----


def test_db78_hausverwaltung_mentioned_as_third_party_stays_wg(config: dict) -> None:
    listing = _listing("db78-3", DB78_TEXT, "tolle WG mit Balkon")
    result = deterministic_prefilter(listing, config)
    assert result.facts.advertiser_type == "wg"
    assert is_formal_application_context(listing, result.facts) is False


# ---- DB 78 bug 4: AI outage falls through to deterministic fallback ----------------


def test_db78_ai_outage_produces_deterministic_fallback(settings, config, answers) -> None:
    outcome = _outage(
        _listing("db78-4", DB78_TEXT, "tolle WG mit Balkon"), settings, config, answers
    )
    assert outcome.status == "drafted"
    assert outcome.generation_trace.ai_attempted is True
    assert outcome.generation_trace.fallback_used is True
    assert outcome.generation_trace.fallback_scenario == "wg_zwischenmiete"
    assert outcome.message is not None
    assert outcome.message.body.strip()


# ---- --fallback-only diagnostic mode -----------------------------------------------


def test_fallback_only_makes_zero_cloud_calls(settings, config, answers) -> None:
    def must_not_run(*_args):
        raise AssertionError("cloud provider must never be called in --fallback-only mode")

    listing = _listing("diag-1", DB71_TEXT, "WG-Zimmer am Ring")
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=settings,
        callers={
            "anthropic": must_not_run,
            "openai": must_not_run,
            "gemini": must_not_run,
        },
        force_fallback=True,
    )
    assert outcome.status == "drafted"
    assert outcome.generation_trace.ai_attempted is False
    assert outcome.generation_trace.ai_skip_reason is not None
    assert outcome.generation_trace.fallback_used is True


def test_fallback_only_via_reprocess_makes_zero_cloud_or_browser_calls(
    settings, config, answers
) -> None:
    database = Database(settings.database_path)
    listing = _listing("diag-2", DB78_TEXT, "tolle WG mit Balkon")
    process_listing(listing, config=config, answers=answers, settings=settings, database=database)
    row = database.find_listing_by_url_or_id(listing.url)
    assert row is not None

    def fail(*_args, **_kwargs):
        raise AssertionError("must never call a cloud provider or browser")

    action = reprocess_listing(
        database,
        settings,
        config,
        answers,
        int(row["id"]),
        fallback_only=True,
        process_fn=lambda *a, **kw: process_listing(
            *a, **kw, callers={"anthropic": fail, "openai": fail, "gemini": fail}
        ),
    )
    assert action.ok
    assert action.status == "drafted"


@pytest.mark.parametrize("status", ["sent", "already_contacted", "send_state_unknown"])
def test_fallback_only_still_refuses_settled_send_states(settings, status: str) -> None:
    database = Database(settings.database_path)
    listing = viable_listing(listing_id=f"diag-settled-{status}")
    listing_db_id, _ = database.discover(listing)
    with database.connect() as connection:
        connection.execute("UPDATE listings SET status=? WHERE id=?", (status, listing_db_id))

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("must never reprocess a settled/ambiguous send state")

    action = reprocess_listing(
        database,
        settings,
        {},
        {},
        listing_db_id,
        fallback_only=True,
        process_fn=must_not_run,
    )

    assert not action.ok
    assert action.status == status
