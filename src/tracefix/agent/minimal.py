"""TraceFix V0.1 的最小单 Agent 控制循环。"""

from __future__ import annotations

import json
import time
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

    def run(self, task: str) -> AgentState:
        """初始化消息历史并运行，正常完成或预算终止时返回最终状态。"""
        if not task.strip():
            raise AgentError("task cannot be empty")

        self.reset()
        # 以下运行期记忆只服务于确定性失败恢复，不进入持久化 AgentState。
        self._failed_apply_calls: dict[str, str] = {}
        self._consecutive_apply_failures = 0
        self._patch_recovery_reminder_sent = False
        self._no_effect_patch_reminder_sent = False
        self._last_patch_failure_reason: str | None = None
        self._tests_passed = False
        self._diff_nonempty = False
        self._finish_reminder_sent = False
        self._successful_tool_cache: dict[str, ToolResult] = {}
        self._repo_map_candidate_reads: set[str] = set()
        self._exploration_reminder_sent = False
        self._patch_action_reminder_sent = False
        self._budget_guidance_sent: set[int] = set()
        # 当前一次请求实际看到了哪些完整工具结果。缓存引用只能在原结果仍可见时使用，
        # 否则必须重新执行读取，避免“指向已被折叠历史”的空引用。
        self._visible_tool_result_ids: set[str] = set()
        self._late_targeted_retrievals = 0
        presentation_config = self.config.presentation.model_copy(
            update={
                "enabled": (
                    self.config.presentation.enabled and self.config.token_optimization_enabled
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

    def _run_initialized(self, task: str) -> AgentState:
        """运行已经完成状态初始化的任务，并集中处理控制流异常。"""
        self._emit(TraceEventType.TASK_STARTED, {"task": task})
        self._emit_state()
        self._append_message(Message(role=MessageRole.SYSTEM, content=self.config.system_prompt))
        if self.repository_map:
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
        self._append_message(Message(role=MessageRole.USER, content=task))

        try:
            while self.state.status is AgentStatus.RUNNING:
                self.step()
        except AgentCompleted as exc:
            self._finish(AgentStatus.COMPLETED, exc.code)
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
        context_view = self.context_manager.prepare(self.history.snapshot(), tool_specs)
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
            self.state.final_output = response.message.content or ""
            self._set_phase(AgentPhase.FINISH, "assistant_final")
            raise AgentCompleted(
                "model returned a final response",
                context={"final_output": self.state.final_output},
            )

        for index, call in enumerate(response.message.tool_calls):
            try:
                self._check_time_budget()
                if call.name == ReservedToolName.RUN_TESTS.value:
                    if self.state.test_runs >= self.config.max_test_runs:
                        raise TestLimitExceeded(
                            "test run budget exceeded",
                            context={
                                "actual": self.state.test_runs,
                                "limit": self.config.max_test_runs,
                            },
                        )
                    self.state.test_runs += 1
                result = self._execute_tool(call)
            except AgentLimitExceeded as exc:
                # 消息历史要求每个 tool call 都有结果，因此为未执行调用补齐失败消息。
                self._append_skipped_results(response.message.tool_calls[index:], exc)
                raise

            self._append_tool_result(result)

        # 提示只能在本轮全部 tool result 写回后追加，否则会违反消息配对协议。
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
            self.config.token_optimization_enabled
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
            self.config.token_optimization_enabled
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
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=False,
                error="duplicate failed patch call was not executed",
                metadata={
                    "duplicate": True,
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
            self.config.token_optimization_enabled
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
            path = call.arguments.get("path")
            if isinstance(path, str):
                normalized = path.replace("\\", "/")
                if normalized in self.repository_candidates:
                    self._repo_map_candidate_reads.add(normalized)
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
                self._finish_reminder_sent = False
                self._last_patch_failure_reason = None
            else:
                self._set_phase(AgentPhase.PATCH, "patch_attempted")
                self._failed_apply_calls.setdefault(signature, call.id)
                self._consecutive_apply_failures += 1
                self._last_patch_failure_reason = result.error
            return

        if call.name == ReservedToolName.RUN_TESTS.value:
            # 测试本身可能生成文件、刷新缓存或改变临时配置，因此所有只读缓存都失效。
            self._successful_tool_cache.clear()
            self._tests_passed = result.success
            if result.success:
                if self._diff_nonempty:
                    self._set_phase(AgentPhase.FINISH, "tests_and_diff_ready")
            else:
                self._set_phase(AgentPhase.PATCH, "tests_failed")
            return

        if call.name == ReservedToolName.GET_GIT_DIFF.value and result.success:
            output = result.output if isinstance(result.output, dict) else {}
            self._diff_nonempty = bool(str(output.get("diff", "")).strip())
            if self._tests_passed and self._diff_nonempty:
                self._set_phase(AgentPhase.FINISH, "tests_and_diff_ready")

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

    def _append_exploration_guidance_if_needed(self) -> None:
        """探索达到任一软上限后推动收敛，不直接拒绝模型后续的定向读取。"""
        if (
            not self.config.token_optimization_enabled
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
            not self.config.token_optimization_enabled
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
        if not self.config.token_optimization_enabled or not pending:
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
        if "does not change" not in reason and "already applied" not in reason:
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
        presented = self._presenter.present(result)
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
