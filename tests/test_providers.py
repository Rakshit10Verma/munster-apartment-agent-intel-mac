from __future__ import annotations

from dataclasses import replace

import pytest

from app.config_loader import load_settings
from app.providers import (
    ProviderError,
    ProviderSchemaError,
    ProviderTransientError,
    ProviderUnavailable,
    route_cloud,
)
from tests.conftest import successful_call, viable_listing


def test_valid_provider_contract(settings, config) -> None:
    expected = successful_call(viable_listing(), config)
    result, notes = route_cloud("system", "prompt", settings, {"anthropic": lambda *_: expected})
    assert result.data.confidence == 0.96
    assert not notes


@pytest.mark.parametrize(
    "error",
    [ProviderSchemaError("bad schema"), ProviderUnavailable("quota/payment unavailable")],
)
def test_schema_or_quota_falls_back(settings, config, error: ProviderError) -> None:
    expected = successful_call(viable_listing(), config)

    def first(*_args):
        raise error

    result, notes = route_cloud(
        "system",
        "prompt",
        settings,
        {"anthropic": first, "openai": lambda *_: expected},
    )
    assert result is expected
    assert notes


def test_timeout_retries_same_provider(settings, config) -> None:
    expected = successful_call(viable_listing(), config)
    attempts = 0

    def flaky(*_args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProviderTransientError("timeout")
        return expected

    retry_settings = replace(settings, provider_order=("anthropic",), ai_max_retries=1)
    result, _ = route_cloud("system", "prompt", retry_settings, {"anthropic": flaky})
    assert result is expected
    assert attempts == 2


def test_all_providers_unavailable(settings) -> None:
    def unavailable(*_args):
        raise ProviderUnavailable("not configured")

    with pytest.raises(ProviderError, match="all configured"):
        route_cloud(
            "system",
            "prompt",
            settings,
            {name: unavailable for name in settings.provider_order},
        )


def test_provider_timeout_overrides_are_configurable_and_openai_has_safe_floor(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("ANTHROPIC_TIMEOUT_SECONDS", "31")
    monkeypatch.setenv("OPENAI_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("GEMINI_TIMEOUT_SECONDS", "32")
    monkeypatch.setenv("FREELLM_TIMEOUT_SECONDS", "33")
    settings = load_settings()
    assert settings.provider_timeout_seconds("anthropic") == 31
    assert settings.provider_timeout_seconds("openai") == 30
    assert settings.provider_timeout_seconds("gemini") == 32
    assert settings.provider_timeout_seconds("freellm") == 33


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False)])
def test_wg_premium_strict_respects_explicit_env_value(
    monkeypatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv("WG_PREMIUM_STRICT", raw)
    assert load_settings().wg_premium_strict is expected


def test_wg_premium_strict_defaults_to_false_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("WG_PREMIUM_STRICT", raising=False)
    monkeypatch.setattr("app.config_loader.load_dotenv", lambda *_args, **_kwargs: None)
    assert load_settings().wg_premium_strict is False

def test_freellm_is_accepted_in_provider_order(monkeypatch) -> None:
    monkeypatch.setenv("AI_PROVIDER_ORDER", "freellm,gemini")
    monkeypatch.setenv("FREELLM_API_KEY", "local-test-key")
    monkeypatch.setenv("FREELLM_MODEL", "test-route")
    settings = load_settings()
    assert settings.provider_order == ("freellm", "gemini")
    assert settings.freellm_base_url == "http://127.0.0.1:3001/v1"
    assert settings.freellm_api_key == "local-test-key"
    assert settings.freellm_model == "test-route"
