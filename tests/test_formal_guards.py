import hashlib
import json
from pathlib import Path

import pytest

from tracefix.exceptions import BenchmarkError
from tracefix.formal_guards import (
    formal_config_issue,
    formal_response_model_issue,
    formal_response_usage_issue,
)
from tracefix.messages import Message, MessageRole
from tracefix.models import LLMConfig, LLMResponse, TokenUsage
from tracefix.p2_protocol import P2BudgetedLLM, P2FormalRunRequirements

PROVIDER = "DeepSeek official direct"


def config():
    return LLMConfig(
        model_name="deepseek/deepseek-flash",
        max_retries=0,
        extra_kwargs={
            "api_base": "https://api.deepseek.com",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )


def response():
    return LLMResponse(
        message=Message(role=MessageRole.ASSISTANT, content="ok"),
        model_name="deepseek-flash",
        usage=TokenUsage(input_tokens=10, output_tokens=2, total_tokens=12),
        raw_response={
            "model": "deepseek-flash",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "prompt_cache_hit_tokens": 4,
                "prompt_cache_miss_tokens": 6,
            },
        },
    )


def test_frozen_configuration_and_raw_response_are_valid():
    assert formal_config_issue(config(), PROVIDER) is None
    assert formal_response_model_issue(config(), PROVIDER, response()) is None
    assert formal_response_usage_issue(config(), PROVIDER, response()) is None


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "http://api.deepseek.com",
        "https://proxy.example.com",
        "https://api.deepseek.com.evil",
        "https://user:password@api.deepseek.com",
        "https://api.deepseek.com?route=other",
        "https://api.deepseek.com:bad",
        "https://api.deepseek.com/other",
    ],
)
def test_endpoint_cannot_be_redirected(endpoint):
    value = config()
    value.extra_kwargs["api_base"] = endpoint
    assert formal_config_issue(value, PROVIDER) == "formal_provider_endpoint_mismatch"


@pytest.mark.parametrize("change", ["thinking", "temperature", "retries", "base_url", "provider"])
def test_nonfrozen_commercial_configuration_is_rejected(change):
    value = config()
    if change == "thinking":
        value.extra_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    elif change == "temperature":
        value.temperature = 1
    elif change == "retries":
        value.extra_kwargs["num_retries"] = 2
    elif change == "base_url":
        value.extra_kwargs["base_url"] = "https://proxy.example.com"
    assert formal_config_issue(value, "other" if change == "provider" else PROVIDER)


@pytest.mark.parametrize("model", [None, "", "other-model", "deepseek/deepseek-flash"])
def test_model_identity_uses_raw_not_adapter_fallback(model):
    value = response()
    value.raw_response["model"] = model
    assert value.model_name == "deepseek-flash"
    assert formal_response_model_issue(config(), PROVIDER, value)


@pytest.mark.parametrize(
    "field",
    [
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
    ],
)
@pytest.mark.parametrize("bad", [None, True, "1", 1.5, -1])
def test_each_raw_usage_field_requires_nonnegative_integer(field, bad):
    value = response()
    value.raw_response["usage"][field] = bad
    assert formal_response_usage_issue(config(), PROVIDER, value)


def test_missing_usage_and_inconsistent_cache_or_normalization_fail_closed():
    value = response()
    value.raw_response.pop("usage")
    assert formal_response_usage_issue(config(), PROVIDER, value) == "response_usage_missing"
    value = response()
    value.raw_response["usage"]["prompt_cache_miss_tokens"] = 7
    assert formal_response_usage_issue(config(), PROVIDER, value) == "response_usage_inconsistent"
    value = response()
    value.usage = TokenUsage(input_tokens=10, output_tokens=0, total_tokens=10)
    assert (
        formal_response_usage_issue(config(), PROVIDER, value)
        == "response_usage_normalization_mismatch"
    )


def test_non_deepseek_fixture_is_unaffected():
    value = LLMConfig(model_name="fixture/model")
    reply = response()
    reply.raw_response = {}
    assert formal_config_issue(value, "fixture") is None
    assert formal_response_model_issue(value, "fixture", reply) is None
    assert formal_response_usage_issue(value, "fixture", reply) is None


def formal_parameters():
    return P2FormalRunRequirements(
        model_name="deepseek/deepseek-flash",
        provider=PROVIDER,
        pricing_source="isolated test fixture",
        currency="CNY",
        total_cost_cap_usd=100,
        input_cost_per_million_usd=2,
        output_cost_per_million_usd=8,
        input_cache_hit_cost_per_million=0.04,
        input_cache_miss_cost_per_million=2,
        output_cost_per_million=8,
    )


def budgeted_fixture(monkeypatch, tmp_path, reply):
    calls = []

    class FakeAdapter:
        def __init__(self, value):
            self.config = value

        def count_input_tokens(self, messages, tools):
            return 20

        def complete(self, messages, tools):
            calls.append("request")
            return reply

    monkeypatch.setattr("tracefix.p2_protocol.LiteLLMAdapter", FakeAdapter)
    ledger = tmp_path / "isolated-ledger.json"
    model = P2BudgetedLLM(
        config().model_copy(update={"max_output_tokens": 10}),
        ledger_path=ledger,
        formal=formal_parameters(),
        input_upper_bound=100,
    )
    return model, ledger, calls


def read_preserved_response(ledger):
    record = ledger["requests"][0]
    artifact = Path(record["response_evidence_path"])
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == record["response_evidence_sha256"]
    return json.loads(artifact.read_text(encoding="utf-8"))


def test_budget_layer_settles_known_usage_then_halts_raw_model_mismatch(monkeypatch, tmp_path):
    reply = response()
    reply.raw_response["model"] = "unexpected-model"
    # Deliberately retain normalized requested identity to exercise raw-response checking.
    model, ledger_path, calls = budgeted_fixture(monkeypatch, tmp_path, reply)
    with pytest.raises(BenchmarkError, match="response_model_identity_mismatch"):
        model.complete([])
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["calculated_spent_amount"] == pytest.approx(0.000036)
    assert ledger["reserved_amount"] == 0
    assert ledger["uncertain_request"] is False
    assert ledger["halt_reason"] == "response_model_identity_mismatch"
    assert ledger["requests"][0]["status"] == "settled"
    assert read_preserved_response(ledger)["provider_usage"] == reply.raw_response["usage"]
    before = ledger_path.read_bytes()
    with pytest.raises(BenchmarkError, match="uncertain request"):
        model.complete([])
    assert calls == ["request"]
    assert ledger_path.read_bytes() == before


def test_budget_layer_preserves_reservation_when_raw_usage_is_missing(monkeypatch, tmp_path):
    reply = response()
    reply.raw_response.pop("usage")
    model, ledger_path, calls = budgeted_fixture(monkeypatch, tmp_path, reply)
    with pytest.raises(BenchmarkError, match="response_usage_missing"):
        model.complete([])
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["calculated_spent_amount"] == 0
    assert ledger["reserved_amount"] == pytest.approx(0.00012)
    assert ledger["halt_reason"] == "response_usage_unknown"
    assert ledger["requests"][0]["status"] == "response_received_usage_unknown"
    assert ledger["requests"][0]["response_received"] is True
    assert read_preserved_response(ledger)["provider_usage"] is None
    before = ledger_path.read_bytes()
    with pytest.raises(BenchmarkError, match="uncertain request"):
        model.complete([])
    assert calls == ["request"]
    assert ledger_path.read_bytes() == before


def test_budget_layer_rejects_nonofficial_endpoint_before_adapter_construction(
    monkeypatch, tmp_path
):
    calls = []

    def forbidden_adapter(value):
        calls.append("constructed")
        raise AssertionError("provider must not be constructed")

    monkeypatch.setattr("tracefix.p2_protocol.LiteLLMAdapter", forbidden_adapter)
    value = config()
    value.extra_kwargs["api_base"] = "https://proxy.example.com"
    ledger = tmp_path / "never-created.json"
    with pytest.raises(BenchmarkError, match="formal_provider_endpoint_mismatch"):
        P2BudgetedLLM(value, ledger_path=ledger, formal=formal_parameters(), input_upper_bound=100)
    assert calls == []
    assert not ledger.exists()
