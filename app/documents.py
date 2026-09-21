from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config_loader import Settings
from .schemas import AttachmentDecision, ListingFacts, SourceListing


def validate_local_bewerbermappe(path: Path | None, config: dict[str, Any]) -> str | None:
    """Check a local document before any browser/Gmail contact flow begins."""
    if path is None:
        return "BEWERBERMAPPE_PATH is not configured"
    if not path.is_absolute():
        return "BEWERBERMAPPE_PATH must be absolute"
    if not path.is_file():
        return "Bewerbermappe file does not exist or is not a regular file"
    if not os.access(path, os.R_OK):
        return "Bewerbermappe file is not readable"
    if path.suffix.casefold() not in set(config["contact"]["allowed_attachment_suffixes"]):
        return "Bewerbermappe file type is not allowed"
    try:
        size = path.stat().st_size
        with path.open("rb") as document:
            signature = document.read(5)
    except OSError:
        return "Bewerbermappe file is not readable"
    if size <= 0:
        return "Bewerbermappe file is empty"
    if size > int(config["contact"]["max_attachment_bytes"]):
        return "Bewerbermappe exceeds attachment size limit"
    if path.suffix.casefold() == ".pdf" and signature != b"%PDF-":
        return "Bewerbermappe file does not appear to be a PDF"
    return None


def validate_applicant_photo(path: Path | None, config: dict[str, Any]) -> str | None:
    """Validate the separate, opt-in photo without reading or logging its contents."""
    if path is None:
        return "APPLICANT_PHOTO_PATH is not configured for a required first-contact photo"
    if not path.is_absolute():
        return "APPLICANT_PHOTO_PATH must be absolute"
    if not path.is_file():
        return "Applicant photo does not exist or is not a regular file"
    if not os.access(path, os.R_OK):
        return "Applicant photo is not readable"
    suffix = path.suffix.casefold()
    if suffix not in {".jpg", ".jpeg", ".png"}:
        return "Applicant photo must be a JPG or PNG"
    try:
        size = path.stat().st_size
        with path.open("rb") as photo:
            signature = photo.read(8)
    except OSError:
        return "Applicant photo is not readable"
    if size <= 0:
        return "Applicant photo is empty"
    if size < 1024:
        return "Applicant photo is too small to be a plausible image"
    if size > int(config["contact"].get("max_attachment_bytes", 15_000_000)):
        return "Applicant photo exceeds attachment size limit"
    if suffix in {".jpg", ".jpeg"} and not signature.startswith(b"\xff\xd8\xff"):
        return "Applicant photo does not appear to be a JPEG"
    if suffix == ".png" and signature != b"\x89PNG\r\n\x1a\n":
        return "Applicant photo does not appear to be a PNG"
    return None


def decide_attachment(
    listing: SourceListing,
    facts: ListingFacts,
    settings: Settings,
    config: dict[str, Any],
) -> AttachmentDecision:
    reasons: list[str] = []
    legitimate = facts.scam_risk == "low" and not facts.critical_ambiguities
    if not legitimate:
        reasons.append("sensitive documents blocked until listing legitimacy is clear")

    # WG-Gesucht stores Rakshit's Bewerbermappe in the account. It must be selected
    # anew in the site's composer and positively verified there; no local path is used.
    if listing.platform == "wg_gesucht":
        if legitimate:
            reasons.append("select existing Meine Bewerbermappe in the WG composer")
        return AttachmentDecision(
            allowed=legitimate,
            should_attach=legitimate,
            source="wg_account" if legitimate else "none",
            requires_browser_verification=legitimate,
            reasons=reasons,
        )

    path = settings.bewerbermappe_path
    file_error = validate_local_bewerbermappe(path, config)
    if file_error:
        reasons.append(file_error)
    file_ok = file_error is None
    allowed = legitimate and file_ok
    if facts.contact_method == "email":
        should_attach = allowed and bool(facts.documents_explicitly_requested)
        if allowed and not facts.documents_explicitly_requested:
            reasons.append("email listing did not explicitly request documents")
    else:
        should_attach = False
        if allowed:
            reasons.append("attachments are not enabled for this contact channel")
    return AttachmentDecision(
        allowed=allowed,
        should_attach=should_attach,
        path=str(path) if path else None,
        source="local_file" if allowed else "none",
        reasons=reasons,
    )
