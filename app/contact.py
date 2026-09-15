from __future__ import annotations

from pathlib import Path

from .browser import prepare_platform_contact
from .config_loader import Settings
from .database import Database
from .gmail_client import send_gmail
from .schemas import AnalysisOutcome, ContactResult


def contact_listing(
    outcome: AnalysisOutcome, settings: Settings, database: Database
) -> ContactResult:
    if outcome.database_id is None:
        return ContactResult(status="review_required", detail="listing is not persisted")
    if not outcome.validation.auto_send_allowed or not outcome.message:
        return ContactResult(status="review_required", detail="validation gates did not pass")
    channel = "email" if outcome.facts.contact_method == "email" else "platform"
    actual = settings.sending_enabled
    if actual and not database.claim_actual_contact(outcome.database_id, channel):
        return ContactResult(
            status="review_required", detail="duplicate actual contact blocked by database"
        )
    try:
        if channel == "email":
            recipient = outcome.facts.contact_email or outcome.listing.contact_email
            if not recipient:
                result = ContactResult(status="review_required", detail="contact email missing")
            else:
                attachment = (
                    Path(outcome.attachment.path)
                    if outcome.attachment.should_attach and outcome.attachment.path
                    else None
                )
                result = send_gmail(
                    settings,
                    recipient,
                    outcome.message.subject,
                    outcome.message.body,
                    attachment,
                )
        else:
            result = prepare_platform_contact(outcome, settings)
    except Exception as exc:
        result = ContactResult(status="send_failed", detail=str(exc)[:500])
    database.record_contact(
        outcome.database_id,
        "actual" if actual else "dry_run",
        channel,
        result.status,
        result.external_message_id,
        result.detail,
    )
    return result
