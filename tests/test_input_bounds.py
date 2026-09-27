"""Zero-cost admission checks for the new provider-scoped P2 boundary."""

from __future__ import annotations

import pytest

from tracefix.exceptions import BenchmarkError, LLMProviderError
from tracefix.models.base import LLMConfig
from tracefix.models.input_bounds import (
    InputBound,
    count_deepseek_v41_request,
    deepseek_flash_capability,
)
from tracefix.p2_protocol import P2BudgetedLLM, P2FormalRunRequirements, P2ProtocolConfig


def _formal() -> P2FormalRunRequirements:
    return P2FormalRunRequirements(
        model_name="deepseek/deepseek-flash",
        provider="DeepSeek official direct",
        pricing_source="frozen-local-test",
        total_cost_cap_usd=100,
        input_cost_per_million_usd=1,
        output_cost_per_million_usd=1,
    )


def _llm(
    tmp_path,
    tokens: int,
    *,
    budget: int = 2_000_000,
    status="verified_upper_bound",
    request_limit: int | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = LLMConfig(
        model_name="deepseek/deepseek-flash",
        max_output_tokens=4096,
        max_retries=0,
        extra_kwargs={
            "api_base": "https://api.deepseek.com",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )
    llm = P2BudgetedLLM(
        config,
        ledger_path=tmp_path / "ledger.json",
        formal=_formal(),
        input_upper_bound=request_limit,
        trial_input_budget=budget,
    )

    class Delegate:
        def count_input_bound(self, messages, tools=()):
            return InputBound(tokens, status, "test-qualified-counter", "fixture")

        def complete(self, messages, tools=()):
            raise AssertionError("admission test must not invoke provider")

    llm._delegate = Delegate()
    return llm


def test_long_context_admission_and_exact_model_edge(tmp_path) -> None:
    # A qualified count above the former 128k bound reaches reservation and
    # only then our sentinel provider; it is not blocked by an extra cap.
    with pytest.raises(AssertionError, match="must not invoke provider"):
        _llm(tmp_path / "over-128k", 128_001).complete(())
    with pytest.raises(AssertionError, match="must not invoke provider"):
        _llm(tmp_path / "exact-edge", 1_044_480).complete(())
    with pytest.raises(BenchmarkError, match="context window") as exceeded:
        _llm(tmp_path / "over-edge", 1_044_481).complete(())
    assert exceeded.value.code == "p2_model_context_exceeded"


def test_standard_budget_and_unqualified_count_stop_before_reservation(tmp_path) -> None:
    with pytest.raises(BenchmarkError, match="trial input budget") as exhausted:
        _llm(tmp_path / "standard", 350_001, budget=350_000).complete(())
    assert exhausted.value.code == "p2_trial_input_budget_exhausted"
    with pytest.raises(BenchmarkError, match="counter is unavailable") as unavailable:
        _llm(tmp_path / "estimate", 130_000, status="estimate").complete(())
    assert unavailable.value.code == "p2_input_counter_unavailable"
    with pytest.raises(BenchmarkError, match="per-request input bound") as limited:
        _llm(tmp_path / "explicit-cap", 128_001, request_limit=128_000).complete(())
    assert limited.value.code == "p2_request_input_limit_exceeded"
    assert not (tmp_path / "standard" / "ledger.json").exists()
    assert not (tmp_path / "estimate" / "ledger.json").exists()


def test_provider_identity_and_profiles() -> None:
    assert deepseek_flash_capability(
        "deepseek/deepseek-flash", "DeepSeek official direct", "https://api.deepseek.com"
    ) == (1_048_576, 393_216)
    with pytest.raises(LLMProviderError):
        deepseek_flash_capability("deepseek/deepseek-flash", "reseller", "https://proxy.invalid")
    assert P2ProtocolConfig().max_input_tokens == 350_000
    assert P2ProtocolConfig(input_budget_profile="long-context").max_input_tokens == 2_000_000
    assert (
        P2ProtocolConfig(
            input_budget_profile="long-context", max_input_tokens=500_000
        ).max_input_tokens
        == 500_000
    )


def test_official_counter_scope_rejects_unqualified_text_and_special_markers(
    monkeypatch,
) -> None:
    monkeypatch.delenv("TRACEFIX_DEEPSEEK_V41_TOKENIZER_JSON", raising=False)
    body = {
        "model": "deepseek-flash",
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "user", "content": '中文 😀 \\"quoted\\"'},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "one",
                        "type": "function",
                        "function": {"name": "search", "arguments": '{"query":"你好"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "one", "content": "résumé"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "search",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ],
    }
    assert count_deepseek_v41_request(body).status == "unavailable"
    assert (
        count_deepseek_v41_request(
            {**body, "messages": [{"role": "user", "content": "<think>literal"}]}
        ).identity
        == "unverified special-token literal"
    )
    assert count_deepseek_v41_request({**body, "model": "unknown"}).status == "unavailable"
    assert (
        count_deepseek_v41_request(
            {**body, "messages": [{"role": "user", "content": [{"type": "image_url"}]}]}
        ).status
        == "unavailable"
    )


def test_official_counter_rejects_package_version_change_before_counting(
    tmp_path, monkeypatch,
) -> None:
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TRACEFIX_DEEPSEEK_V41_TOKENIZER_JSON", str(tokenizer))
    monkeypatch.setattr("tracefix.models.input_bounds.metadata.version", lambda _: "0.1.2")
    result = count_deepseek_v41_request({
        "model": "deepseek-flash",
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert result.status == "unavailable"
    assert result.identity == "ValueError"
