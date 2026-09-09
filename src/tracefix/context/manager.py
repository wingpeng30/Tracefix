"""在不修改完整消息历史的前提下构造紧凑的模型请求视图。"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
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
    """单次模型请求的软压缩阈值、硬窗口和保留策略。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = True
    context_window_tokens: int = Field(default=1_000_000, ge=1)
    compaction_trigger_tokens: int = Field(default=32_000, ge=1)
    retain_ratio: float = Field(default=0.375, gt=0, lt=1)
    tool_result_threshold_chars: int = Field(default=8_192, ge=256)
    tool_result_head_chars: int = Field(default=4_096, ge=0)
    tool_result_tail_chars: int = Field(default=1_024, ge=0)

    @model_validator(mode="after")
    def validate_budgets(self) -> ContextConfig:
        """拒绝不会缩小内容或超过模型硬窗口的配置。"""
        if self.compaction_trigger_tokens > self.context_window_tokens:
            raise ValueError("compaction_trigger_tokens cannot exceed context_window_tokens")
        marker_size = len(_PRUNE_MARKER)
        if self.tool_result_head_chars + self.tool_result_tail_chars + marker_size >= (
            self.tool_result_threshold_chars
        ):
            raise ValueError("tool result head, tail and marker must fit below the threshold")
        return self


class ContextMetrics(BaseModel):
    """一次 Agent 运行累计的上下文压缩统计。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    preparation_count: int = Field(default=0, ge=0)
    compaction_count: int = Field(default=0, ge=0)
    tool_results_pruned: int = Field(default=0, ge=0)
    messages_compacted: int = Field(default=0, ge=0)
    batches_compacted: int = Field(default=0, ge=0)
    estimated_tokens_saved: int = Field(default=0, ge=0)

    def add_view(self, view: ContextView) -> None:
        """把一次请求视图的指标累计到运行级统计。"""
        self.preparation_count += 1
        self.compaction_count += int(view.compacted)
        self.tool_results_pruned += view.tool_results_pruned
        self.messages_compacted += view.messages_compacted
        self.batches_compacted += view.batches_compacted
        self.estimated_tokens_saved += max(
            0, view.estimated_tokens_before - view.estimated_tokens_after
        )


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


class ContextManager:
    """生成模型可见视图；调用方仍持有完整、不可损失的历史。"""

    def __init__(self, config: ContextConfig | None = None) -> None:
        self.config = config or ContextConfig()

    def prepare(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
    ) -> ContextView:
        """裁剪工具输出，并在达到软阈值时折叠较早的完整工具批次。"""
        original = tuple(message.model_copy(deep=True) for message in messages)
        before = self.estimate_tokens(original, tools)
        if not self.config.enabled:
            self._ensure_hard_limit(before)
            return self._view(
                original, before, before, original_message_count=len(original)
            )

        pruned, prune_count = self._prune_tool_results(original)
        pruned_tokens = self.estimate_tokens(pruned, tools)
        if pruned_tokens < self.config.compaction_trigger_tokens:
            self._ensure_hard_limit(pruned_tokens)
            return self._view(
                pruned,
                before,
                pruned_tokens,
                tool_results_pruned=prune_count,
                original_message_count=len(original),
            )

        anchors, batches = self._split_batches(pruned)
        retained, compacted_batches = self._select_recent_batches(batches, tools)
        if not compacted_batches:
            self._ensure_hard_limit(pruned_tokens)
            return self._view(
                pruned,
                before,
                pruned_tokens,
                tool_results_pruned=prune_count,
                original_message_count=len(original),
            )

        summary = self._summarize_batches(compacted_batches)
        request = (*anchors, summary, *(message for batch in retained for message in batch))
        after = self.estimate_tokens(request, tools)
        if after >= pruned_tokens:
            # 确定性摘要也必须真正缩小请求；否则保留裁剪视图更安全。
            return self._view(
                pruned,
                before,
                pruned_tokens,
                tool_results_pruned=prune_count,
                original_message_count=len(original),
            )
        self._ensure_hard_limit(after)
        compacted_messages = sum(len(batch) for batch in compacted_batches)
        return self._view(
            request,
            before,
            after,
            tool_results_pruned=prune_count,
            messages_compacted=compacted_messages,
            batches_compacted=len(compacted_batches),
            compacted=True,
            original_message_count=len(original),
        )

    @staticmethod
    def estimate_tokens(messages: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> int:
        """按供应商实际接收的近似 JSON 大小估算 Token，不用于计费。"""
        payload = {
            "messages": [ContextManager._wire_message(message) for message in messages],
            "tools": [tool.to_openai_tool() for tool in tools],
        }
        byte_count = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        return math.ceil(byte_count / 4)

    def _prune_tool_results(
        self, messages: tuple[Message, ...]
    ) -> tuple[tuple[Message, ...], int]:
        """只替换模型请求副本中的超长 tool content，保留合法 JSON 外壳。"""
        result: list[Message] = []
        count = 0
        for message in messages:
            if message.role is not MessageRole.TOOL or message.content is None:
                result.append(message)
                continue
            if len(message.content) <= self.config.tool_result_threshold_chars:
                result.append(message)
                continue
            content = self._prune_tool_content(message.content)
            result.append(message.model_copy(update={"content": content}, deep=True))
            count += 1
        return tuple(result), count

    def _prune_tool_content(self, content: str) -> str:
        """优先保留 ToolResult 顶层字段；无法解析时退化为普通首尾裁剪。"""
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
        # metadata 偶尔也可能很大；第二次收敛只保留执行语义所需的顶层字段。
        compact = {
            key: payload.get(key)
            for key in ("call_id", "tool_name", "success", "output", "error", "duration_ms")
        }
        return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

    def _select_recent_batches(
        self,
        batches: tuple[tuple[Message, ...], ...],
        tools: Sequence[ToolSpec],
    ) -> tuple[tuple[tuple[Message, ...], ...], tuple[tuple[Message, ...], ...]]:
        """从末尾按完整批次保留软阈值一定比例的近期历史。"""
        target = max(1, int(self.config.compaction_trigger_tokens * self.config.retain_ratio))
        retained_reversed: list[tuple[Message, ...]] = []
        used = 0
        for batch in reversed(batches):
            batch_tokens = self.estimate_tokens(batch, tools)
            if retained_reversed and used + batch_tokens > target:
                break
            retained_reversed.append(batch)
            used += batch_tokens
        retained_count = len(retained_reversed)
        retained_start = len(batches) - retained_count
        failed_test_index = self._latest_failed_test_batch(batches)
        if failed_test_index is not None:
            # 最新失败测试是下一步修复最重要的证据，即使超出软保留量也原样保留。
            retained_start = min(retained_start, failed_test_index)
        retained = batches[retained_start:]
        compacted_count = retained_start
        return retained, batches[:compacted_count]

    @staticmethod
    def _latest_failed_test_batch(
        batches: tuple[tuple[Message, ...], ...],
    ) -> int | None:
        """返回最近一个失败 run_tests 所在批次，无法解析时不作错误推断。"""
        for index in range(len(batches) - 1, -1, -1):
            batch = batches[index]
            if not batch or not any(call.name == "run_tests" for call in batch[0].tool_calls):
                continue
            for message in batch[1:]:
                if message.role is not MessageRole.TOOL or not message.content:
                    continue
                try:
                    payload = json.loads(message.content)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and payload.get("tool_name") == "run_tests":
                    if payload.get("success") is False:
                        return index
        return None

    @staticmethod
    def _split_batches(
        messages: tuple[Message, ...]
    ) -> tuple[tuple[Message, ...], tuple[tuple[Message, ...], ...]]:
        """提取 system/首个 user 锚点，并让工具调用和结果保持为原子批次。"""
        anchor_indexes: set[int] = set()
        for index, message in enumerate(messages):
            if message.role is MessageRole.SYSTEM:
                anchor_indexes.add(index)
        first_user = next(
            (index for index, message in enumerate(messages) if message.role is MessageRole.USER),
            None,
        )
        if first_user is not None:
            anchor_indexes.add(first_user)
        anchors = tuple(message for i, message in enumerate(messages) if i in anchor_indexes)
        remainder = [message for i, message in enumerate(messages) if i not in anchor_indexes]

        batches: list[tuple[Message, ...]] = []
        index = 0
        while index < len(remainder):
            message = remainder[index]
            batch = [message]
            index += 1
            if message.role is MessageRole.ASSISTANT and message.tool_calls:
                expected = {call.id for call in message.tool_calls}
                while index < len(remainder) and remainder[index].role is MessageRole.TOOL:
                    tool_message = remainder[index]
                    batch.append(tool_message)
                    index += 1
                    expected.discard(tool_message.tool_call_id)
                # MessageHistory 已保证历史闭合；这里保留防御式检查，避免外部直接传入坏序列。
                if expected:
                    raise ContextBudgetExceeded(
                        "context contains incomplete tool call batch",
                        context={"pending_tool_call_ids": sorted(expected)},
                    )
            batches.append(tuple(batch))
        return anchors, tuple(batches)

    def _summarize_batches(self, batches: tuple[tuple[Message, ...], ...]) -> Message:
        """把较早批次投影成可审计、无推理补全的结构化文本。"""
        lines = [_SUMMARY_HEADER]
        for number, batch in enumerate(batches, start=1):
            assistant = batch[0]
            if assistant.content:
                shortened = self._shorten(assistant.content, 240, 120)
                lines.append(f"- 早期轮次 {number} assistant: {shortened}")
            results = {message.tool_call_id: message for message in batch[1:]}
            for call in assistant.tool_calls:
                arguments = json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))
                line = f"  - {call.name}({self._shorten(arguments, 300, 120)})"
                tool_message = results.get(call.id)
                if tool_message is not None and tool_message.content is not None:
                    line += f" => {self._summarize_tool_result(tool_message.content)}"
                lines.append(line)
        content = "\n".join(lines)
        # 摘要本身也必须有界，防止大量短调用重新堆出超长上下文。
        if len(content) > self.config.tool_result_threshold_chars:
            content = self._head_tail(
                content,
                self.config.tool_result_head_chars,
                self.config.tool_result_tail_chars,
            )
        return Message(
            role=MessageRole.USER,
            content=content,
            metadata={"tracefix_context_summary": True},
        )

    def _summarize_tool_result(self, content: str) -> str:
        """保留工具结果的成功状态、错误和关键输出首尾。"""
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

    def _ensure_hard_limit(self, tokens: int) -> None:
        if tokens > self.config.context_window_tokens:
            raise ContextBudgetExceeded(
                "minimum safe request exceeds the configured context window",
                context={"estimated_tokens": tokens, "limit": self.config.context_window_tokens},
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
    def _view(
        messages: tuple[Message, ...],
        before: int,
        after: int,
        **metrics: Any,
    ) -> ContextView:
        return ContextView(
            messages=messages,
            estimated_tokens_before=before,
            estimated_tokens_after=after,
            original_message_count=metrics.pop("original_message_count", len(messages)),
            request_message_count=len(messages),
            **metrics,
        )
