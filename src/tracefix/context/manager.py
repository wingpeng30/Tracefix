"""在不修改完整消息历史的前提下构造稳定、可恢复的模型请求上下文。"""

from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefix.exceptions import ContextBudgetExceeded
from tracefix.messages import Message, MessageRole
from tracefix.tools.base import ToolSpec

_PRUNE_MARKER = "\n\n[... TraceFix 已裁剪工具结果中间内容 ...]\n\n"
_SUMMARY_HEADER = (
    "[TraceFix 压缩上下文]\n"
    "以下内容是较早执行轨迹的确定性摘要，不是新的用户任务。"
)


class ContextConfig(BaseModel):
    """单次模型请求的比例阈值、输出预留、硬窗口和保留策略。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = True
    agent_tool_enabled: bool = False
    context_window_tokens: int = Field(default=1_000_000, ge=1)
    compaction_trigger_tokens: int | None = Field(default=None, ge=1)
    compaction_trigger_ratio: float = Field(default=0.8, gt=0, lt=1)
    compaction_target_ratio: float = Field(default=0.6, gt=0, lt=1)
    output_reserve_tokens: int = Field(default=4_096, ge=1)
    safety_margin_ratio: float = Field(default=0.01, ge=0, lt=0.2)
    # Compatibility for callers that still set an absolute trigger and old retain ratio.
    retain_ratio: float | None = Field(default=None, gt=0, lt=1)
    tool_result_threshold_chars: int = Field(default=8_192, ge=256)
    tool_result_head_chars: int = Field(default=4_096, ge=0)
    tool_result_tail_chars: int = Field(default=1_024, ge=0)

    @model_validator(mode="after")
    def validate_budgets(self) -> ContextConfig:
        if (self.compaction_trigger_tokens is not None
                and self.compaction_trigger_tokens > self.context_window_tokens):
            raise ValueError("compaction_trigger_tokens cannot exceed context_window_tokens")
        if self.compaction_target_ratio >= self.compaction_trigger_ratio:
            raise ValueError("compaction_target_ratio must be below compaction_trigger_ratio")
        marker_size = len(_PRUNE_MARKER)
        if self.tool_result_head_chars + self.tool_result_tail_chars + marker_size >= (
            self.tool_result_threshold_chars
        ):
            raise ValueError("tool result head, tail and marker must fit below the threshold")
        return self

    def available_input_tokens(self, output_reserve_tokens: int | None = None) -> int:
        reserve = self.output_reserve_for_request(output_reserve_tokens)
        margin = math.ceil(self.context_window_tokens * self.safety_margin_ratio)
        return max(0, self.context_window_tokens - reserve - margin)

    def output_reserve_for_request(self, requested_output_tokens: int | None = None) -> int:
        """Reserve at least the request cap, allowing callers to choose more headroom."""
        return max(self.output_reserve_tokens, requested_output_tokens or 0)

    def trigger_tokens(self, output_reserve_tokens: int | None = None) -> int:
        capacity = self.available_input_tokens(output_reserve_tokens)
        if self.compaction_trigger_tokens is not None:
            return min(capacity, self.compaction_trigger_tokens)
        return max(1, math.floor(capacity * self.compaction_trigger_ratio))

    def target_tokens(self, output_reserve_tokens: int | None = None) -> int:
        capacity = self.available_input_tokens(output_reserve_tokens)
        if self.compaction_trigger_tokens is not None:
            trigger = min(capacity, self.compaction_trigger_tokens)
            ratio = self.retain_ratio or self.compaction_target_ratio
            return max(1, math.floor(trigger * ratio))
        return max(1, math.floor(capacity * self.compaction_target_ratio))


class ContextSessionState(BaseModel):
    """稳定压缩投影绑定到完整历史前缀、工具定义和压缩代次。"""

    model_config = ConfigDict(extra="forbid")

    projection: tuple[Message, ...] = ()
    history_watermark: int = Field(default=0, ge=0)
    history_sha256: str | None = None
    epoch: int = Field(default=0, ge=0)
    no_op_digest: str | None = None
    tokenizer_identity: str | None = None
    tool_specs: tuple[ToolSpec, ...] | None = None


class ContextMetrics(BaseModel):
    """一次 Agent 运行累计的压缩、投影复用及计数方法统计。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    preparation_count: int = Field(default=0, ge=0)
    compaction_count: int = Field(default=0, ge=0)
    tool_results_pruned: int = Field(default=0, ge=0)
    messages_compacted: int = Field(default=0, ge=0)
    batches_compacted: int = Field(default=0, ge=0)
    estimated_tokens_saved: int = Field(default=0, ge=0)
    projection_reuses: int = Field(default=0, ge=0)
    unique_pruned_results: int = Field(default=0, ge=0)
    unique_pruned_result_ids: list[str] = Field(default_factory=list)
    compaction_no_ops: int = Field(default=0, ge=0)
    count_method_counts: dict[str, int] = Field(default_factory=dict)
    tokenizer_identity: str | None = None

    def add_view(self, view: ContextView) -> None:
        self.preparation_count += 1
        self.compaction_count += int(view.compacted)
        self.tool_results_pruned += view.tool_results_pruned
        self.messages_compacted += view.messages_compacted
        self.batches_compacted += view.batches_compacted
        self.estimated_tokens_saved += max(
            0, view.estimated_tokens_before - view.estimated_tokens_after
        )
        self.projection_reuses += int(view.projection_reused)
        for call_id in view.pruned_tool_call_ids:
            if call_id not in self.unique_pruned_result_ids:
                self.unique_pruned_result_ids.append(call_id)
        self.unique_pruned_results = len(self.unique_pruned_result_ids)
        self.compaction_no_ops += int(view.compaction_no_op)
        self.count_method_counts[view.count_method] = (
            self.count_method_counts.get(view.count_method, 0) + 1
        )
        if view.tokenizer_identity is not None:
            self.tokenizer_identity = view.tokenizer_identity


class ContextView(BaseModel):
    """一次 LLM 请求实际使用的消息快照及其压缩指标。"""

    model_config = ConfigDict(extra="forbid")

    messages: tuple[Message, ...]
    estimated_tokens_before: int = Field(ge=0)
    estimated_tokens_after: int = Field(ge=0)
    original_message_count: int = Field(ge=0)
    request_message_count: int = Field(ge=0)
    tool_results_pruned: int = Field(default=0, ge=0)
    messages_compacted: int = Field(default=0, ge=0)
    batches_compacted: int = Field(default=0, ge=0)
    compacted: bool = False
    projection_reused: bool = False
    compaction_no_op: bool = False
    count_method: str = "tracefix-json-estimate"
    tokenizer_identity: str | None = None
    pruned_tool_call_ids: tuple[str, ...] = ()
    compaction_target_tokens: int | None = None
    compaction_target_met: bool | None = None
    compaction_target_reason: str | None = None


class ContextManager:
    """生成模型可见视图；完整审计历史保持不变。"""

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config or ContextConfig()
        self._measurement_cache: OrderedDict[str, tuple[int, str]] = OrderedDict()
        self._tokenizer_identity: str | None = None

    def prepare(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        *,
        state: ContextSessionState | None = None,
        force: bool = False,
        output_reserve_tokens: int | None = None,
        token_counter: Callable[[Sequence[Message], Sequence[ToolSpec]], Any] | None = None,
    ) -> ContextView:
        """构造稳定投影；只在触发边界重写旧上下文，其他轮次只追加。"""
        original = tuple(message.model_copy(deep=True) for message in messages)
        configured_specs = tuple(tools)
        validate_counter_for_session = (
            token_counter is not None and self._tokenizer_identity is None
        )
        if state is not None:
            if state.tokenizer_identity is not None:
                if (self._tokenizer_identity is not None
                        and self._tokenizer_identity != state.tokenizer_identity):
                    raise ContextBudgetExceeded("tokenizer identity changed within context session")
                self._tokenizer_identity = state.tokenizer_identity
            specs_changed = (
                state.tool_specs is not None
                and self._digest_specs(state.tool_specs)
                != self._digest_specs(configured_specs)
            )
            if specs_changed:
                raise ContextBudgetExceeded("tool schemas changed since context projection")
            state.tool_specs = configured_specs

        reused = bool(state is not None and state.history_watermark)
        if reused:
            assert state is not None
            if (len(original) < state.history_watermark
                    or self._digest(original[:state.history_watermark]) != state.history_sha256):
                raise ContextBudgetExceeded("history changed before its saved context projection")
            source = (*state.projection, *original[state.history_watermark:])
            tail_start = len(state.projection)
        else:
            source = original
            tail_start = 0

        count_method = "tracefix-json-estimate"
        measurements: dict[str, int] = {}

        def measure(
            view: Sequence[Message],
            schemas: Sequence[ToolSpec],
            *,
            validate_counter: bool = False,
        ) -> int:
            nonlocal count_method
            request_digest = self._digest_wire(view, schemas)
            counter_identity = state.tokenizer_identity if state is not None else None
            counter_identity = counter_identity or self._tokenizer_identity or "unbound"
            cache_key = f"{counter_identity}:{request_digest}"
            if cache_key not in measurements:
                cached = None if validate_counter else self._measurement_cache.get(cache_key)
                if cached is not None:
                    value, count_method = cached
                    self._measurement_cache.move_to_end(cache_key)
                else:
                    try:
                        value = token_counter(view, schemas) if token_counter is not None else None
                    except Exception:
                        value = None
                    if value is not None:
                        count_method = getattr(value, "method", "official-tokenizer-estimate")
                        status = getattr(value, "status", None)
                        reported_identity = getattr(value, "identity", None)
                        reported_identity = reported_identity or getattr(value, "method", None)
                        if reported_identity is None and token_counter is not None:
                            owner = getattr(token_counter, "__self__", None)
                            callable_name = getattr(
                                token_counter, "__qualname__", type(token_counter).__qualname__
                            )
                            owner_name = (
                                f"{type(owner).__module__}.{type(owner).__qualname__}"
                                if owner is not None else callable_name
                            )
                            reported_identity = owner_name
                        if status == "unavailable":
                            if reported_identity in {
                                "ValueError", "OSError", "FileNotFoundError", "ImportError",
                                "ModuleNotFoundError", "AttributeError", "RuntimeError",
                            }:
                                raise ContextBudgetExceeded(
                                    "tokenizer identity could not be verified",
                                    context={"tokenizer_identity": reported_identity},
                                )
                            if (state is not None and state.tokenizer_identity is not None
                                    and reported_identity == "official tokenizer not configured"):
                                raise ContextBudgetExceeded(
                                    "pinned tokenizer is no longer configured",
                                    context={"tokenizer_identity": state.tokenizer_identity},
                                )
                        elif reported_identity is not None:
                            if (state is not None and state.tokenizer_identity is not None
                                    and state.tokenizer_identity != reported_identity):
                                raise ContextBudgetExceeded(
                                    "tokenizer identity changed since context checkpoint",
                                    context={"saved": state.tokenizer_identity,
                                             "current": reported_identity},
                                )
                            if (self._tokenizer_identity is not None
                                    and self._tokenizer_identity != reported_identity):
                                raise ContextBudgetExceeded(
                                    "tokenizer identity changed within context session",
                                    context={"saved": self._tokenizer_identity,
                                             "current": reported_identity},
                                )
                            self._tokenizer_identity = reported_identity
                            if state is not None:
                                state.tokenizer_identity = reported_identity
                            counter_identity = reported_identity
                            cache_key = f"{counter_identity}:{request_digest}"
                        value = getattr(value, "tokens", value)
                    if type(value) is not int or value < 0:
                        count_method = ("tracefix-json-estimate" if token_counter is None
                                        else "tracefix-json-estimate-fallback")
                        value = self.estimate_tokens(view, schemas)
                    self._measurement_cache[cache_key] = (value, count_method)
                    self._measurement_cache.move_to_end(cache_key)
                    while len(self._measurement_cache) > 512:
                        self._measurement_cache.popitem(last=False)
                measurements[cache_key] = value
            return measurements[cache_key]

        reserve = (self.config.output_reserve_tokens if output_reserve_tokens is None
                   else output_reserve_tokens)
        capacity = self.config.available_input_tokens(reserve)
        trigger = self.config.trigger_tokens(reserve)
        target = self.config.target_tokens(reserve)
        before = measure(
            source, configured_specs, validate_counter=validate_counter_for_session
        )
        if not self.config.enabled:
            self._ensure_hard_limit(before, reserve)
            return self._view(source, before, before, original_message_count=len(original),
                              projection_reused=reused, count_method=count_method,
                              tokenizer_identity=self._tokenizer_identity)

        prefix = source[:tail_start]
        pruned_tail, pruned_ids = self._prune_tool_results(source[tail_start:])
        candidate = (*prefix, *pruned_tail)
        candidate_tokens = measure(candidate, configured_specs)
        due = force or candidate_tokens >= trigger or candidate_tokens > capacity
        digest = self._digest(original)
        if not due:
            self._ensure_hard_limit(candidate_tokens, reserve)
            self._save_projection(state, candidate, original, configured_specs)
            return self._view(
                candidate, before, candidate_tokens, original_message_count=len(original),
                tool_results_pruned=len(pruned_ids), pruned_tool_call_ids=pruned_ids,
                projection_reused=reused, count_method=count_method,
                tokenizer_identity=self._tokenizer_identity,
            )

        repeated_no_op = state is not None and state.no_op_digest == digest
        if repeated_no_op:
            request, removed, batches = candidate, 0, 0
        else:
            request, removed, batches = self._compact(candidate, target, configured_specs, measure)
        after = measure(request, configured_specs)
        no_op = request == candidate
        self._ensure_hard_limit(after, reserve)
        self._save_projection(state, request, original, configured_specs)
        if state is not None:
            if no_op:
                state.no_op_digest = digest
            else:
                state.epoch += 1
                state.no_op_digest = None
        return self._view(
            request, before, after, original_message_count=len(original),
            tool_results_pruned=len(pruned_ids), pruned_tool_call_ids=pruned_ids,
            messages_compacted=removed, batches_compacted=batches, compacted=not no_op,
            projection_reused=reused, compaction_no_op=no_op, count_method=count_method,
            tokenizer_identity=self._tokenizer_identity,
            compaction_target_tokens=target, compaction_target_met=after <= target,
            compaction_target_reason=(
                None if after <= target else "minimum_safe_projection_exceeds_target"
            ),
        )

    @staticmethod
    def _save_projection(state, projection, original, tools) -> None:
        if state is None:
            return
        state.projection = tuple(projection)
        state.history_watermark = len(original)
        state.history_sha256 = ContextManager._digest(original)
        state.tool_specs = tuple(tools)

    def _compact(self, messages, target, tools, measure):
        fixed, units = self._split_batches(tuple(messages))
        latest_test = next((index for index in range(len(units) - 1, -1, -1)
                            if self._unit_is_test(units[index])), None)
        protected = {
            index for index, unit in enumerate(units)
            if len(unit) == 1 and (
                (
                    unit[0].role is MessageRole.USER
                    and unit[0].metadata.get("kind") not in {
                        "context_summary", "task_fact_anchor",
                    }
                    and (
                        not unit[0].metadata.get("kind")
                        or unit[0].metadata.get("kind") in {
                            "skill_catalog", "skill_instructions", "repository_map",
                        }
                    )
                )
                or (unit[0].role is MessageRole.SYSTEM
                    and not unit[0].metadata.get("tracefix_context_summary")
                    and unit[0].metadata.get("kind") != "task_fact_anchor")
            )
        }
        facts = [index for index, unit in enumerate(units)
                 if len(unit) == 1 and unit[0].metadata.get("kind") == "task_fact_anchor"]
        if facts:
            protected.add(facts[-1])
        if latest_test is not None:
            protected.add(latest_test)

        pinned = [unit for index, unit in enumerate(units) if index in protected]
        pinned_tokens = measure((*fixed, *(message for unit in pinned for message in unit)), tools)
        remaining_target = max(1, target - pinned_tokens)
        eligible = [index for index in range(len(units)) if index not in protected]
        retained: set[int] = set()
        retained_tokens = 0
        for index in reversed(eligible):
            cost = measure(units[index], ())
            if retained and retained_tokens + cost > remaining_target:
                break
            retained.add(index)
            retained_tokens += cost

        output = list(fixed)
        pending: list[tuple[Message, ...]] = []
        removed_messages = removed_batches = 0

        def flush() -> None:
            nonlocal removed_messages, removed_batches
            if not pending:
                return
            output.append(self._summarize_batches(tuple(pending)))
            removed_messages += sum(len(unit) for unit in pending)
            removed_batches += len(pending)
            pending.clear()

        for index, unit in enumerate(units):
            if index in protected or index in retained:
                flush()
                output.extend(unit)
            else:
                pending.append(unit)
        flush()
        return tuple(output), removed_messages, removed_batches

    @staticmethod
    def _unit_is_test(unit: tuple[Message, ...]) -> bool:
        return bool(unit and unit[0].role is MessageRole.ASSISTANT
                    and any(call.name == "run_tests" for call in unit[0].tool_calls))

    @staticmethod
    def estimate_tokens(messages: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> int:
        """按供应商请求 JSON 大小估算 Token；不是计费或硬上限证明。"""
        payload = {
            "messages": [ContextManager._wire_message(message) for message in messages],
            "tools": [tool.to_openai_tool() for tool in tools],
        }
        byte_count = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())
        return math.ceil(byte_count / 4)

    def _prune_tool_results(self, messages):
        result: list[Message] = []
        pruned_ids: list[str] = []
        for message in messages:
            if (message.role is not MessageRole.TOOL or message.content is None
                    or len(message.content) <= self.config.tool_result_threshold_chars):
                result.append(message)
                continue
            content = self._prune_tool_content(message.content)
            metadata = dict(message.metadata)
            metadata["context_pruned"] = True
            result.append(message.model_copy(update={"content": content, "metadata": metadata},
                                             deep=True))
            if message.tool_call_id:
                pruned_ids.append(message.tool_call_id)
        return tuple(result), tuple(pruned_ids)

    def _prune_tool_content(self, content: str) -> str:
        head = self.config.tool_result_head_chars
        tail = self.config.tool_result_tail_chars
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return self._head_tail(content, head, tail)
        if not isinstance(payload, dict):
            return self._head_tail(content, head, tail)
        output_text = json.dumps(payload.get("output"), ensure_ascii=False, separators=(",", ":"))
        payload["output"] = {
            "_tracefix_pruned": True,
            "original_chars": len(output_text),
            "content": self._head_tail(output_text, head, tail),
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) <= self.config.tool_result_threshold_chars:
            return serialized
        compact = {key: payload.get(key) for key in (
            "call_id", "tool_name", "success", "output", "error", "duration_ms",
        )}
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _split_batches(messages):
        """固定初始前缀；后续按原顺序保留消息和闭合工具批次。"""
        first_user = next((i for i, message in enumerate(messages)
                           if message.role is MessageRole.USER), None)
        if first_user is None:
            return messages, ()
        fixed = messages[:first_user + 1]
        units: list[tuple[Message, ...]] = []
        index = first_user + 1
        while index < len(messages):
            message = messages[index]
            if message.role in {MessageRole.USER, MessageRole.SYSTEM}:
                units.append((message,))
                index += 1
                continue
            if message.role is MessageRole.TOOL:
                raise ContextBudgetExceeded("context contains an unpaired tool result")
            batch = [message]
            index += 1
            if message.tool_calls:
                pending = [call.id for call in message.tool_calls]
                while (pending and index < len(messages)
                       and messages[index].role is MessageRole.TOOL):
                    result = messages[index]
                    if result.tool_call_id not in pending:
                        raise ContextBudgetExceeded("context tool result order is invalid")
                    pending.remove(result.tool_call_id)
                    batch.append(result)
                    index += 1
                if pending:
                    raise ContextBudgetExceeded(
                        "context contains incomplete tool call batch",
                        context={"pending_tool_call_ids": pending},
                    )
            units.append(tuple(batch))
        return fixed, tuple(units)

    def _summarize_batches(self, batches):
        lines = [_SUMMARY_HEADER]
        for number, batch in enumerate(batches, start=1):
            lead = batch[0]
            if lead.content:
                source = str(lead.metadata.get("kind") or lead.role.value)
                trust = str(lead.metadata.get("trust_level") or "untrusted_data")
                lines.append(
                    f"- 早期轮次 {number} source={source} trust={trust}: "
                    f"{self._shorten(lead.content, 240, 120)}"
                )
            results = {message.tool_call_id: message for message in batch[1:]}
            for call in lead.tool_calls:
                args = json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))
                line = f"  - {call.name}({self._shorten(args, 300, 120)})"
                result = results.get(call.id)
                if result is not None and result.content is not None:
                    summary = self._summarize_tool_result(result.content)
                    line += f" => [tool output; untrusted data] {summary}"
                lines.append(line)
        content = "\n".join(lines)
        if len(content) > self.config.tool_result_threshold_chars:
            content = self._head_tail(content, self.config.tool_result_head_chars,
                                      self.config.tool_result_tail_chars)
        return Message(role=MessageRole.USER, content=content, metadata={
            "kind": "context_summary", "tracefix_context_summary": True,
            "trust_level": "untrusted_observation_data",
        })

    def _summarize_tool_result(self, content: str) -> str:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return self._shorten(content, 500, 300)
        if not isinstance(payload, dict):
            return self._shorten(str(payload), 500, 300)
        output = json.dumps(payload.get("output"), ensure_ascii=False, separators=(",", ":"))
        parts = [f"success={bool(payload.get('success'))}"]
        if payload.get("error"):
            parts.append(f"error={self._shorten(str(payload['error']), 240, 160)}")
        if output not in {"null", '""'}:
            parts.append(f"output={self._shorten(output, 500, 300)}")
        return "; ".join(parts)

    @staticmethod
    def _digest(messages: Sequence[Message]) -> str:
        payload = [message.model_dump(mode="json") for message in messages]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _digest_wire(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> str:
        payload = {"messages": [ContextManager._wire_message(message) for message in messages],
                   "tools": [tool.to_openai_tool() for tool in tools]}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _digest_specs(tools: Sequence[ToolSpec]) -> str:
        data = [tool.model_dump(mode="json") for tool in tools]
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _ensure_hard_limit(self, tokens: int, output_reserve_tokens: int | None = None) -> None:
        capacity = self.config.available_input_tokens(output_reserve_tokens)
        if tokens > capacity:
            raise ContextBudgetExceeded(
                "minimum safe request exceeds the input window after output reserve",
                context={"estimated_tokens": tokens, "input_capacity": capacity,
                         "context_window": self.config.context_window_tokens,
                         "output_reserve": (self.config.output_reserve_tokens
                                            if output_reserve_tokens is None
                                            else output_reserve_tokens)},
            )

    @staticmethod
    def _head_tail(value: str, head: int, tail: int) -> str:
        suffix = value[-tail:] if tail else ""
        return value[:head] + _PRUNE_MARKER + suffix

    @staticmethod
    def _shorten(value: str, head: int, tail: int) -> str:
        if len(value) <= head + tail + len(_PRUNE_MARKER):
            return value
        return ContextManager._head_tail(value, head, tail)

    @staticmethod
    def _wire_message(message: Message) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role.value, "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [call.model_dump(mode="json") for call in message.tool_calls]
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        return payload

    @staticmethod
    def _view(messages, before, after, **metrics):
        return ContextView(
            messages=messages, estimated_tokens_before=before, estimated_tokens_after=after,
            original_message_count=metrics.pop("original_message_count", len(messages)),
            request_message_count=len(messages), **metrics,
        )
