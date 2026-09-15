from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .config_loader import Settings
from .schemas import AIAnalysis, ProviderMetadata
from .utils import extract_json


class ProviderError(RuntimeError):
    """A provider failed without exposing credentials or response bodies."""


class ProviderUnavailable(ProviderError):
    pass


class ProviderSchemaError(ProviderError):
    pass


class ProviderTransientError(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderCall:
    data: AIAnalysis
    metadata: ProviderMetadata


ProviderCaller = Callable[[str, str, Settings], ProviderCall]


def _safe_error(provider: str, exc: Exception) -> str:
    text = str(exc)
    for key_name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        secret = os.getenv(key_name, "")
        if secret:
            text = text.replace(secret, "<redacted>")
    return f"{provider}: {text[:300]}"


def _parse(text: str, provider: str) -> AIAnalysis:
    try:
        return AIAnalysis.model_validate(extract_json(text))
    except (ValueError, ValidationError) as exc:
        raise ProviderSchemaError(f"{provider}: response did not match contract") from exc


def _usage_value(usage: Any, *names: str) -> int | None:
    for name in names:
        value = getattr(usage, name, None)
        if isinstance(value, int):
            return value
    return None


def call_anthropic(system: str, prompt: str, settings: Settings) -> ProviderCall:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ProviderUnavailable("anthropic: key not configured")
    model = os.getenv("ANTHROPIC_MODEL", "").strip()
    if not model:
        raise ProviderUnavailable("anthropic: model not configured")
    started = time.monotonic()
    try:
        from anthropic import Anthropic

        client = Anthropic(
            api_key=key,
            timeout=float(settings.ai_timeout_seconds),
            max_retries=0,
        )
        response = client.messages.create(
            model=model,
            max_tokens=3500,
            system=f"{system}\nReturn only JSON, without Markdown fences.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            str(getattr(block, "text", ""))
            for block in response.content
            if getattr(block, "type", "") == "text"
        )
        data = _parse(text, "anthropic")
        usage = response.usage
        return ProviderCall(
            data,
            ProviderMetadata(
                provider="anthropic",
                model=model,
                latency_ms=round((time.monotonic() - started) * 1000),
                input_tokens=_usage_value(usage, "input_tokens"),
                output_tokens=_usage_value(usage, "output_tokens"),
            ),
        )
    except ProviderSchemaError:
        raise
    except Exception as exc:
        raise ProviderError(_safe_error("anthropic", exc)) from exc


def call_openai(system: str, prompt: str, settings: Settings) -> ProviderCall:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise ProviderUnavailable("openai: key not configured")
    model = os.getenv("OPENAI_MODEL", "").strip()
    if not model:
        raise ProviderUnavailable("openai: model not configured")
    started = time.monotonic()
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=key,
            timeout=float(settings.ai_timeout_seconds),
            max_retries=0,
        )
        response = client.responses.create(
            model=model,
            instructions=f"{system}\nReturn only JSON, without Markdown fences.",
            input=prompt,
        )
        data = _parse(response.output_text, "openai")
        usage = response.usage
        return ProviderCall(
            data,
            ProviderMetadata(
                provider="openai",
                model=model,
                latency_ms=round((time.monotonic() - started) * 1000),
                input_tokens=_usage_value(usage, "input_tokens"),
                output_tokens=_usage_value(usage, "output_tokens"),
            ),
        )
    except ProviderSchemaError:
        raise
    except Exception as exc:
        raise ProviderError(_safe_error("openai", exc)) from exc


def call_gemini(system: str, prompt: str, settings: Settings) -> ProviderCall:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise ProviderUnavailable("gemini: key not configured")
    model = os.getenv("GEMINI_MODEL", "").strip()
    if not model:
        raise ProviderUnavailable("gemini: model not configured")
    started = time.monotonic()
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(
            api_key=key, http_options=types.HttpOptions(timeout=settings.ai_timeout_seconds * 1000)
        )
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=AIAnalysis,
            ),
        )
        data = _parse(response.text or "", "gemini")
        usage = response.usage_metadata
        return ProviderCall(
            data,
            ProviderMetadata(
                provider="gemini",
                model=model,
                latency_ms=round((time.monotonic() - started) * 1000),
                input_tokens=_usage_value(usage, "prompt_token_count"),
                output_tokens=_usage_value(usage, "candidates_token_count"),
            ),
        )
    except ProviderSchemaError:
        raise
    except Exception as exc:
        raise ProviderError(_safe_error("gemini", exc)) from exc


DEFAULT_CALLERS: dict[str, ProviderCaller] = {
    "anthropic": call_anthropic,
    "openai": call_openai,
    "gemini": call_gemini,
}
_PROVIDER_COOLDOWN_UNTIL: dict[str, float] = {}


def _is_transient(exc: Exception) -> bool:
    status = getattr(exc.__cause__, "status_code", None)
    text = str(exc).casefold()
    return status in {408, 409, 429, 500, 502, 503, 504} or any(
        marker in text for marker in ("timeout", "timed out", "rate limit", "temporar")
    )


def route_cloud(
    system: str,
    prompt: str,
    settings: Settings,
    callers: dict[str, ProviderCaller] | None = None,
) -> tuple[ProviderCall, list[str]]:
    available = callers or DEFAULT_CALLERS
    use_circuit_breaker = callers is None
    notes: list[str] = []
    for name in settings.provider_order:
        if use_circuit_breaker and _PROVIDER_COOLDOWN_UNTIL.get(name, 0) > time.monotonic():
            notes.append(f"{name}: temporarily skipped after a recent transient failure")
            continue
        caller = available.get(name)
        if caller is None:
            notes.append(f"{name}: provider implementation unavailable")
            continue
        attempts = settings.ai_max_retries + 1
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                result = caller(system, prompt, settings)
                _PROVIDER_COOLDOWN_UNTIL.pop(name, None)
                return result, notes
            except ProviderError as exc:
                notes.append(str(exc))
                consumed_timeout = time.monotonic() - started >= settings.ai_timeout_seconds * 0.8
                if not _is_transient(exc) or attempt + 1 >= attempts or consumed_timeout:
                    if use_circuit_breaker and _is_transient(exc):
                        _PROVIDER_COOLDOWN_UNTIL[name] = (
                            time.monotonic() + settings.ai_provider_cooldown_seconds
                        )
                    break
                time.sleep(min(2**attempt, 2))
    raise ProviderError("all configured cloud providers failed: " + " | ".join(notes))
