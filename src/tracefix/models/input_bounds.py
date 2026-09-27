"""Provider-scoped input bounds. Estimates are never accepted as hard limits."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Literal

from tracefix.exceptions import LLMProviderError

DEEPSEEK_FLASH_CONTEXT = 1_048_576
DEEPSEEK_FLASH_MAX_OUTPUT = 393_216
DEEPSEEK_FLASH_SOURCE = "https://api-docs.deepseek.com/api/list-models/"
V41_TOKENIZER_SHA256 = "81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99"
V41_RECIPE_VERSION = "0.1.1"
_UNSAFE_LITERALS = (
    "<think>",
    "</think>",
    "<｜",
    "｜DSML｜",
    "｜begin▁of▁sentence｜",
    "｜end▁of▁sentence｜",
)


@dataclass(frozen=True)
class InputBound:
    tokens: int | None
    status: Literal["verified_exact", "verified_upper_bound", "estimate", "unavailable"]
    method: str
    identity: str


def deepseek_flash_capability(model_name: str, provider: str, api_base: str) -> tuple[int, int]:
    """Never apply official capacity to a reseller or an unknown alias."""
    if (
        model_name.casefold() not in {"deepseek/deepseek-flash", "deepseek-flash"}
        or "deepseek" not in provider.casefold()
        or api_base.rstrip("/") != "https://api.deepseek.com"
    ):
        raise LLMProviderError("official DeepSeek Flash context capability is unavailable")
    return DEEPSEEK_FLASH_CONTEXT, DEEPSEEK_FLASH_MAX_OUTPUT


def count_deepseek_v41_request(body: dict[str, Any]) -> InputBound:
    """Use the pinned official renderer/tokenizer only in the calibrated text scope."""
    if body.get("model") != "deepseek-flash" or body.get("thinking") != {"type": "disabled"}:
        return InputBound(None, "unavailable", "deepseek-v41", "unsupported request settings")
    if any(not isinstance(m.get("content"), (str, type(None))) for m in body.get("messages", [])):
        return InputBound(None, "unavailable", "deepseek-v41", "non-text message")
    if any(literal in json.dumps(body, ensure_ascii=False) for literal in _UNSAFE_LITERALS):
        return InputBound(None, "unavailable", "deepseek-v41", "unverified special-token literal")
    tokenizer_path = os.environ.get("TRACEFIX_DEEPSEEK_V41_TOKENIZER_JSON")
    if not tokenizer_path:
        return InputBound(None, "unavailable", "deepseek-v41", "official tokenizer not configured")
    path = Path(tokenizer_path).expanduser().resolve()
    try:
        if metadata.version("deepseek-recipe") != V41_RECIPE_VERSION:
            raise ValueError("official recipe package version differs from pinned identity")
        if hashlib.sha256(path.read_bytes()).hexdigest() != V41_TOKENIZER_SHA256:
            raise ValueError("tokenizer SHA-256 does not match the pinned official copy")
        recipe = importlib.import_module("deepseek_recipe")
        request = recipe.ChatCompletionRequest(body)
        converted = request.convert(recipe.ConversionOptions())
        tokenizer = recipe.Tokenizer.from_file(str(path))
        encoding = recipe.DeepseekV41Encoding().with_tokenizer(tokenizer)
        value = len(encoding.encode(converted.conversation))
    except (ImportError, OSError, ValueError, AttributeError, RuntimeError) as exc:
        return InputBound(None, "unavailable", "deepseek-v41", type(exc).__name__)
    if value <= 0:
        return InputBound(None, "unavailable", "deepseek-v41", "invalid token count")
    # Rendering a request is not proof that the hosted API accounts for every
    # wrapper token in the same way. Until a provider-usage comparison proves a
    # conservative contract, this count can guide context reduction only.
    return InputBound(value, "estimate", "deepseek-recipe-v41", V41_TOKENIZER_SHA256)
