"""TraceFix V0.1 的最小单 Agent 控制循环。"""

from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from tracefix.agent.base import AgentPhase, AgentState, AgentStatus, BaseAgent
from tracefix.agent.presentation import ToolResultPresenter
from tracefix.exceptions import (
    AgentCompleted,
    AgentError,
    AgentLimitExceeded,
    LLMResponseFormatError,
    StepLimitExceeded,
    TestLimitExceeded,
    TimeLimitExceeded,
    TokenBudgetExceeded,
    ToolError,
    ToolExecutionError,
    TraceFixError,
    TraceProtocolError,
    sanitize_payload,
)
from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.models.base import LLMResponse, TokenUsage
from tracefix.tools.base import ReservedToolName, ToolResult
from tracefix.tracing.base import TraceEvent, TraceEventType


class MinimalAgent(BaseAgent):
    """使用原生工具调用完成“分析—执行—反馈”闭环的最小 Agent。"""

    checkpoint_callback: Callable[[MinimalAgent], None] | None = None

    _RECOVERY_FIELDS = (
        "_failed_apply_calls", "_invalid_test_signatures", "_consecutive_apply_failures",
        "_patch_recovery_reminder_sent", "_no_effect_patch_reminder_sent",
        "_last_patch_failure_reason", "_no_effect_patch_targets",
        "_repeated_no_effect_patch_signature", "_fresh_read_required_for_patch",
        "_fresh_target_reads", "_tests_passed", "_diff_nonempty",
        "_finish_reminder_sent", "_unverified_finish_reminder_sent",
        "_repo_map_candidate_reads", "_exploration_reminder_sent",
        "_patch_action_reminder_sent", "_budget_guidance_sent",
        "_late_targeted_retrievals",
        "_last_test_evidence",
    )

    def checkpoint_payload(self) -> dict[str, Any]:
        """Capture only deterministic state at a complete message boundary."""
        if self.history.pending_tool_call_ids:
            raise AgentError("cannot checkpoint with pending tool calls")
        memory = {name: getattr(self, name) for name in self._RECOVERY_FIELDS}
        memory["_repo_map_candidate_reads"] = sorted(self._repo_map_candidate_reads)
        memory["_budget_guidance_sent"] = sorted(self._budget_guidance_sent)
        return {
            "state": self.state.model_dump(mode="json"),
            "history": [message.model_dump(mode="json") for message in self.history.snapshot()],
            "memory": json.loads(json.dumps(memory, ensure_ascii=False)),
            "task_id": self._task_id,
            "active_seconds": max(0.0, time.monotonic() - self._started_monotonic),
        }

    def verify_test_source(self, source_sha256: str) -> None:
        """Invalidate a passing test if the checkout diff has since changed."""
        evidence = self._last_test_evidence
        if (
            evidence is not None
            and evidence.get("valid") is True
            and isinstance(evidence.get("source_sha256"), str)
            and evidence["source_sha256"] != source_sha256
        ):
            evidence["valid"] = False
            evidence["invalidated_by"] = "checkout_changed_after_test"
            self._tests_passed = False
            self.state.validation_status = "unverified"

    def resume(self, payload: dict[str, Any]) -> AgentState:
        """Continue a previously validated local checkpoint without resetting history."""
        from tracefix.messages import MessageHistory

        self.state = AgentState.model_validate(payload["state"])
        self.history = MessageHistory(Message.model_validate(item) for item in payload["history"])
        if self.history.pending_tool_call_ids:
            raise AgentError("checkpoint contains pending tool calls")
        memory = payload["memory"]
        for name in self._RECOVERY_FIELDS:
            if name not in memory:
                raise AgentError(f"checkpoint missing runtime state: {name}")
            setattr(self, name, memory[name])
        self._repo_map_candidate_reads = set(memory["_repo_map_candidate_reads"])
        self._budget_guidance_sent = set(memory["_budget_guidance_sent"])
        self._successful_tool_cache = {}
        self._visible_tool_result_ids = set()
        self._task_id = str(payload["task_id"])
        self._started_monotonic = time.monotonic() - float(payload["active_seconds"])
        self._presenter = ToolResultPresenter(self.config.presentation.model_copy(
            update={"enabled": self.config.presentation.enabled
                    and self._feature_enabled(self.config.tool_result_presentation_enabled)}
        ))
        self.state.status = AgentStatus.RUNNING
        self.state.finished_at = None
        self.state.stop_reason = None
        return self._run_initialized(self.state.task or "", resume=True)

    def run(self, task: str) -> AgentState:
        """初始化消息历史并运行，正常完成或预算终止时返回最终状态。"""
        if not task.strip():
            raise AgentError("task cannot be empty")

        self.reset()
        # 以下运行期记忆只服务于确定性失败恢复，不进入持久化 AgentState。
        self._failed_apply_calls: dict[str, str] = {}
        self._invalid_test_signatures: dict[str, int] = {}
        self._consecutive_apply_failures = 0
        self._patch_recovery_reminder_sent = False
        self._no_effect_patch_reminder_sent = False
        self._last_patch_failure_reason: str | None = None
        self._no_effect_patch_targets: dict[
            str, dict[str, tuple[tuple[int, int], ...] | None]
        ] = {}
        self._repeated_no_effect_patch_signature: str | None = None
        self._fresh_read_required_for_patch = False
        self._fresh_target_reads: dict[str, list[tuple[int, int, int, bool]]] = {}
        self._tests_passed = False
        self._diff_nonempty = False
        self._finish_reminder_sent = False
        self._unverified_finish_reminder_sent = False
        self._successful_tool_cache: dict[str, ToolResult] = {}
        self._repo_map_candidate_reads: set[str] = set()
        self._exploration_reminder_sent = False
        self._patch_action_reminder_sent = False
        self._budget_guidance_sent: set[int] = set()
        # 当前一次请求实际看到了哪些完整工具结果。缓存引用只能在原结果仍可见时使用，
        # 否则必须重新执行读取，避免“指向已被折叠历史”的空引用。
        self._visible_tool_result_ids: set[str] = set()
        self._late_targeted_retrievals = 0
        self._last_test_evidence: dict[str, Any] | None = None
        presentation_config = self.config.presentation.model_copy(
            update={
                "enabled": (
                    self.config.presentation.enabled
                    and self._feature_enabled(self.config.tool_result_presentation_enabled)
                )
            }
        )
        self._presenter = ToolResultPresenter(presentation_config)
        self._task_id = uuid4().hex
        self._started_monotonic = time.monotonic()
        self.state.task = task
        self.state.status = AgentStatus.RUNNING
        self.state.started_at = datetime.now(UTC)

        try:
            return self._run_initialized(task)
        except TraceProtocolError:
            # sink 已经不可用时不能再尝试写事件，只更新内存中的最终状态。
            self.state.status = AgentStatus.FAILED
            self.state.stop_reason = TraceProtocolError.code
            self.state.finished_at = datetime.now(UTC)
            raise

    def _run_initialized(self, task: str, *, resume: bool = False) -> AgentState:
        """运行已经完成状态初始化的任务，并集中处理控制流异常。"""
        if not resume:
            self._emit(TraceEventType.TASK_STARTED, {"task": task})
            self._emit_state()
            self._append_message(
            Message(role=MessageRole.SYSTEM, content=self.config.system_prompt)
            )
        if not resume and self.config.skills_enabled:
            catalog = self.tools.skill_catalog
            if catalog:
                entries = "\n".join(
                    f"- {item.name}: {item.description}"
                    for item in catalog
                )
                self._append_message(
                    Message(
                        role=MessageRole.SYSTEM,
                        content=(
                            "可按需加载以下 TraceFix skills。仅在任务匹配时调用 load_skill；"
                            "技能只提供指令，不授予额外工具权限。相对引用通过 load_skill 的 "
                            "reference 参数读取。\n" + entries
                        ),
                        metadata={"kind": "skill_catalog"},
                    )
                )
                self._emit(
                    TraceEventType.SKILL_CATALOG_EXPOSED,
                    {"skills": [entry.model_dump(mode="json") for entry in catalog]},
                )
        if not resume and self.repository_map:
            # 将静态地图作为 system 锚点加入完整历史：压缩器会永久保留它，模型请求视图
            # 与 JSONL 审计也能明确区分“索引提供的候选”和 Agent 自己确认的事实。
            self._append_message(
                Message(
                    role=MessageRole.SYSTEM,
                    content=self.repository_map,
                    metadata={"kind": "repository_map"},
                )
            )
            self._emit(
                TraceEventType.REPO_MAP_ADDED,
                {"chars": len(self.repository_map)},
            )
        if not resume:
            self._append_message(Message(role=MessageRole.USER, content=task))
        if not resume and self.checkpoint_callback is not None:
            self.checkpoint_callback(self)

        try:
            while self.state.status is AgentStatus.RUNNING:
                self.step()
                if self.checkpoint_callback is not None:
                    self.checkpoint_callback(self)
        except AgentCompleted as exc:
            reason = exc.code
            if self.config.require_tested_completion and self.state.validation_status != "verified":
                reason = "agent_completed_unverified"
            self._finish(AgentStatus.COMPLETED, reason)
        except AgentLimitExceeded as exc:
            self._emit_error(exc)
            self._finish(AgentStatus.INTERRUPTED, exc.code)
        except KeyboardInterrupt:
            self._finish(AgentStatus.INTERRUPTED, "keyboard_interrupt")
            raise
        except TraceFixError as exc:
            self._emit_error(exc)
            self._finish(AgentStatus.FAILED, exc.code)
            raise
        except Exception as exc:
            wrapped = AgentError(
                f"unexpected agent error: {exc}",
                context={"error_type": type(exc).__name__},
            )
            self._emit_error(wrapped)
            self._finish(AgentStatus.FAILED, wrapped.code)
            raise wrapped from exc

        return self.state

    def step(self) -> None:
        """请求一次模型响应，并顺序执行响应中的全部工具调用。"""
        if self.state.status is not AgentStatus.RUNNING:
            raise AgentError(
                "agent is not running",
                context={"status": self.state.status.value},
            )

        self._check_pre_request_budgets()
        # 累计 Token 是多轮请求之和；在真正越限前把收敛要求放进下一次请求。
        self._append_budget_guidance_if_needed()
        self.state.step_count += 1
        tool_specs = self.tools.specs()
        context_started = time.perf_counter()
        full_history = self.history.snapshot()
        request_history = full_history
        if self._last_test_evidence is not None:
            fact = {
                "source": "tool_result",
                "latest_test": self._last_test_evidence,
                "validation_status": self.state.validation_status,
            }
            anchor = Message(
                role=MessageRole.SYSTEM,
                content="[TraceFix 已核验任务事实]\n" + json.dumps(
                    fact, ensure_ascii=False, sort_keys=True
                ),
                metadata={"kind": "task_fact_anchor"},
            )
            request_history = (full_history[0], anchor, *full_history[1:])
        context_view = self.context_manager.prepare(request_history, tool_specs)
        self.state.context_preparation_seconds += time.perf_counter() - context_started
        self._visible_tool_result_ids = {
            message.tool_call_id
            for message in context_view.messages
            if message.role is MessageRole.TOOL and message.tool_call_id is not None
        }
        self.state.context_metrics.add_view(context_view)
        self._emit(
            TraceEventType.CONTEXT_PREPARED,
            {
                "estimated_tokens_before": context_view.estimated_tokens_before,
                "estimated_tokens_after": context_view.estimated_tokens_after,
                "original_message_count": context_view.original_message_count,
                "request_message_count": context_view.request_message_count,
                "tool_results_pruned": context_view.tool_results_pruned,
                "messages_compacted": context_view.messages_compacted,
                "batches_compacted": context_view.batches_compacted,
                "compacted": context_view.compacted,
            },
        )
        if context_view.compacted or context_view.tool_results_pruned:
            self._emit(
                TraceEventType.CONTEXT_COMPACTED,
                {
                    "estimated_tokens_saved": max(
                        0,
                        context_view.estimated_tokens_before - context_view.estimated_tokens_after,
                    ),
                    "tool_results_pruned": context_view.tool_results_pruned,
                    "messages_compacted": context_view.messages_compacted,
                    "batches_compacted": context_view.batches_compacted,
                },
            )
        if self.config.record_request_views:
            # 这是供应商实际收到的压缩视图，而非完整历史。事件默认关闭，避免轨迹
            # 体积翻倍；开启时仍统一经过 _emit 的凭据脱敏。
            self._emit(
                TraceEventType.MODEL_REQUEST_VIEW,
                {
                    "schema_version": 1,
                    "sanitized": True,
                    "messages": [
                        message.model_dump(mode="json") for message in context_view.messages
                    ],
                    "tools": [spec.model_dump(mode="json") for spec in tool_specs],
                    "context": {
                        "estimated_tokens_before": context_view.estimated_tokens_before,
                        "estimated_tokens_after": context_view.estimated_tokens_after,
                        "compacted": context_view.compacted,
                        "tool_results_pruned": context_view.tool_results_pruned,
                    },
                },
            )
        self._emit(
            TraceEventType.MODEL_REQUESTED,
            {
                "message_count": context_view.request_message_count,
                "tool_names": list(self.tools.names),
            },
        )

        try:
            # 压缩仅影响供应商请求；self.history 仍保留完整审计轨迹。
            model_started = time.perf_counter()
            response = self.llm.complete(context_view.messages, tool_specs)
            model_duration_seconds = time.perf_counter() - model_started
            self.state.model_request_seconds += model_duration_seconds
        except LLMResponseFormatError as exc:
            # 响应解析失败也可能已经产生费用；尽量从异常上下文追回 usage。
            usage = exc.context.get("usage")
            if isinstance(usage, dict):
                self._add_usage_dict(usage)
            else:
                self.state.cost_complete = False
            raise
        except Exception:
            # A timed-out request may already have reached the provider and incurred cost.
            self.state.cost_complete = False
            raise

        self._add_usage(response.usage)
        self._append_message(response.message)
        self._emit_model_response(response, model_duration_seconds)

        # usage 只能在供应商返回后得知，因此一次调用可能略微越过预算。
        budget_error: AgentLimitExceeded | None = self._post_response_budget_error()
        if budget_error is None:
            try:
                self._check_time_budget()
            except TimeLimitExceeded as exc:
                budget_error = exc
        if budget_error is not None:
            self._append_skipped_results(response.message.tool_calls, budget_error)
            raise budget_error

        if not response.message.tool_calls:
            if (
                self.config.require_tested_completion
                and self.state.validation_status != "verified"
                and not self._unverified_finish_reminder_sent
            ):
                self._append_message(
                    Message(
                        role=MessageRole.USER,
                        content=(
                            "系统验证提示：当前补丁尚无与之对应的有效测试通过记录，或尚未确认非空改动。"
                            "请检查 Diff，并运行实际测试；若预算不允许或测试与需求冲突，"
                            "请说明具体情况。"
                        ),
                        metadata={"kind": "validation_required"},
                    )
                )
                self._unverified_finish_reminder_sent = True
                return
            self.state.final_output = response.message.content or ""
            self._set_phase(AgentPhase.FINISH, "assistant_final")
            raise AgentCompleted(
                "model returned a final response",
                context={"final_output": self.state.final_output},
            )

        pending_skill_messages: list[tuple[Message, dict[str, object]]] = []
        for index, call in enumerate(response.message.tool_calls):
            try:
                self._check_time_budget()
                if call.name == ReservedToolName.RUN_TESTS.value:
                    tool = self.tools.get(call.name)
                    try:
                        prepare = getattr(tool, "prepare", None)
                        if prepare is not None:
                            prepare(call)
                    except ToolError as exc:
                        signature = self._tool_signature(call)
                        count = self._invalid_test_signatures.get(signature, 0) + 1
                        self._invalid_test_signatures[signature] = count
                        self.state.rejected_test_calls += 1
                        self._emit(TraceEventType.TEST_CALL_REJECTED, {
                            "call_id": call.id, "reason": exc.message,
                            "same_call_count": count,
                            "total_count": self.state.rejected_test_calls,
                        })
                        if count >= 3 or self.state.rejected_test_calls >= 6:
                            raise TestLimitExceeded("invalid test call limit exceeded") from exc
                        result = ToolResult(
                            call_id=call.id,
                            tool_name=call.name,
                            success=False,
                            error=exc.message,
                            metadata={"error": exc.to_dict(), "test_call_rejected": True},
                        )
                        self._append_tool_result(result)
                        continue
                    if self.state.test_runs >= self.config.max_test_runs:
                        raise TestLimitExceeded(
                            "test run budget exceeded",
                            context={
                                "actual": self.state.test_runs,
                                "limit": self.config.max_test_runs,
                            },
                        )
                    def record_test_start(call_id: str = call.id) -> None:
                        self.state.test_runs += 1
                        self._emit(TraceEventType.TEST_PROCESS_STARTED, {
                            "call_id": call_id, "test_runs": self.state.test_runs,
                        })
                    if prepare is not None:
                        tool.on_process_started = record_test_start
                result = self._execute_tool(call)
                if call.name == ReservedToolName.RUN_TESTS.value and prepare is None:
                    record_test_start()
            except AgentLimitExceeded as exc:
                # 消息历史要求每个 tool call 都有结果，因此为未执行调用补齐失败消息。
                self._append_skipped_results(response.message.tool_calls[index:], exc)
                raise

            self._append_tool_result(result)
            if (
                result.success
                and result.tool_name == "load_skill"
                and isinstance(result.output, dict)
                and result.output.get("kind") in {"skill", "reference"}
                and isinstance(result.output.get("content"), str)
            ):
                content = result.output["content"]
                metadata: dict[str, object] = {
                    "kind": "skill_instructions",
                    "skill_name": result.output.get("name"),
                    "skill_version": result.output.get("version"),
                    "content_sha256": result.output.get("sha256"),
                    "source": result.output.get("path"),
                    "content_bytes": result.output.get("content_bytes"),
                }
                pending_skill_messages.append(
                    (
                        Message(
                            role=MessageRole.SYSTEM,
                            content=(
                                "<tracefix_skill_instructions>\n"
                                + content
                                + "\n</tracefix_skill_instructions>"
                            ),
                            metadata=metadata,
                        ),
                        metadata,
                    )
                )

        # 提示只能在本轮全部 tool result 写回后追加，否则会违反消息配对协议。
        for message, metadata in pending_skill_messages:
            self._append_message(message)
            self._emit(
                TraceEventType.SKILL_ACTIVATED,
                {**metadata, "content": message.content},
            )
        self._append_patch_recovery_reminder_if_needed()
        self._append_no_effect_patch_guidance_if_needed()
        self._append_exploration_guidance_if_needed()
        self._append_patch_action_guidance_if_needed()
        self._append_finish_reminder_if_ready()
        self._check_time_budget()

    def _execute_tool(self, call: ToolCall) -> ToolResult:
        """执行一个工具；可恢复错误会被转换成结构化失败结果。"""
        self._emit(TraceEventType.TOOL_CALLED, {"call": call.model_dump(mode="json")})
        started = time.monotonic()
        signature = self._tool_signature(call)

        if call.name == ReservedToolName.SEARCH_CODE.value:
            self.state.search_calls += 1
        elif call.name == ReservedToolName.READ_FILE.value:
            self.state.file_read_calls += 1

        if (
            self._feature_enabled(self.config.action_guidance_enabled)
            and self.state.input_tokens / self.config.max_input_tokens >= 0.85
            and call.name
            in {
                ReservedToolName.SEARCH_CODE.value,
                ReservedToolName.READ_FILE.value,
            }
            and not self._is_targeted_retrieval(call)
        ):
            # 最后 15% 不再阻断所有读取：只拒绝宽泛搜索，并仍允许一次有具体路径/符号
            # 的定向取证。这样预算策略不会把关键修复证据误当作无效探索。
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error="broad exploration deferred by input-token convergence policy",
                metadata={
                    "policy_blocked": True,
                    "input_budget_ratio": round(
                        self.state.input_tokens / self.config.max_input_tokens, 4
                    ),
                    "recommendation": (
                        "请改为一次包含具体路径或 glob 的定向读取，或执行补丁、测试、Diff。"
                    ),
                },
                duration_ms=(time.monotonic() - started) * 1000,
            )
            self._record_tool_outcome(call, result, signature)
            return result

        # 搜索和读取是纯读取操作。完全相同的成功调用直接返回紧凑引用，原始结果仍在
        # 完整历史中，避免再次扫描仓库并把同一大段内容重复送入后续上下文。
        if (
            self._feature_enabled(self.config.read_cache_enabled)
            and call.name
            in {
                ReservedToolName.SEARCH_CODE.value,
                ReservedToolName.READ_FILE.value,
            }
            and signature in self._successful_tool_cache
            and self._successful_tool_cache[signature].call_id in self._visible_tool_result_ids
        ):
            previous = self._successful_tool_cache[signature]
            self.state.cached_tool_calls += 1
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=True,
                output=self._cached_result_summary(previous),
                metadata={
                    "cached": True,
                    "original_call_id": previous.call_id,
                    "recommendation": "请使用历史中的原始结果，不要重复相同调用。",
                },
                duration_ms=(time.monotonic() - started) * 1000,
            )
            self._record_tool_outcome(call, result, signature)
            return result

        # 对完全相同且已经失败的补丁不再重复调用 Git。模型仍会收到一个合法的
        # tool result，因而既节省步骤内开销，也不会留下悬空调用 ID。
        if (
            call.name == ReservedToolName.APPLY_PATCH.value
            and signature in self._failed_apply_calls
        ):
            repeated_no_effect = (
                self.config.require_fresh_read_after_repeated_no_effect_patch
                and signature in self._no_effect_patch_targets
                and bool(self._no_effect_patch_targets[signature])
            )
            if repeated_no_effect:
                self._repeated_no_effect_patch_signature = signature
                self._fresh_read_required_for_patch = True
                self._fresh_target_reads.clear()
                self._invalidate_no_effect_target_read_cache(signature)
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error=(
                    "repeated no-effect patch requires a fresh target read and different patch"
                    if repeated_no_effect
                    else "duplicate failed patch call was not executed"
                ),
                metadata={
                    "duplicate": True,
                    **({"recovery_gate": "fresh_read_required"} if repeated_no_effect else {}),
                    "original_call_id": self._failed_apply_calls[signature],
                    "recommendation": (
                        "不要重复相同补丁；请重新读取目标文件，并使用标准 unified diff "
                        "或 *** Begin Patch / *** Update File 格式。"
                    ),
                },
                duration_ms=(time.monotonic() - started) * 1000,
            )
            self._record_tool_outcome(call, result, signature)
            return result

        if (
            call.name == ReservedToolName.APPLY_PATCH.value
            and self.config.require_fresh_read_after_repeated_no_effect_patch
            and self._fresh_read_required_for_patch
            and not self._no_effect_paths_were_read()
        ):
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error="fresh target read required after repeated no-effect patch",
                metadata={
                    "recovery_gate": "fresh_read_required",
                    "target_paths": sorted(
                        self._no_effect_patch_targets.get(
                            self._repeated_no_effect_patch_signature or "", {}
                        )
                    ),
                },
                duration_ms=(time.monotonic() - started) * 1000,
            )
            return result

        if (
            call.name == ReservedToolName.APPLY_PATCH.value
            and self.config.require_fresh_read_after_repeated_no_effect_patch
            and self._fresh_read_required_for_patch
        ):
            # A fresh read authorizes exactly one distinct patch attempt.
            self._fresh_read_required_for_patch = False
            self._fresh_target_reads.clear()
            self._repeated_no_effect_patch_signature = None

        try:
            tool = self.tools.get(call.name)
            result = tool.execute(call)
            if result.call_id != call.id or result.tool_name != call.name:
                raise ToolExecutionError(
                    "tool returned mismatched call metadata",
                    context={
                        "expected_call_id": call.id,
                        "actual_call_id": result.call_id,
                        "expected_tool_name": call.name,
                        "actual_tool_name": result.tool_name,
                    },
                )
        except ToolError as exc:
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error=exc.message,
                metadata={"error": exc.to_dict()},
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except Exception as exc:
            wrapped = ToolExecutionError(
                f"unexpected tool error: {exc}",
                context={"tool_name": call.name, "error_type": type(exc).__name__},
            )
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error=wrapped.message,
                metadata={"error": wrapped.to_dict()},
                duration_ms=(time.monotonic() - started) * 1000,
            )
        self.state.tool_execution_seconds += time.monotonic() - started
        self._record_tool_outcome(call, result, signature)
        if (
            self._feature_enabled(self.config.read_cache_enabled)
            and result.success
            and call.name
            in {
                ReservedToolName.SEARCH_CODE.value,
                ReservedToolName.READ_FILE.value,
            }
        ):
            self._successful_tool_cache[signature] = result.model_copy(deep=True)
        return result

    def _is_targeted_retrieval(self, call: ToolCall) -> bool:
        """判断高预算下的读取是否足够具体，并限制为一次补充证据机会。"""
        if self._late_targeted_retrievals >= 1:
            return False
        if call.name == ReservedToolName.READ_FILE.value:
            self._late_targeted_retrievals += 1
            return True
        if call.name == ReservedToolName.SEARCH_CODE.value:
            path = call.arguments.get("path", ".")
            glob = call.arguments.get("glob", "**/*")
            if path != "." or glob != "**/*":
                self._late_targeted_retrievals += 1
                return True
        return False

    @staticmethod
    def _cached_result_summary(previous: ToolResult) -> dict[str, Any]:
        """为命中过往结果的只读调用生成小型引用，不复制源码正文或全部匹配。"""
        output = previous.output if isinstance(previous.output, dict) else {}
        if previous.tool_name == ReservedToolName.READ_FILE.value:
            return {
                "cached": True,
                "path": output.get("path"),
                "previous_range": [output.get("start_line"), output.get("end_line")],
                "unchanged_since_last_patch": True,
            }
        matches = output.get("matches", [])
        paths = sorted(
            {
                str(match.get("path"))
                for match in matches
                if isinstance(match, dict) and match.get("path")
            }
        )
        return {
            "cached": True,
            "query": output.get("query"),
            "match_count": len(matches),
            "matched_files": paths[:10],
        }

    @staticmethod
    def _tool_signature(call: ToolCall) -> str:
        """补齐只读工具默认参数后生成稳定签名，识别语义相同的调用。"""
        arguments = dict(call.arguments)
        if call.name == ReservedToolName.SEARCH_CODE.value:
            arguments = {
                "path": ".",
                "glob": "**/*",
                "case_sensitive": False,
                "max_results": 20,
                **arguments,
            }
        elif call.name == ReservedToolName.READ_FILE.value:
            arguments = {"start_line": 1, "end_line": None, **arguments}
        arguments = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"{call.name}:{arguments}"

    def _record_tool_outcome(
        self,
        call: ToolCall,
        result: ToolResult,
        signature: str,
    ) -> None:
        """记录影响失败恢复和正常收尾的少量确定性事实。"""
        if call.name == ReservedToolName.READ_FILE.value and result.success:
            output = result.output if isinstance(result.output, dict) else {}
            requested_path = call.arguments.get("path")
            output_path = output.get("path")
            if isinstance(output_path, str):
                normalized_output_path = output_path.replace("\\", "/")
                if (
                    self._fresh_read_required_for_patch
                    and not result.metadata.get("cached")
                    and self._path_key(normalized_output_path)
                    in {
                        self._path_key(target)
                        for target in self._no_effect_patch_targets.get(
                            self._repeated_no_effect_patch_signature or "", {}
                        ).keys()
                    }
                ):
                    self._fresh_target_reads.setdefault(
                        self._path_key(normalized_output_path), []
                    ).append(
                        (
                            int(output.get("start_line", 1)),
                            int(output.get("end_line", 0)),
                            int(output.get("total_lines", 0)),
                            bool(output.get("truncated", True)),
                        )
                    )
            # Repo Map 记录的是成功的候选读取调用。保留请求路径兼容旧工具适配器；
            # 恢复门槛上方则只认工具返回的真实路径与行范围。
            if isinstance(requested_path, str):
                normalized_requested_path = requested_path.replace("\\", "/")
                if normalized_requested_path in self.repository_candidates:
                    self._repo_map_candidate_reads.add(normalized_requested_path)
                    self.state.repo_map_candidate_reads = len(self._repo_map_candidate_reads)
                    # Repo Map 候选被读到只是一条定位证据，不强行切换阶段；模型仍可在
                    # 后续测试反馈下补读调用方，避免过早补丁导致准确率下降。

        if call.name == ReservedToolName.APPLY_PATCH.value:
            if result.success:
                self._set_phase(AgentPhase.VERIFY, "patch_applied")
                self._consecutive_apply_failures = 0
                # 代码已经改变，旧的只读缓存可能过期，必须全部失效。
                self._successful_tool_cache.clear()
                # 文件再次改变后，旧的测试和 Diff 结论都已经过期。
                self._tests_passed = False
                self._diff_nonempty = False
                self.state.validation_status = "unverified"
                if self._last_test_evidence is not None:
                    self._last_test_evidence["valid"] = False
                    self._last_test_evidence["invalidated_by"] = call.id
                self._finish_reminder_sent = False
                self._last_patch_failure_reason = None
            else:
                self._set_phase(AgentPhase.PATCH, "patch_attempted")
                self._failed_apply_calls.setdefault(signature, call.id)
                if (
                    self.config.require_fresh_read_after_repeated_no_effect_patch
                    and result.error == "no_effect"
                ):
                    self._no_effect_patch_targets[signature] = self._extract_patch_read_ranges(
                        str(call.arguments.get("patch", ""))
                    )
                self._consecutive_apply_failures += 1
                self._last_patch_failure_reason = result.error
            return

        if call.name == ReservedToolName.RUN_TESTS.value:
            # 测试本身可能生成文件、刷新缓存或改变临时配置，因此所有只读缓存都失效。
            self._successful_tool_cache.clear()
            self._tests_passed = result.success
            output = result.output if isinstance(result.output, dict) else {}
            self._last_test_evidence = {
                "call_id": call.id,
                "success": result.success,
                "valid": result.success,
                "command": output.get("command"),
                "test_status": output.get("test_status"),
                "returncode": output.get("returncode"),
                "source_sha256": output.get("source_sha256_after"),
                "audit_path": output.get("audit_path"),
                "junit_path": output.get("junit_path"),
            }
            if result.success:
                if self._diff_nonempty:
                    self.state.validation_status = "verified"
                    self._set_phase(AgentPhase.FINISH, "tests_and_diff_ready")
            else:
                self.state.validation_status = "unverified"
                self._set_phase(AgentPhase.PATCH, "tests_failed")
            return

        if call.name == ReservedToolName.GET_GIT_DIFF.value and result.success:
            output = result.output if isinstance(result.output, dict) else {}
            self._diff_nonempty = bool(str(output.get("diff", "")).strip())
            if self._tests_passed and self._diff_nonempty:
                self.state.validation_status = "verified"
                self._set_phase(AgentPhase.FINISH, "tests_and_diff_ready")
            else:
                self.state.validation_status = "unverified"

    def _set_phase(self, phase: AgentPhase, reason: str) -> None:
        """仅在阶段实际变化时更新状态并留下可审计事件。"""
        if self.state.phase is phase:
            return
        previous = self.state.phase
        self.state.phase = phase
        self._emit(
            TraceEventType.AGENT_PHASE_CHANGED,
            {"from": previous.value, "to": phase.value, "reason": reason},
        )

    def _feature_enabled(self, override: bool | None) -> bool:
        """Resolve a split optimization flag with backward-compatible fallback."""
        return self.config.token_optimization_enabled if override is None else override

    @staticmethod
    def _extract_patch_paths(patch: str) -> set[str]:
        """Extract old-side file paths, honoring quoted paths containing whitespace."""
        return set(MinimalAgent._extract_patch_read_ranges(patch))

    @staticmethod
    def _extract_patch_read_ranges(
        patch: str,
    ) -> dict[str, tuple[tuple[int, int], ...] | None]:
        """Return pre-patch line ranges the recovery gate must observe.

        ``None`` means that the patch syntax did not expose reliable hunk ranges, so
        a complete, untruncated read of that file is required.
        """
        ranges: dict[str, list[tuple[int, int]] | None] = {}
        old_path: str | None = None
        for line in patch.splitlines():
            if line.startswith("*** Update File: "):
                old_path = line.removeprefix("*** Update File: ").strip()
                if old_path:
                    ranges.setdefault(old_path.replace("\\", "/"), None)
                continue
            if line.startswith("--- "):
                try:
                    candidate = shlex.split(line[4:], posix=True)[0]
                except (ValueError, IndexError):
                    old_path = None
                    continue
                if candidate == "/dev/null":
                    old_path = None
                    continue
                old_path = candidate[2:] if candidate.startswith("a/") else candidate
                old_path = old_path.replace("\\", "/")
                ranges.setdefault(old_path, [])
                continue
            if old_path is not None and line.startswith("@@"):
                match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
                if match is None:
                    ranges[old_path] = None
                    continue
                start = int(match.group(1))
                count = int(match.group(2) or "1")
                first = max(1, start if count else start + 1)
                last = max(first, start + count - 1)
                existing = ranges.get(old_path)
                if existing is not None:
                    existing.append((first, last))
        return {
            path: None if not spans else tuple(spans)
            for path, spans in ranges.items()
        }

    @staticmethod
    def _path_key(path: str) -> str:
        """Normalize separators and case for workspace-relative path comparisons."""
        return path.replace("\\", "/").casefold()

    def _no_effect_paths_were_read(self) -> bool:
        targets = self._no_effect_patch_targets.get(
            self._repeated_no_effect_patch_signature or "", set()
        )
        if not targets:
            return False
        for target, required_ranges in targets.items():
            observations = self._fresh_target_reads.get(self._path_key(target), ())
            if required_ranges is None:
                if not any(
                    start <= 1 and end >= total and total > 0 and not truncated
                    for start, end, total, truncated in observations
                ):
                    return False
                continue
            for required_start, required_end in required_ranges:
                if not any(
                    start <= required_start and end >= required_end and not truncated
                    for start, end, _total, truncated in observations
                ):
                    return False
        return True

    def _invalidate_no_effect_target_read_cache(self, signature: str) -> None:
        """A cached pre-failure read cannot satisfy recovery or hide a new read."""
        targets = {
            self._path_key(path)
            for path in self._no_effect_patch_targets.get(signature, {})
        }
        for cached_signature in tuple(self._successful_tool_cache):
            tool_name, separator, encoded = cached_signature.partition(":")
            if tool_name != ReservedToolName.READ_FILE.value or not separator:
                continue
            try:
                arguments = json.loads(encoded)
            except json.JSONDecodeError:
                continue
            path = arguments.get("path") if isinstance(arguments, dict) else None
            if isinstance(path, str) and self._path_key(path) in targets:
                self._successful_tool_cache.pop(cached_signature, None)

    def _append_exploration_guidance_if_needed(self) -> None:
        """探索达到任一软上限后推动收敛，不直接拒绝模型后续的定向读取。"""
        if (
            not self._feature_enabled(self.config.action_guidance_enabled)
            or self.state.phase is not AgentPhase.EXPLORE
            or self._exploration_reminder_sent
        ):
            return
        reasons = []
        if self.state.step_count >= self.config.max_exploration_steps:
            reasons.append("模型探索步骤已达到软上限")
        if self.state.search_calls >= self.config.max_search_calls:
            reasons.append("搜索调用已达到软上限")
        if self.state.file_read_calls >= self.config.max_file_reads_before_patch:
            reasons.append("补丁前文件读取已达到软上限")
        if not reasons:
            return
        self._append_message(
            Message(
                role=MessageRole.USER,
                content=(
                    f"系统行动提示：{'；'.join(reasons)}。请停止一般性搜索，基于现有证据提出"
                    "最小补丁。若仍缺信息，只允许一次针对具体符号或调用方的定向读取，并明确"
                    "说明缺失证据。"
                ),
                metadata={"kind": "exploration_budget", "reasons": reasons},
            )
        )
        self._exploration_reminder_sent = True
        self._set_phase(AgentPhase.PATCH, "exploration_soft_limit")

    def _append_patch_action_guidance_if_needed(self) -> None:
        """读取足够的 Repo Map 候选后要求从定位切换到最小修改。"""
        if (
            not self._feature_enabled(self.config.action_guidance_enabled)
            or self._patch_action_reminder_sent
            or self.state.repo_map_candidate_reads < self.config.repo_map_reads_before_patch
        ):
            return
        self._append_message(
            Message(
                role=MessageRole.USER,
                content=(
                    "系统行动提示：你已经读取了足够的高优先级候选文件。请基于当前代码提出"
                    "最小补丁；如果仍不能修改，只进行一次有明确目标的定向读取，不要重新开始"
                    "全仓库搜索。"
                ),
                metadata={
                    "kind": "patch_action",
                    "repo_map_candidate_reads": self.state.repo_map_candidate_reads,
                },
            )
        )
        self._patch_action_reminder_sent = True

    def _append_budget_guidance_if_needed(self) -> None:
        """按累计输入预算的 50%/70%/85% 发送一次性收敛提示。"""
        ratio = self.state.input_tokens / self.config.max_input_tokens
        reached = [threshold for threshold in (50, 70, 85) if ratio >= threshold / 100]
        pending = [
            threshold for threshold in reached if threshold not in self._budget_guidance_sent
        ]
        if not self._feature_enabled(self.config.action_guidance_enabled) or not pending:
            return
        threshold = max(pending)
        if threshold >= 85:
            instruction = "优先补丁、测试、Diff 或最终回答；若缺关键证据，只进行一次定向读取。"
        elif threshold >= 70:
            instruction = "停止一般性搜索，优先形成补丁并运行测试。"
        else:
            instruction = "开始收敛；后续搜索必须针对具体缺失证据。"
        self._append_message(
            Message(
                role=MessageRole.USER,
                content=(
                    f"系统预算提示：累计输入 Token 已使用约 {ratio:.0%}（触发 {threshold}% "
                    f"阈值）。{instruction}"
                ),
                metadata={"kind": "token_budget_guidance", "threshold_percent": threshold},
            )
        )
        self._budget_guidance_sent.update(reached)

    def _append_patch_recovery_reminder_if_needed(self) -> None:
        """连续补丁失败后给出一次明确恢复路径，阻断无信息重试。"""
        if self._consecutive_apply_failures < 2 or self._patch_recovery_reminder_sent:
            return
        self._append_message(
            Message(
                role=MessageRole.USER,
                content=(
                    "系统恢复提示：apply_patch 已连续失败。不要再次提交相同或近似的补丁。"
                    "请先根据错误重新读取目标文件，核对上下文；随后使用标准 Git unified "
                    "diff，或 *** Begin Patch / *** Update File: <相对路径> / @@ 更新块。"
                ),
                metadata={"kind": "patch_recovery", "failures": self._consecutive_apply_failures},
            )
        )
        self._patch_recovery_reminder_sent = True

    def _append_no_effect_patch_guidance_if_needed(self) -> None:
        """补丁未改变文件时立即要求重新读取，避免模型提交空补丁后盲目重试。"""
        if self._no_effect_patch_reminder_sent or not self._last_patch_failure_reason:
            return
        reason = self._last_patch_failure_reason.casefold()
        no_effect_reasons = ("does not change", "already applied", "no_effect")
        if not any(token in reason for token in no_effect_reasons):
            return
        self._append_message(
            Message(
                role=MessageRole.USER,
                content=(
                    "系统恢复提示：刚才的 apply_patch 没有改变目标文件。请先 read_file "
                    "核对待修改的确切行和当前 Diff；下一次补丁必须包含实际替换或新增，"
                    "不要重复提交只含上下文的空补丁。"
                ),
                metadata={"kind": "no_effect_patch", "reason": self._last_patch_failure_reason},
            )
        )
        self._no_effect_patch_reminder_sent = True

    def _append_finish_reminder_if_ready(self) -> None:
        """测试通过且已有改动时提示模型收尾，避免解决后继续消耗步骤。"""
        if not (self._tests_passed and self._diff_nonempty) or self._finish_reminder_sent:
            return
        self._append_message(
            Message(
                role=MessageRole.USER,
                content=(
                    "系统验证提示：最近一次测试已经通过，且 get_git_diff 显示存在非空改动。"
                    "若没有新的反例需要处理，请停止调用工具，直接给出修改与测试结果的最终说明。"
                ),
                metadata={"kind": "ready_to_finish"},
            )
        )
        self._finish_reminder_sent = True

    def _append_tool_result(self, result: ToolResult) -> None:
        """把工具结果转换成模型可消费的 tool 消息并写入轨迹。"""
        original = result.model_dump_json()
        model_result = result
        if (
            result.success
            and result.tool_name == "load_skill"
            and isinstance(result.output, dict)
            and isinstance(result.output.get("content"), str)
        ):
            receipt = {key: value for key, value in result.output.items() if key != "content"}
            model_result = result.model_copy(update={"output": receipt}, deep=True)
        presented = self._presenter.present(model_result)
        self.state.presentation_metrics.add(original, presented)
        self._append_message(
            Message(
                role=MessageRole.TOOL,
                content=presented,
                tool_call_id=result.call_id,
                metadata={"tool_name": result.tool_name, "success": result.success},
            )
        )
        self._emit(
            TraceEventType.TOOL_RETURNED,
            {"result": result.model_dump(mode="json")},
        )
        self._emit(
            TraceEventType.TOOL_RESULT_PRESENTED,
            {
                "call_id": result.call_id,
                "tool_name": result.tool_name,
                "original_chars": len(original),
                "presented_chars": len(presented),
                "compacted": original != presented,
            },
        )

    def _append_skipped_results(
        self,
        calls: tuple[ToolCall, ...],
        reason: AgentLimitExceeded,
    ) -> None:
        """为预算终止后无法执行的调用补写失败结果，保持消息历史闭合。"""
        for call in calls:
            self._append_tool_result(
                ToolResult(
                    call_id=call.id,
                    tool_name=call.name,
                    success=False,
                    error=f"not executed: {reason.message}",
                    metadata={"skipped": True, "reason": reason.code},
                )
            )

    def _check_pre_request_budgets(self) -> None:
        """在产生下一次模型费用前检查所有已知预算。"""
        self._check_time_budget()
        if self.state.step_count >= self.config.max_steps:
            raise StepLimitExceeded(
                "step budget exceeded",
                context={"actual": self.state.step_count, "limit": self.config.max_steps},
            )
        if self.state.input_tokens >= self.config.max_input_tokens:
            raise TokenBudgetExceeded(
                "input token budget exceeded",
                context={
                    "kind": "input",
                    "actual": self.state.input_tokens,
                    "limit": self.config.max_input_tokens,
                },
            )
        if self.state.output_tokens >= self.config.max_output_tokens:
            raise TokenBudgetExceeded(
                "output token budget exceeded",
                context={
                    "kind": "output",
                    "actual": self.state.output_tokens,
                    "limit": self.config.max_output_tokens,
                },
            )

    def _post_response_budget_error(self) -> TokenBudgetExceeded | None:
        """返回模型响应后出现的 Token 越限错误。"""
        if self.state.input_tokens > self.config.max_input_tokens:
            return TokenBudgetExceeded(
                "input token budget exceeded",
                context={
                    "kind": "input",
                    "actual": self.state.input_tokens,
                    "limit": self.config.max_input_tokens,
                },
            )
        if self.state.output_tokens > self.config.max_output_tokens:
            return TokenBudgetExceeded(
                "output token budget exceeded",
                context={
                    "kind": "output",
                    "actual": self.state.output_tokens,
                    "limit": self.config.max_output_tokens,
                },
            )
        return None

    def _check_time_budget(self) -> None:
        """使用单调时钟检查运行时长，避免系统时间调整影响预算。"""
        started = getattr(self, "_started_monotonic", None)
        if started is None:
            return
        elapsed = time.monotonic() - started
        if elapsed >= self.config.wall_time_seconds:
            raise TimeLimitExceeded(
                "wall time budget exceeded",
                context={"actual_seconds": elapsed, "limit": self.config.wall_time_seconds},
            )

    def _add_usage(self, usage: TokenUsage) -> None:
        """累计一次已成功解析的模型调用用量。"""
        self.state.input_tokens += usage.input_tokens
        self.state.output_tokens += usage.output_tokens
        if usage.cost_usd is None:
            # 未知费用不能冒充为零；保留已知部分并标记汇总不完整。
            self.state.cost_complete = False
        else:
            self.state.cost_usd += usage.cost_usd

    def _add_usage_dict(self, usage: dict[str, Any]) -> None:
        """从格式错误上下文中尽力恢复可计费的 usage。"""
        for key, state_field in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
        ):
            value = usage.get(key)
            if isinstance(value, int) and value >= 0:
                setattr(self.state, state_field, getattr(self.state, state_field) + value)
        cost = usage.get("cost_usd")
        if isinstance(cost, (int, float)) and cost >= 0:
            self.state.cost_usd += float(cost)
        else:
            self.state.cost_complete = False

    def _append_message(self, message: Message) -> None:
        """追加消息并同步发送追踪事件。"""
        self.history.append(message)
        self._emit(TraceEventType.MESSAGE_ADDED, {"message": message.model_dump(mode="json")})

    def _emit_model_response(self, response: LLMResponse, duration_seconds: float) -> None:
        """记录规范化后的模型响应与用量，不重复写入原始供应商响应。"""
        self._emit(
            TraceEventType.MODEL_RESPONDED,
            {
                "model_name": response.model_name,
                "finish_reason": response.finish_reason,
                "usage": response.usage.model_dump(mode="json"),
                "message_id": response.message.id,
                "duration_ms": round(duration_seconds * 1000, 3),
            },
        )

    def _finish(self, status: AgentStatus, reason: str) -> None:
        """以统一顺序写入最终状态和任务结束事件。"""
        self.state.status = status
        self.state.stop_reason = reason
        self.state.finished_at = datetime.now(UTC)
        self._emit_state()
        self._emit(TraceEventType.TASK_FINISHED, {"state": self.state.model_dump(mode="json")})

    def _emit_state(self) -> None:
        """记录当前完整 Agent 状态。"""
        self._emit(
            TraceEventType.AGENT_STATE_CHANGED,
            {"state": self.state.model_dump(mode="json")},
        )

    def _emit_error(self, error: TraceFixError) -> None:
        """将稳定异常结构写入追踪器。"""
        self._emit(TraceEventType.ERROR, {"error": error.to_dict()})

    def _emit(self, event_type: TraceEventType, payload: dict[str, Any]) -> None:
        """向可选 sink 写入事件，并把 sink 故障映射为轨迹协议异常。"""
        if self.trace_sink is None:
            return
        try:
            self.trace_sink.write(
                TraceEvent(
                    event_type=event_type,
                    task_id=getattr(self, "_task_id", None),
                    step=self.state.step_count,
                    payload=sanitize_payload(payload),
                )
            )
        except TraceProtocolError:
            raise
        except Exception as exc:
            raise TraceProtocolError(
                f"trace sink failed: {exc}",
                context={"event_type": event_type.value, "error_type": type(exc).__name__},
            ) from exc
