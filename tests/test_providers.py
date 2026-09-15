from __future__ import annotations

from dataclasses import replace

import pytest

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
