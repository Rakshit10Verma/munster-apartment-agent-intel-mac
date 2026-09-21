from __future__ import annotations

from dataclasses import replace

from app.documents import decide_attachment
from app.schemas import ListingFacts
from tests.conftest import viable_listing


def test_wg_low_risk_uses_account_bewerbermappe_without_local_path(settings, config) -> None:
    result = decide_attachment(
        viable_listing(),
        ListingFacts(scam_risk="low"),
        settings,
        config,
    )
    assert result.allowed and result.should_attach
    assert result.source == "wg_account"
    assert result.path is None
    assert result.requires_browser_verification


def test_medium_risk_blocks_sensitive_document(settings, config, tmp_path) -> None:
    document = tmp_path / "Bewerbermappe.pdf"
    document.write_bytes(b"%PDF-test")
    result = decide_attachment(
        viable_listing(),
        ListingFacts(scam_risk="medium"),
        replace(settings, bewerbermappe_path=document),
        config,
    )
    assert not result.allowed
    assert not result.should_attach


def test_email_attaches_only_when_explicitly_requested(settings, config, tmp_path) -> None:
    document = tmp_path / "Bewerbermappe.pdf"
    document.write_bytes(b"%PDF-test")
    listing = viable_listing(platform="asta_muenster", contact_email="owner@example.test")
    base = ListingFacts(contact_method="email", scam_risk="low")
    configured = replace(settings, bewerbermappe_path=document)
    assert not decide_attachment(listing, base, configured, config).should_attach
    requested = base.model_copy(update={"documents_explicitly_requested": ["SCHUFA"]})
    assert decide_attachment(listing, requested, configured, config).should_attach


def test_non_pdf_is_blocked(settings, config, tmp_path) -> None:
    document = tmp_path / "Bewerbermappe.zip"
    document.write_bytes(b"private")
    result = decide_attachment(
        viable_listing(platform="asta_muenster"),
        ListingFacts(
            scam_risk="low",
            contact_method="email",
            documents_explicitly_requested=["Bewerbermappe"],
        ),
        replace(settings, bewerbermappe_path=document),
        config,
    )
    assert not result.allowed
