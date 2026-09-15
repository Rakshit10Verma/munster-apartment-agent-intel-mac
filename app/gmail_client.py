from __future__ import annotations

import base64
import html
import mimetypes
import re
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .config_loader import Settings
from .schemas import ContactResult, SourceListing

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def gmail_service(settings: Settings, interactive: bool = False) -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    credentials = None
    if settings.gmail_token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(settings.gmail_token_path), SCOPES)
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        if not interactive:
            raise RuntimeError("Gmail OAuth login required; run ./login.sh gmail")
        if not settings.gmail_credentials_path.is_file():
            raise RuntimeError(
                f"Gmail OAuth credentials missing at {settings.gmail_credentials_path}"
            )
        flow = InstalledAppFlow.from_client_secrets_file(
            str(settings.gmail_credentials_path), SCOPES
        )
        credentials = flow.run_local_server(port=0)
        settings.gmail_token_path.parent.mkdir(parents=True, exist_ok=True)
        settings.gmail_token_path.write_text(credentials.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def _message_body(payload: dict[str, Any]) -> str:
    chunks: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in {"text/plain", "text/html"}:
            decoded = _decode(data)
            if mime == "text/html":
                decoded = BeautifulSoup(decoded, "html.parser").get_text(" ")
            chunks.append(decoded)
        for child in part.get("parts", []):
            visit(child)

    visit(payload)
    return html.unescape("\n".join(chunks)).strip()


def _header(payload: dict[str, Any], name: str) -> str:
    for header in payload.get("headers", []):
        if header.get("name", "").casefold() == name.casefold():
            return str(header.get("value", ""))
    return ""


def _wg_urls(text: str) -> list[str]:
    return list(
        dict.fromkeys(
            re.findall(
                r"https?://(?:www\.)?wg-gesucht\.de/[A-Za-z0-9_./?=&%-]+",
                text,
                re.I,
            )
        )
    )


def discover_gmail_alerts(settings: Settings, max_results: int = 30) -> list[SourceListing]:
    service = gmail_service(settings)
    response = (
        service.users()
        .messages()
        .list(userId="me", q=settings.gmail_alert_query, maxResults=max_results)
        .execute()
    )
    listings: list[SourceListing] = []
    for summary in response.get("messages", []):
        message = (
            service.users().messages().get(userId="me", id=summary["id"], format="full").execute()
        )
        payload = message.get("payload", {})
        body = _message_body(payload)
        subject = _header(payload, "Subject")
        urls = _wg_urls(body)
        if not urls:
            continue
        for url in urls:
            match = re.search(r"(?:angebot|wohnungen|wg-zimmer)[^0-9]*(\d{5,})", url, re.I)
            external_id = match.group(1) if match else summary["id"]
            listings.append(
                SourceListing(
                    platform="wg_gesucht",
                    listing_id=external_id,
                    url=url,
                    title=subject,
                    raw_text=body,
                    source_metadata={"gmail_message_id": summary["id"]},
                )
            )
    return listings


def send_gmail(
    settings: Settings,
    recipient: str,
    subject: str,
    body: str,
    attachment: Path | None = None,
) -> ContactResult:
    if not settings.sending_enabled:
        return ContactResult(
            status="dry_run_ready",
            detail="Gmail draft validated; sending disabled by DRY_RUN/AUTO_SEND",
        )
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    if attachment:
        mime, _ = mimetypes.guess_type(attachment.name)
        main_type, sub_type = (mime or "application/octet-stream").split("/", 1)
        message.add_attachment(
            attachment.read_bytes(), maintype=main_type, subtype=sub_type, filename=attachment.name
        )
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    service = gmail_service(settings)
    sent = service.users().messages().send(userId="me", body={"raw": encoded}).execute()
    return ContactResult(
        status="sent",
        external_message_id=str(sent.get("id", "")),
        detail="Gmail API accepted message",
    )
