from __future__ import annotations

from pathlib import Path

from .browser import prepare_platform_contact
from .config_loader import Settings, load_yaml
from .database import Database
from .documents import validate_applicant_photo, validate_local_bewerbermappe
from .gmail_client import send_gmail
from .prefilter import photo_required_for_initial_contact
from .schemas import AnalysisOutcome, ContactResult, SendTrigger


def contact_listing(
    outcome: AnalysisOutcome, settings: Settings, database: Database, trigger: SendTrigger
) -> ContactResult:
    """The one shared send pipeline for every trigger (daemon autonomous, ./approve.sh,
    Telegram Confirm Send, the dashboard's Approve & Send). `trigger` decides only who
    is allowed to initiate a real send (see Settings.send_permitted) -- every other
    safety gate below (dedup, Bewerbermappe, validation, verification) applies
    identically regardless of who called this."""
    if outcome.database_id is None:
        return ContactResult(status="review_required", detail="listing is not persisted")
    if not outcome.validation.auto_send_allowed or not outcome.message:
        return ContactResult(status="review_required", detail="validation gates did not pass")
    listing_db_id = outcome.database_id
    channel = "email" if outcome.facts.contact_method == "email" else "platform"
    actual = settings.send_permitted(trigger)
    if (
        channel == "platform"
        and outcome.listing.platform == "wg_gesucht"
        and (
            outcome.facts.photo_required_with_first_message
            or photo_required_for_initial_contact(outcome.listing.raw_text)
        )
    ):
        photo_error = validate_applicant_photo(
            settings.applicant_photo_path, load_yaml("config.yaml")
        )
        if photo_error:
            result = ContactResult(status="review_required", detail=photo_error)
            database.record_contact(
                listing_db_id,
                "actual" if actual else "dry_run",
                channel,
                result.status,
                detail=result.detail,
                trigger=trigger,
            )
            return result
    # The WG contact flow uses the account-stored Bewerbermappe, not a local file.
    # Refuse any stale/unsafe attachment policy before opening the browser. Email
    # uses the configured local file, which must be checked before claiming a send.
    if (
        channel == "platform"
        and outcome.listing.platform == "wg_gesucht"
        and not (
            outcome.attachment.allowed
            and outcome.attachment.should_attach
            and outcome.attachment.source == "wg_account"
        )
    ):
        result = ContactResult(
            status="review_required",
            detail="WG account Bewerbermappe is blocked by document/scam policy",
        )
        database.record_contact(
            listing_db_id,
            "actual" if actual else "dry_run",
            channel,
            result.status,
            detail=result.detail,
            trigger=trigger,
        )
        return result
    if channel == "email" and outcome.attachment.should_attach:
        document_path = Path(outcome.attachment.path) if outcome.attachment.path else None
        file_error = validate_local_bewerbermappe(document_path, load_yaml("config.yaml"))
        if file_error:
            result = ContactResult(status="bewerbermappe_attachment_failed", detail=file_error)
            database.record_contact(
                listing_db_id,
                "actual" if actual else "dry_run",
                channel,
                result.status,
                detail=result.detail,
                trigger=trigger,
            )
            return result
    try:
        if channel == "email":
            if actual and not database.claim_actual_contact(listing_db_id, channel):
                return ContactResult(
                    status="already_contacted",
                    detail="duplicate actual contact blocked by database",
                )
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
                    send_permitted=actual,
                )
        else:
            result = prepare_platform_contact(
                outcome,
                settings,
                send_permitted=actual,
                claim_actual=(lambda: database.claim_actual_contact(listing_db_id, channel))
                if actual
                else None,
                record_send_clicked=(
                    lambda fingerprint, body, premium_state, attachment_state, url: (
                        database.record_send_clicked(
                            listing_db_id, fingerprint, body, premium_state, attachment_state, url
                        )
                    )
                )
                if actual
                else None,
            )
    except Exception as exc:
        result = ContactResult(status="send_failed", detail=str(exc)[:500])
    database.record_contact(
        listing_db_id,
        "actual" if actual else "dry_run",
        channel,
        result.status,
        result.external_message_id,
        result.detail,
        trigger=trigger,
    )
    return result
