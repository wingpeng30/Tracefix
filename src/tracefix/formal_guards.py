"""Fail-closed checks for the frozen official DeepSeek formal campaign.

These helpers have no side effects. The budget layer persists response evidence,
keeps reservations for unknown usage, and settles known usage before a model halt.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from tracefix.models.base import LLMConfig, LLMResponse


def _deepseek(config: LLMConfig, provider: str) -> bool:
    return config.model_name.casefold().startswith("deepseek/") or "deepseek" in provider.casefold()


def _official_endpoint(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname == "api.deepseek.com"
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and parsed.path.rstrip("/") in ("", "/v1")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def formal_config_issue(config: LLMConfig, provider: str) -> str | None:
    """Call before constructing/invoking the real provider, only in formal mode."""
    if not _deepseek(config, provider):
        return None
    if config.model_name != "deepseek/deepseek-flash" or provider != "DeepSeek official direct":
        return "formal_provider_identity_mismatch"
    if not _official_endpoint(config.extra_kwargs.get("api_base")):
        return "formal_provider_endpoint_mismatch"
    if "base_url" in config.extra_kwargs and not _official_endpoint(
        config.extra_kwargs["base_url"]
    ):
        return "formal_provider_endpoint_mismatch"
    body = config.extra_kwargs.get("extra_body")
    if not isinstance(body, dict) or body.get("thinking") != {"type": "disabled"}:
        return "formal_thinking_configuration_mismatch"
    if config.temperature != 0:
        return "formal_temperature_mismatch"
    if config.max_retries != 0 or config.extra_kwargs.get("num_retries", 0) != 0:
        return "formal_retries_mismatch"
    return None


def formal_response_model_issue(
    config: LLMConfig, provider: str, response: LLMResponse
) -> str | None:
    """Read raw model identity so adapter fallback cannot disguise a missing field."""
    if not _deepseek(config, provider):
        return None
    model = response.raw_response.get("model")
    if not isinstance(model, str) or not model:
        return "response_model_identity_missing"
    expected = config.model_name.removeprefix("deepseek/")
    if model != expected:
        return "response_model_identity_mismatch"
    return None


def formal_response_usage_issue(
    config: LLMConfig, provider: str, response: LLMResponse
) -> str | None:
    """Require complete raw supplier counts before any reservation is released."""
    if not _deepseek(config, provider):
        return None
    usage = response.raw_response.get("usage")
    if not isinstance(usage, dict):
        return "response_usage_missing"
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    )
    if any(type(usage.get(field)) is not int or usage[field] < 0 for field in fields):
        return "response_usage_incomplete_or_noninteger"
    if usage["prompt_tokens"] <= 0 or (
        usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
        or usage["prompt_tokens"]
        != usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]
    ):
        return "response_usage_inconsistent"
    if (
        response.usage.input_tokens != usage["prompt_tokens"]
        or response.usage.output_tokens != usage["completion_tokens"]
        or response.usage.total_tokens != usage["total_tokens"]
    ):
        return "response_usage_normalization_mismatch"
    return None
