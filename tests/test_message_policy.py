from __future__ import annotations

from app.message_policy import (
    GERMAN_WG_VIEWING_CLOSING,
    count_age_mismatch_mentions,
    enforce_message_policy,
)
from app.schemas import ListingFacts, MessageDraft
from tests.conftest import viable_listing


def _age_mismatch_facts() -> ListingFacts:
    return ListingFacts(
        listing_language="de",
        advertiser_type="wg",
        housing_type="wg_room",
        age_min=25,
        age_max=35,
        age_requirement_strength="soft",
        age_mismatch=True,
    )


def test_cloud_viewing_wording_is_replaced_by_required_closing() -> None:
    draft = MessageDraft(
        body=(
            "Hallo zusammen,\n\nEure ruhige WG gefällt mir sehr.\n\n"
            "Eine Video-Besichtigung wäre möglich. Die Bewerbermappe reiche ich später nach.\n\n"
            "Viele Grüße\nRakshit"
        ),
        language="de",
        address_register="du",
    )
    result = enforce_message_policy(
        viable_listing(),
        ListingFacts(listing_language="de", advertiser_type="wg"),
        draft,
    )
    assert result.body.endswith(GERMAN_WG_VIEWING_CLOSING)
    assert "reiche ich später" not in result.body
    assert result.body.count("Liebe Grüße") == 1


def test_soft_age_mismatch_gets_natural_fit_acknowledgement_when_ai_omits_it() -> None:
    draft = MessageDraft(
        body=f"Hallo zusammen,\n\nEure WG gefällt mir sehr.\n\n{GERMAN_WG_VIEWING_CLOSING}",
        language="de",
        address_register="du",
    )
    result = enforce_message_policy(
        viable_listing(),
        ListingFacts(
            listing_language="de",
            advertiser_type="wg",
            housing_type="wg_room",
            age_min=25,
            age_max=35,
            age_requirement_strength="soft",
            age_mismatch=True,
        ),
        draft,
    )
    assert "zwischen 25 und 35" in result.body
    assert "etwas jünger" in result.body
    assert "menschlich trotzdem gut passen könnte" in result.body
    assert "unkompliziert" not in result.body
    assert "nötige Reife" not in result.body
    assert result.body.endswith(GERMAN_WG_VIEWING_CLOSING)
    assert count_age_mismatch_mentions(result.body) == 1


def test_ai_authored_age_mismatch_sentence_is_replaced_not_duplicated() -> None:
    """Reproduces the real production incident on listing 14096490: the cloud
    provider already wrote its own brief age-mismatch acknowledgement (without the
    maturity reassurance), and the deterministic policy used to add a second, separate
    sentence on top of it instead of recognizing and replacing the first one."""
    draft = MessageDraft(
        body=(
            "Hallo zusammen,\n\nMit 21 bin ich zwar etwas jünger als eure gesuchte Spanne, aber "
            "das dürfte kein Problem sein. Euer Wohnzimmer gefällt mir sehr.\n\n"
            f"{GERMAN_WG_VIEWING_CLOSING}"
        ),
        language="de",
        address_register="du",
    )
    result = enforce_message_policy(viable_listing(), _age_mismatch_facts(), draft)
    assert count_age_mismatch_mentions(result.body) == 1
    assert "zwischen 25 und 35" in result.body
    assert "Wohnzimmer gefällt mir sehr" in result.body
    assert result.body.endswith(GERMAN_WG_VIEWING_CLOSING)


def test_age_mismatch_note_is_not_duplicated_when_already_fully_acknowledged() -> None:
    draft = MessageDraft(
        body=(
            "Hallo zusammen,\n\nIch weiß, dass ihr eigentlich jemanden zwischen 25 und 35 sucht. "
            "Ich werde im Oktober 22 und bin damit etwas jünger, glaube aber, dass ich trotzdem "
            "gut zu euch passen könnte. Ich bin eher ruhig, verlässlich und ordentlich und "
            f"bringe die nötige Reife für ein entspanntes Zusammenleben mit.\n\n"
            f"{GERMAN_WG_VIEWING_CLOSING}"
        ),
        language="de",
        address_register="du",
    )
    result = enforce_message_policy(viable_listing(), _age_mismatch_facts(), draft)
    assert count_age_mismatch_mentions(result.body) == 1
