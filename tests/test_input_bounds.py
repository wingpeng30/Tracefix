"""Zero-cost admission checks for the new provider-scoped P2 boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tracefix.exceptions import BenchmarkError, LLMProviderError
from tracefix.messages import Message, MessageRole
from tracefix.models.base import LLMConfig, LLMResponse, TokenUsage
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
        def __init__(self):
            self.config = config

        def count_input_bound(self, messages, tools=()):
            return InputBound(
                tokens, status, "test-qualified-counter", "fixture", "a" * 64
            )

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


def test_output_budget_exhaustion_stops_before_reservation(tmp_path) -> None:
    llm = _llm(tmp_path, 100)
    llm._trial_output_used = llm._trial_output_budget

    with pytest.raises(BenchmarkError, match="trial token budget"):
        llm.complete(())

    assert not (tmp_path / "ledger.json").exists()


def test_total_cost_cap_rejects_reserved_request_before_send(tmp_path) -> None:
    config = LLMConfig(
        model_name="deepseek/deepseek-flash",
        max_output_tokens=4096,
        max_retries=0,
        extra_kwargs={
            "api_base": "https://api.deepseek.com",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )

    class Delegate:
        def __init__(self):
            self.config = config

        def count_input_tokens(self, _messages, _tools=()):
            return 100

        def complete(self, *_args):
            raise AssertionError("cost cap must stop before provider invocation")

    llm = P2BudgetedLLM(
        config,
        ledger_path=tmp_path / "ledger.json",
        formal=_formal().model_copy(update={"total_cost_cap_usd": 0.000001}),
        input_upper_bound=1000,
        legacy_input_counting=True,
        trial_output_budget=20_000,
    )
    llm._delegate = Delegate()
    llm._trial_output_used = 19_950

    with pytest.raises(BenchmarkError, match="total cost cap"):
        llm.complete(())

    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["halt_reason"] == "cost_cap_would_be_exceeded"
    assert ledger["request_count"] == 0
    assert ledger["uncertain_request"] is False


def test_reserved_request_ledger_records_final_request_hash(tmp_path) -> None:
    with pytest.raises(AssertionError, match="must not invoke provider"):
        _llm(tmp_path, 100).complete(())
    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["requests"][0]["request_sha256"] == "a" * 64


def test_compatibility_send_uses_remaining_output_bound_and_restores_config(tmp_path) -> None:
    config = LLMConfig(
        model_name="deepseek/deepseek-flash",
        max_output_tokens=4096,
        max_retries=0,
        extra_kwargs={
            "api_base": "https://api.deepseek.com",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )
    sent_bounds = []

    class Delegate:
        def __init__(self):
            self.config = config

        def count_input_tokens(self, _messages, _tools=()):
            return 100

        def complete(self, _messages, _tools=()):
            sent_bounds.append(self.config.max_output_tokens)
            return LLMResponse(
                message=Message(role=MessageRole.ASSISTANT, content="ok"),
                usage=TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120),
                model_name="deepseek/deepseek-flash",
                raw_response={
                    "model": "deepseek-flash",
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "prompt_cache_hit_tokens": 0,
                        "prompt_cache_miss_tokens": 100,
                    },
                },
            )

    llm = P2BudgetedLLM(
        config,
        ledger_path=tmp_path / "ledger.json",
        formal=_formal(),
        input_upper_bound=1000,
        legacy_input_counting=True,
        trial_output_budget=20_000,
    )
    llm._delegate = Delegate()
    llm._trial_output_used = 19_950
    llm.complete(())
    assert llm._delegate.config.max_output_tokens == 4096
    llm.complete(())
    assert sent_bounds == [50, 30]
    assert llm._delegate.config.max_output_tokens == 4096
    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert [row["output_token_upper_bound"] for row in ledger["requests"]] == [50, 30]


def test_prepared_request_binds_count_reservation_and_frozen_send(tmp_path) -> None:
    config = LLMConfig(
        model_name="deepseek/deepseek-flash",
        max_output_tokens=4096,
        max_retries=0,
        extra_kwargs={
            "api_base": "https://api.deepseek.com",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )
    prepared = SimpleNamespace(request_sha256="b" * 64)
    observed = []

    def response():
        return LLMResponse(
            message=Message(role=MessageRole.ASSISTANT, content="ok"),
            usage=TokenUsage(input_tokens=100, output_tokens=20, total_tokens=120),
            model_name="deepseek/deepseek-flash",
            raw_response={
                "model": "deepseek-flash",
                "usage": {
                    "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
                    "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100,
                },
            },
        )

    class PreparedDelegate:
        def __init__(self):
            self.config = config

        def count_input_bound(self, _messages, _tools=()):
            return InputBound(100, "verified_upper_bound", "fixture", "fixture", "a" * 64)

        def prepare_request(self, _messages, _tools=()):
            observed.append(("prepare", self.config.max_output_tokens))
            return prepared

        def complete_prepared(self, request):
            observed.append(("send", self.config.max_output_tokens, request is prepared))
            return response()

        def complete(self, *_args):
            raise AssertionError("a frozen request must use complete_prepared")

    llm = P2BudgetedLLM(
        config,
        ledger_path=tmp_path / "ledger.json",
        formal=_formal(),
        input_upper_bound=1000,
        trial_output_budget=20_000,
    )
    llm._delegate = PreparedDelegate()
    llm._trial_output_used = 19_950

    llm.complete(())

    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert observed == [("prepare", 50), ("send", 4096, True)]
    assert llm._delegate.config.max_output_tokens == 4096
    assert ledger["requests"][0]["output_token_upper_bound"] == 50
    assert ledger["requests"][0]["request_sha256"] == "b" * 64


def test_preparation_failure_restores_config_before_any_reservation(tmp_path) -> None:
    config = LLMConfig(
        model_name="deepseek/deepseek-flash",
        max_output_tokens=4096,
        max_retries=0,
        extra_kwargs={
            "api_base": "https://api.deepseek.com",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )

    class FailingPreparer:
        def __init__(self):
            self.config = config

        def count_input_bound(self, _messages, _tools=()):
            return InputBound(100, "verified_upper_bound", "fixture", "fixture", "a" * 64)

        def prepare_request(self, _messages, _tools=()):
            assert self.config.max_output_tokens == 50
            raise RuntimeError("request preparation failed")

        def complete(self, *_args):
            raise AssertionError("failed preparation must not send")

    llm = P2BudgetedLLM(
        config,
        ledger_path=tmp_path / "ledger.json",
        formal=_formal(),
        input_upper_bound=1000,
        trial_output_budget=20_000,
    )
    llm._delegate = FailingPreparer()
    llm._trial_output_used = 19_950

    with pytest.raises(RuntimeError, match="preparation failed"):
        llm.complete(())

    assert llm._delegate.config.max_output_tokens == 4096
    assert not (tmp_path / "ledger.json").exists()


def test_compatibility_send_refuses_unbounded_delegate_before_reservation(tmp_path) -> None:
    llm = P2BudgetedLLM(
        LLMConfig(
            model_name="deepseek/deepseek-flash", max_output_tokens=4096, max_retries=0,
            extra_kwargs={
                "api_base": "https://api.deepseek.com",
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        ),
        ledger_path=tmp_path / "ledger.json",
        formal=_formal(),
        input_upper_bound=1000,
        legacy_input_counting=True,
    )

    class UnconfigurableDelegate:
        def count_input_tokens(self, _messages, _tools=()):
            return 100

        def complete(self, _messages, _tools=()):
            raise AssertionError("unbounded provider call must be rejected")

    llm._delegate = UnconfigurableDelegate()
    with pytest.raises(BenchmarkError, match="cannot enforce"):
        llm.complete(())
    assert not (tmp_path / "ledger.json").exists()
    # Legacy adapters may expose no cleanup hook.
    llm.close()


def test_compatibility_send_restores_config_after_unknown_outcome_without_retry(tmp_path):
    config = LLMConfig(
        model_name="deepseek/deepseek-flash",
        max_output_tokens=4096,
        max_retries=0,
        extra_kwargs={
            "api_base": "https://api.deepseek.com",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )
    calls = []

    class FailingDelegate:
        def __init__(self):
            self.config = config

        def count_input_tokens(self, _messages, _tools=()):
            return 100

        def complete(self, _messages, _tools=()):
            calls.append(self.config.max_output_tokens)
            raise TimeoutError("provider result is unknown")

    llm = P2BudgetedLLM(
        config,
        ledger_path=tmp_path / "ledger.json",
        formal=_formal(),
        input_upper_bound=1000,
        legacy_input_counting=True,
    )
    llm._delegate = FailingDelegate()
    llm._trial_output_used = 19_950
    with pytest.raises(TimeoutError, match="unknown"):
        llm.complete(())
    assert calls == [50]
    assert llm._delegate.config.max_output_tokens == 4096
    with pytest.raises(BenchmarkError, match="uncertain request"):
        llm.complete(())
    assert calls == [50]


def test_provider_response_above_reserved_output_cap_is_durable_and_halts(tmp_path):
    config = LLMConfig(
        model_name="deepseek/deepseek-flash",
        max_output_tokens=4096,
        max_retries=0,
        extra_kwargs={
            "api_base": "https://api.deepseek.com",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )

    class Delegate:
        def __init__(self):
            self.config = config

        def count_input_tokens(self, _messages, _tools=()):
            return 100

        def complete(self, _messages, _tools=()):
            assert self.config.max_output_tokens == 50
            return LLMResponse(
                message=Message(role=MessageRole.ASSISTANT, content="too long"),
                usage=TokenUsage(input_tokens=100, output_tokens=51, total_tokens=151),
                model_name="deepseek/deepseek-flash",
                raw_response={
                    "model": "deepseek-flash",
                    "usage": {
                        "prompt_tokens": 100, "completion_tokens": 51,
                        "total_tokens": 151, "prompt_cache_hit_tokens": 0,
                        "prompt_cache_miss_tokens": 100,
                    },
                },
            )

    llm = P2BudgetedLLM(
        config,
        ledger_path=tmp_path / "ledger.json",
        formal=_formal(),
        input_upper_bound=1000,
        legacy_input_counting=True,
        trial_output_budget=20_000,
    )
    delegate = Delegate()
    llm._delegate = delegate
    llm._trial_output_used = 19_950

    with pytest.raises(BenchmarkError, match="persisted request bound"):
        llm.complete(())

    ledger = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert delegate.config.max_output_tokens == 4096
    assert ledger["requests"][0]["status"] == "response_received_bound_exceeded"
    assert ledger["requests"][0]["output_token_upper_bound"] == 50
    assert ledger["requests"][0]["actual_output_tokens"] == 51
    assert ledger["halt_reason"] == "provider_usage_exceeded_request_bound"


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
    missing_tokenizer = count_deepseek_v41_request(body)
    assert len(missing_tokenizer.request_sha256 or "") == 64
    assert missing_tokenizer.request_sha256 == count_deepseek_v41_request(body).request_sha256
    assert (
        count_deepseek_v41_request(
            {**body, "messages": [{"role": "user", "content": "<think>literal"}]}
        ).identity
        == "unverified special-token literal"
    )
    assert count_deepseek_v41_request({**body, "model": "unknown"}).status == "unavailable"
    assert count_deepseek_v41_request({**body, "unknown_parameter": True}).identity == (
        "unsupported request fields"
    )
    assert count_deepseek_v41_request({**body, "messages": []}).identity == "invalid messages"
    assert count_deepseek_v41_request(
        {**body, "messages": [{"role": "system", "content": "x", "unexpected": 1}]}
    ).identity == "invalid message structure"
    assert count_deepseek_v41_request(
        {**body, "messages": [{"role": "developer", "content": "x"}]}
    ).identity == "invalid message role"
    assert count_deepseek_v41_request({**body, "tools": [None]}).identity == (
        "invalid tool schema"
    )
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


@pytest.mark.parametrize(
    ("field", "value", "identity"),
    [
        ("tool_calls", {}, "invalid tool calls"),
        ("tool_calls", [{"id": "x"}], "invalid tool call structure"),
    ],
)
def test_official_counter_rejects_malformed_tool_calls(field, value, identity) -> None:
    body = {
        "model": "deepseek-flash",
        "thinking": {"type": "disabled"},
        "messages": [{"role": "assistant", "content": None, field: value}],
    }
    assert count_deepseek_v41_request(body).identity == identity
    assert count_deepseek_v41_request(
        {
            "model": "deepseek-flash",
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": "x"}],
            "tools": None,
        }
    ).identity == "invalid tool schemas"
    assert count_deepseek_v41_request(
        {
            "model": "deepseek-flash",
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "x", "parameters": []}}],
        }
    ).identity == "invalid tool schema"


@pytest.mark.parametrize(("encoded", "expected"), [([1, 2, 3], "estimate"), ([], "unavailable")])
def test_official_counter_uses_frozen_renderer_as_estimate(
    tmp_path, monkeypatch, encoded, expected
) -> None:
    import hashlib

    import tracefix.models.input_bounds as bounds

    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text("fixture tokenizer", encoding="utf-8")
    digest = hashlib.sha256(tokenizer.read_bytes()).hexdigest()
    monkeypatch.setenv("TRACEFIX_DEEPSEEK_V41_TOKENIZER_JSON", str(tokenizer))
    monkeypatch.setattr(bounds, "V41_TOKENIZER_SHA256", digest)
    monkeypatch.setattr(bounds.metadata, "version", lambda _: bounds.V41_RECIPE_VERSION)

    class Encoding:
        def with_tokenizer(self, _tokenizer):
            return self

        def encode(self, conversation):
            assert conversation == "rendered fixture"
            return encoded

    class Request:
        def __init__(self, payload):
            self.payload = payload

        def convert(self, _options):
            return SimpleNamespace(conversation="rendered fixture")

    recipe = SimpleNamespace(
        ChatCompletionRequest=Request,
        ConversionOptions=lambda: object(),
        Tokenizer=SimpleNamespace(from_file=lambda _path: object()),
        DeepseekV41Encoding=Encoding,
    )
    monkeypatch.setattr(bounds.importlib, "import_module", lambda _name: recipe)
    result = bounds.count_deepseek_v41_request(
        {
            "model": "deepseek-flash",
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": "中文 😀"}],
            "tools": [],
        }
    )
    assert result.status == expected
    assert result.tokens == (len(encoded) if encoded else None)
    assert result.request_sha256 and len(result.request_sha256) == 64
    if expected == "estimate":
        assert result.identity == digest
        monkeypatch.setattr(bounds, "V41_TOKENIZER_SHA256", "0" * 64)
        mismatch = bounds.count_deepseek_v41_request(
            {
                "model": "deepseek-flash",
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": "中文 😀"}],
            }
        )
        assert mismatch.status == "unavailable"
        assert mismatch.identity == "ValueError"


def test_official_counter_rejects_unserializable_request_hash() -> None:
    result = count_deepseek_v41_request({"unserializable": object()})
    assert result.status == "unavailable"
    assert result.identity == "non-serializable request"
