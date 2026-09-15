from __future__ import annotations

from typing import Any

from .config_loader import Settings
from .schemas import AttachmentDecision, ListingFacts, SourceListing


def decide_attachment(
    listing: SourceListing,
    facts: ListingFacts,
    settings: Settings,
    config: dict[str, Any],
) -> AttachmentDecision:
    reasons: list[str] = []
    path = settings.bewerbermappe_path
    legitimate = facts.scam_risk == "low" and not facts.critical_ambiguities
    if not legitimate:
        reasons.append("sensitive documents blocked until listing legitimacy is clear")
    if path is None:
        reasons.append("BEWERBERMAPPE_PATH is not configured")
    elif not path.is_absolute():
        reasons.append("BEWERBERMAPPE_PATH must be absolute")
    elif not path.is_file():
        reasons.append("Bewerbermappe file does not exist")
    else:
        allowed_suffixes = set(config["contact"]["allowed_attachment_suffixes"])
        if path.suffix.casefold() not in allowed_suffixes:
            reasons.append("Bewerbermappe file type is not allowed")
        if path.stat().st_size > int(config["contact"]["max_attachment_bytes"]):
            reasons.append("Bewerbermappe exceeds attachment size limit")

    file_ok = path is not None and not any(
        phrase in reason
        for reason in reasons
        for phrase in (
            "not configured",
            "must be absolute",
            "does not exist",
            "not allowed",
            "exceeds",
        )
    )
    allowed = legitimate and file_ok
    if listing.platform == "wg_gesucht":
        should_attach = allowed
    elif facts.contact_method == "email":
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
        reasons=reasons,
    )
