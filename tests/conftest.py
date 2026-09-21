from __future__ import annotations

from pathlib import Path

import pytest

from app.config_loader import Settings, load_yaml
from app.message_policy import GERMAN_WG_VIEWING_CLOSING
from app.prefilter import deterministic_prefilter
from app.providers import ProviderCall
from app.schemas import (
    AIAnalysis,
    MessageDraft,
    ProviderMetadata,
    SourceListing,
)

NATURAL_WG_BODY = f"""Hallo zusammen,

eure WG mit gemeinsamen Kochabenden und der Begeisterung fürs Radfahren klingt sehr sympathisch.
Ich bin Rakshit, 21 Jahre alt, und starte im Oktober meinen Master in Information Systems an der
Universität Münster. Neben dem Studium arbeite ich vollständig remote als Werkstudent bei der
Landesbausparkasse. Ich koche gern, gehe bouldern und mag eine entspannte, soziale WG, respektiere
aber genauso persönlichen Freiraum. Bei Kartenspielen bin ich übrigens ganz klar bei Skyjo oder
Flip 7. Ich suche ein langfristiges Zuhause für ungefähr zwei Jahre.

{GERMAN_WG_VIEWING_CLOSING}"""


@pytest.fixture
def config() -> dict:
    return load_yaml("config.yaml")


@pytest.fixture
def answers() -> dict:
    return load_yaml("answer_bank.yaml")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        provider_order=("anthropic", "openai", "gemini"),
        ai_timeout_seconds=2,
        anthropic_timeout_seconds=2,
        openai_timeout_seconds=30,
        gemini_timeout_seconds=2,
        ai_max_retries=0,
        ai_provider_cooldown_seconds=300,
        dry_run=True,
        auto_send=False,
        bewerbermappe_path=None,
        wg_use_premium_boost=True,
        wg_premium_strict=True,
        browser_headless=True,
        browser_profile_path=tmp_path / "profile",
        browser_timeout_seconds=5,
        database_path=tmp_path / "agent.sqlite",
        input_directory=tmp_path / "inbox",
        poll_interval_seconds=1,
        gmail_enabled=False,
        gmail_credentials_path=tmp_path / "credentials.json",
        gmail_token_path=tmp_path / "token.json",
        gmail_alert_query="",
        log_level="INFO",
        telegram_enabled=False,
        telegram_bot_token="",
        telegram_allowed_user_id=None,
        telegram_chat_id="",
    )


def viable_listing(**updates: object) -> SourceListing:
    values = {
        "platform": "wg_gesucht",
        "listing_id": "1234567",
        "url": "https://www.wg-gesucht.de/wg-zimmer.1234567.html",
        "title": "Entspannte WG in Münster",
        "raw_text": (
            "WG-Zimmer in Münster, 490 € warm, mindestens 12 Monate. "
            "Wir kochen gern zusammen und fahren viel Fahrrad. Anmeldung möglich."
        ),
    }
    values.update(updates)
    return SourceListing.model_validate(values)


def successful_call(
    listing: SourceListing, config: dict, body: str = NATURAL_WG_BODY
) -> ProviderCall:
    prefilter = deterministic_prefilter(listing, config)
    facts = prefilter.facts.model_copy(
        update={
            "advertiser_type": "wg",
            "personalization_hooks": ["gemeinsamen Kochabenden und Radfahren"],
            "confidence": 0.96,
        }
    )
    message = MessageDraft(
        subject="WG-Zimmer in Münster",
        body=body,
        answered_question_ids=[question.id for question in facts.hidden_questions],
        applied_command_ids=[command.id for command in facts.hidden_commands],
        hooks_used=["gemeinsamen Kochabenden und Radfahren"],
        language="de",
        address_register="du",
    )
    return ProviderCall(
        AIAnalysis(
            facts=facts,
            decision_recommendation="APPLY",
            decision_reasons=["fits budget and duration"],
            message=message,
            confidence=0.96,
        ),
        ProviderMetadata(provider="mock", model="mock-1", latency_ms=15),
    )
