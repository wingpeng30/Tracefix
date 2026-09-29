"""Durable, conservative CNY budget for explicitly authorized live experiments."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from tracefix.exceptions import LLMProviderError
from tracefix.messages import Message
from tracefix.models.base import LLMConfig, LLMResponse
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.tools.base import ToolSpec


class LiveBudgetAdapter(LiteLLMAdapter):
    """Reserve peak-price capacity before each DeepSeek Flash request."""

    PEAK_INPUT_CNY_PER_MILLION = 2.0
    PEAK_OUTPUT_CNY_PER_MILLION = 8.0

    def __init__(
        self, config: LLMConfig, *, ledger_path: Path, limit_cny: float = 20.0,
        client: Any | None = None,
    ) -> None:
        super().__init__(config, client=client)
        if config.model_name != "deepseek/deepseek-flash" or config.max_retries != 0:
            raise ValueError("live budget requires DeepSeek Flash with zero automatic retries")
        if config.max_output_tokens is None or limit_cny <= 0:
            raise ValueError("live budget requires bounded output and positive CNY limit")
        self.ledger_path = ledger_path.resolve()
        self.limit_cny = limit_cny

    def _read(self) -> dict[str, Any]:
        if not self.ledger_path.is_file():
            return {"schema_version": 1, "limit_cny": self.limit_cny, "requests": []}
        data = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or data.get("limit_cny") != self.limit_cny:
            raise LLMProviderError("live budget ledger identity changed")
        if any(item.get("status") != "completed" for item in data["requests"]):
            raise LLMProviderError("live budget has an unknown prior request; stop paid calls")
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.ledger_path.with_name(self.ledger_path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.ledger_path)

    def complete(
        self, messages: tuple[Message, ...] | list[Message],
        tools: tuple[ToolSpec, ...] | list[ToolSpec] = (),
    ) -> LLMResponse:
        data = self._read()
        input_bound = self.count_input_tokens(messages, tools)
        request_max_cny = (
            input_bound * self.PEAK_INPUT_CNY_PER_MILLION
            + self.config.max_output_tokens * self.PEAK_OUTPUT_CNY_PER_MILLION
        ) / 1_000_000
        spent = sum(item["peak_cost_cny"] for item in data["requests"])
        if spent + request_max_cny > self.limit_cny:
            raise LLMProviderError("live experiment CNY budget would be exceeded")
        kwargs = self.request_kwargs(messages, tools)
        request_hash = hashlib.sha256(json.dumps(
            kwargs, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        entry: dict[str, Any] = {
            "status": "pending", "request_sha256": request_hash,
            "input_bound": input_bound, "reserved_peak_cny": request_max_cny,
        }
        data["requests"].append(entry)
        self._write(data)
        try:
            response = super().complete(messages, tools)
            entry.update({
                "status": "completed", "response_model": response.model_name,
                "usage": response.usage.model_dump(mode="json"),
                "peak_cost_cny": (
                    response.usage.input_tokens * self.PEAK_INPUT_CNY_PER_MILLION
                    + response.usage.output_tokens * self.PEAK_OUTPUT_CNY_PER_MILLION
                ) / 1_000_000,
            })
            self._write(data)
            return response
        except BaseException:
            # A request may have reached the provider; never replay automatically.
            raise
