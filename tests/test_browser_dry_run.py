from __future__ import annotations

from dataclasses import replace

from app.browser import prepare_platform_contact
from app.schemas import (
    AnalysisOutcome,
    AttachmentDecision,
    ListingFacts,
    MessageDraft,
    RuleDecision,
    ValidationResult,
)
from tests.conftest import NATURAL_WG_BODY, viable_listing


def test_playwright_fills_and_attaches_but_never_sends(settings, tmp_path) -> None:
    html = tmp_path / "composer.html"
    html.write_text(
        """<!doctype html><html><body>
        <textarea name="message"></textarea>
        <input name="subject">
        <input type="file">
        <button type="submit" onclick="document.body.dataset.sent='yes'">Senden</button>
        </body></html>""",
        encoding="utf-8",
    )
    document = tmp_path / "Bewerbermappe.pdf"
    document.write_bytes(b"%PDF-test")
    listing = viable_listing(url=html.as_uri())
    outcome = AnalysisOutcome(
        listing=listing,
        facts=ListingFacts(confidence=0.9),
        rule_decision=RuleDecision(decision="APPLY"),
        status="drafted",
        message=MessageDraft(
            subject="Test subject",
            body=NATURAL_WG_BODY,
            hooks_used=["Radfahren"],
            language="de",
            address_register="du",
        ),
        attachment=AttachmentDecision(allowed=True, should_attach=True, path=str(document)),
        validation=ValidationResult(auto_send_allowed=True),
    )
    result = prepare_platform_contact(
        outcome,
        replace(settings, dry_run=True, auto_send=False, browser_headless=True),
    )
    assert result.status == "dry_run_ready", result.detail
    assert result.screenshot_path
    assert __import__("pathlib").Path(result.screenshot_path).is_file()
    assert "stopped before Send" in result.detail
