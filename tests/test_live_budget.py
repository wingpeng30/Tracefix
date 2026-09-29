from __future__ import annotations

import json

import pytest

from tracefix.exceptions import LLMProviderError, LLMResponseFormatError
from tracefix.live_budget import LiveBudgetAdapter
from tracefix.messages import Message, MessageRole
from tracefix.models.base import LLMConfig


class Client:
    def __init__(self, *, usage: dict | None = None) -> None:
        self.calls = 0
        self.usage = usage

    def completion(self, **_kwargs):
        self.calls += 1
        return {
            "model": "deepseek-flash",
            "usage": self.usage,
            "choices": [
                {"message": {"content": "done", "tool_calls": []}, "finish_reason": "stop"}
            ],
        }


def _adapter(tmp_path, client: Client, *, limit: float = 20.0) -> LiveBudgetAdapter:
    return LiveBudgetAdapter(
        LLMConfig(
            model_name="deepseek/deepseek-flash", max_output_tokens=512,
            max_retries=0, extra_kwargs={
                "api_base": "https://api.deepseek.com",
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        ),
        ledger_path=tmp_path / "ledger.json", limit_cny=limit, client=client,
    )


def test_live_budget_records_real_usage_and_refuses_next_request_over_limit(tmp_path) -> None:
    client = Client(usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})
    messages = [Message(role=MessageRole.USER, content="hello")]
    probe = _adapter(tmp_path, client)
    reserve = (
        probe.count_input_tokens(messages) * 2 + 512 * 8
    ) / 1_000_000
    adapter = _adapter(tmp_path, client, limit=reserve + 0.0001)
    adapter.complete(messages)
    record = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert record["requests"][0]["status"] == "completed"
    assert record["requests"][0]["peak_cost_cny"] == pytest.approx(0.00036)
    with pytest.raises(LLMProviderError, match="budget"):
        adapter.complete(messages)
    assert client.calls == 1


def test_live_budget_keeps_unknown_result_pending_and_blocks_replay(tmp_path) -> None:
    client = Client(usage=None)
    adapter = _adapter(tmp_path, client)
    messages = [Message(role=MessageRole.USER, content="hello")]
    with pytest.raises(LLMResponseFormatError, match="usage"):
        adapter.complete(messages)
    with pytest.raises(LLMProviderError, match="unknown prior request"):
        adapter.complete(messages)
    assert client.calls == 1


def test_live_budget_rejects_request_before_provider_when_reservation_exceeds_limit(
    tmp_path,
) -> None:
    client = Client(usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    adapter = _adapter(tmp_path, client, limit=0.001)
    with pytest.raises(LLMProviderError, match="budget"):
        adapter.complete([Message(role=MessageRole.USER, content="hello")])
    assert client.calls == 0


def test_live_budget_rejects_nonofficial_or_thinking_endpoint(tmp_path) -> None:
    config = LLMConfig(
        model_name="deepseek/deepseek-flash", max_output_tokens=512,
        max_retries=0, extra_kwargs={"api_base": "https://example.invalid"},
    )
    with pytest.raises(ValueError, match="official non-thinking"):
        LiveBudgetAdapter(config, ledger_path=tmp_path / "ledger.json")


def test_live_budget_rejects_retries_and_changed_ledger_identity(tmp_path) -> None:
    retried = LLMConfig(
        model_name="deepseek/deepseek-flash", max_output_tokens=512, max_retries=1,
    )
    with pytest.raises(ValueError, match="zero automatic retries"):
        LiveBudgetAdapter(retried, ledger_path=tmp_path / "ledger.json")

    client = Client(usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120})
    adapter = _adapter(tmp_path, client)
    message = [Message(role=MessageRole.USER, content="hello")]
    adapter.complete(message)
    adapter.complete(message)
    assert client.calls == 2
    changed_limit = _adapter(tmp_path, client, limit=19.0)
    with pytest.raises(LLMProviderError, match="identity changed"):
        changed_limit.complete(message)
    assert client.calls == 2
