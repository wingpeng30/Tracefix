"""Frozen, cold-start three-arm experiments; no production strategy changes."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from tracefix.agent.base import DEFAULT_SYSTEM_PROMPT
from tracefix.comparison_profiles import (
    HOLDOUT_PROFILE,
    OfficialCounter,
    profile_config,
    protocol_profile,
)
from tracefix.exceptions import LLMProviderError, LLMResponseFormatError, PreRequestBudgetExceeded
from tracefix.messages import Message, MessageRole
from tracefix.models.input_bounds import InputBound
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.tools.base import ToolResult

PRODUCT_BASE = "bc7dd7df4939a374062ea0c7039279de81112cd2"
TASK_IDS = (
    "more-itertools-windowed-empty-462",
    "markdown-quoted-braces-1414",
    "click-completion-resources-2800",
    "psf__requests-1142",
    "psf__requests-1766",
    "pytest-dev__pytest-10051",
    "pytest-dev__pytest-10081",
    "pytest-dev__pytest-10356",
    "sphinx-doc__sphinx-10435",
    "sphinx-doc__sphinx-10449",
)
ARMS = ("A", "B", "C")
LIMITS = profile_config()["limits"]
PATCH_RULE = (
    "只修改产品源码；禁止修改、新增、删除或重命名测试、pytest配置、conftest及setup.cfg。"
    "修复必须保留已有行为，不得跳过测试。"
)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    """A failed replacement preserves the last durable file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def schedule(
    profile: dict | None = None, task_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    selected = profile or profile_config()
    if selected["name"] == HOLDOUT_PROFILE:
        from tracefix.comparison_holdout import balanced_schedule

        return balanced_schedule(task_ids or list(TASK_IDS))
    rows = []
    for repetition in range(1, selected["repetitions"] + 1):
        for index, task in enumerate(TASK_IDS):
            order = ("A", "B", "C") if index % 2 == 0 else ("A", "C", "B")
            if selected["arms"] == ["B", "C"]:
                order = ("B", "C") if index % 2 == 0 else ("C", "B")
            offset = (index + repetition - 1) % len(order)
            if selected["arms"] == ["B", "C"]:
                offset = 0
            for arm in order[offset:] + order[:offset]:
                rows.append(
                    {
                        "id": f"{len(rows) + 1:03d}",
                        "task_id": task,
                        "repetition": repetition,
                        "arm": arm,
                    }
                )
    return rows


def static_material(
    source: Path, issue: str, tracked: list[str], *, full_files: bool = False
) -> str:
    """Issue-only lexical retrieval. Never reads task answer metadata."""
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", issue.casefold()))
    candidates = []
    for name in sorted(tracked):
        relative = Path(name)
        path = source / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or path.is_symlink()
            or not path.resolve().is_relative_to(source.resolve())
            or not name.endswith(".py")
            or any(part.startswith(".") for part in relative.parts)
            or any(part in {"tests", "testing", "__pycache__"} for part in relative.parts)
            or relative.name.startswith("test_")
            or relative.name == "conftest.py"
        ):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        score = sum(min(10, content.casefold().count(token)) for token in tokens)
        score += 30 * sum(token in name.casefold() for token in tokens)
        candidates.append((-score, name, content))
    result = "[共同静态源码材料；按问题词频检索，不代表正确修改位置]\n"
    if full_files:
        remaining = 1_000_000
        result += (
            "[产品源码路径索引]\n" + "\n".join(name for _, name, _ in sorted(candidates)) + "\n"
        )
        for _, name, content in sorted(candidates):
            encoded = content.encode("utf-8")
            if len(encoded) > remaining:
                result += f"\n--- {name} (完整文件未纳入：共同静态材料容量不足) ---\n"
                continue
            result += f"\n--- {name} (完整文件) ---\n{content}\n"
            remaining -= len(encoded)
        return result
    for _, name, content in sorted(candidates)[:6]:
        # UTF-8 truncation is deterministic; total source excerpts <= 18 KiB.
        lines = content.splitlines(keepends=True)
        best = max(
            range(len(lines)),
            key=lambda i: sum(token in lines[i].casefold() for token in tokens),
            default=0,
        )
        start = max(0, best - 12) if len(content.encode()) > 3000 else 0
        excerpt = "".join(lines[start:]).encode()[:3000].decode("utf-8", errors="ignore")
        result += f"\n--- {name} (从第{start + 1}行，可能截断) ---\n{excerpt}\n"
    return result


def extract_patch(content: str) -> str:
    fenced = re.findall(r"```(?:diff|patch)?\s*\n(.*?)```", content, re.DOTALL)
    choices = fenced if fenced else [content]
    patches = [
        text
        for text in choices
        if text.lstrip().startswith(("diff --git ", "*** Begin Patch", "--- a/"))
    ]
    if len(patches) != 1:
        raise ValueError("response must contain exactly one patch")
    return patches[0].strip() + "\n"


class ComparisonBudget(LiteLLMAdapter):
    """Shared durable CNY ledger plus identical per-trial pre-request protection."""

    def __init__(
        self,
        config,
        *,
        root: Path,
        trial: dict,
        protocol_sha: str,
        client=None,
        profile: dict | None = None,
        counter_runtime: dict | None = None,
    ):
        super().__init__(config, client=client)
        if (
            config.model_name != "deepseek/deepseek-flash"
            or config.max_retries != 0
            or config.extra_kwargs
            != {
                "api_base": "https://api.deepseek.com",
                "extra_body": {"thinking": {"type": "disabled"}},
            }
        ):
            raise ValueError("official non-thinking endpoint with no retries required")
        self.root, self.trial, self.protocol_sha = root, trial, protocol_sha
        self.started = time.monotonic()
        self.profile = profile or profile_config()
        self.limits = self.profile["limits"]
        self.counter = OfficialCounter(counter_runtime) if counter_runtime else None
        self.counter_seconds = 0.0
        self.tools_seconds = 0.0

    def ledger(self) -> dict:
        path = self.root / "requests.json"
        data = (
            read_json(path)
            if path.exists()
            else {
                "schema_version": 1,
                "protocol_sha256": self.protocol_sha,
                "limit_cny": self.limits["limit_cny"],
                "requests": [],
            }
        )
        if (
            data.get("schema_version") != 1
            or data.get("protocol_sha256") != self.protocol_sha
            or data.get("limit_cny") != self.limits["limit_cny"]
        ):
            raise ValueError("ledger identity mismatch")
        if data.get("halt_reason"):
            raise LLMProviderError("campaign halted: " + data["halt_reason"])
        if any(row.get("status") != "completed" for row in data["requests"]):
            raise LLMProviderError("unknown prior request; no automatic replay")
        return data

    def count_input_bound(self, messages, tools=()) -> InputBound:
        if self.counter is None:
            raise LLMProviderError("official counter unavailable")
        kwargs = self.request_kwargs(messages, tools)
        started = time.monotonic()
        try:
            count = self.counter.count(kwargs)
        finally:
            self.counter_seconds += time.monotonic() - started
        return InputBound(count.tokens, count.status, count.method, count.identity, digest(kwargs))

    def complete(self, messages, tools=()):
        ledger = self.ledger()
        previous = [r for r in ledger["requests"] if r["trial_id"] == self.trial["id"]]
        used_input = sum(r["usage"]["input_tokens"] for r in previous)
        used_output = sum(r["usage"]["output_tokens"] for r in previous)
        maximum = 1 if self.trial["arm"] == "A" else self.limits["requests"]
        if (
            len(previous) >= maximum
            or time.monotonic() - self.started >= self.limits["active_seconds"]
        ):
            write_json(
                self.root / "trials" / self.trial["id"] / "refusal.json",
                {"trial_id": self.trial["id"], "sent": False, "reason": "request/time limit"},
            )
            raise PreRequestBudgetExceeded("request/time limit; request not sent")
        remaining = self.limits["output_tokens"] - used_output
        if remaining <= 0:
            write_json(
                self.root / "trials" / self.trial["id"] / "refusal.json",
                {"trial_id": self.trial["id"], "sent": False, "reason": "output budget exhausted"},
            )
            raise PreRequestBudgetExceeded("output budget exhausted; request not sent")
        self.config.max_output_tokens = min(
            remaining,
            8000
            if maximum == 1 and self.profile["name"] != HOLDOUT_PROFILE
            else self.profile["per_request_output_tokens"],
        )
        bound = self.count_input_tokens(messages, tools)
        count = None
        admission = bound
        if self.profile["input_counter"] == "official_estimate":
            try:
                count = self.count_input_bound(messages, tools)
                if (
                    count.status != "estimate"
                    or not isinstance(count.tokens, int)
                    or count.tokens <= 0
                ):
                    raise ValueError("official count unavailable")
                admission = count.tokens
            except Exception as exc:
                ledger["halt_reason"] = f"official counter unavailable: {type(exc).__name__}"
                write_json(self.root / "requests.json", ledger)
                write_json(
                    self.root / "trials" / self.trial["id"] / "refusal.json",
                    {"sent": False, "reason": ledger["halt_reason"]},
                )
                raise LLMProviderError(ledger["halt_reason"]) from exc
        spent = sum(r["peak_cost_cny"] for r in ledger["requests"])
        same_arm = [r for r in ledger["requests"] if r.get("arm") == self.trial["arm"]]
        arm_spent = sum(r["peak_cost_cny"] for r in same_arm)
        trial_spent = sum(r["peak_cost_cny"] for r in previous)
        arm_limit, trial_limit = self.profile["arm_limit_cny"], self.profile["trial_limit_cny"]
        if self.profile["capability_mode"]:
            from tracefix.models.input_bounds import DEEPSEEK_FLASH_CONTEXT

            available = min(
                self.limits["limit_cny"] - spent,
                arm_limit - arm_spent if arm_limit is not None else float("inf"),
                trial_limit - trial_spent,
            )
            affordable_output = (round(available * 1_000_000) - bound * 2) // 8
            output_allowance = (
                min(
                    self.config.max_output_tokens,
                    affordable_output,
                    DEEPSEEK_FLASH_CONTEXT - admission,
                )
                if affordable_output > 0 and DEEPSEEK_FLASH_CONTEXT > admission
                else 0
            )
            if output_allowance <= 0:
                reason = "fee or provider context exhausted"
                if self.profile["name"] == HOLDOUT_PROFILE:
                    reason = (
                        "trial_fee_limit" if affordable_output <= 0 else "provider_context_limit"
                    )
                write_json(
                    self.root / "trials" / self.trial["id"] / "refusal.json",
                    {"sent": False, "reason": reason},
                )
                raise PreRequestBudgetExceeded(reason + "; request not sent")
            self.config.max_output_tokens = output_allowance
            if count is not None:
                count = InputBound(
                    count.tokens,
                    count.status,
                    count.method,
                    count.identity,
                    digest(self.request_kwargs(messages, tools)),
                )
        reservation = (bound * 2 + self.config.max_output_tokens * 8) / 1_000_000
        if (
            (
                not self.profile["capability_mode"]
                and used_input + admission > self.limits["input_tokens"]
            )
            or round((spent + reservation) * 1_000_000)
            > round(self.limits["limit_cny"] * 1_000_000)
            or (
                arm_limit is not None
                and round((arm_spent + reservation) * 1_000_000) > round(arm_limit * 1_000_000)
            )
            or (
                trial_limit is not None
                and round((trial_spent + reservation) * 1_000_000) > round(trial_limit * 1_000_000)
            )
        ):
            refusal = {
                "trial_id": self.trial["id"],
                "sent": False,
                "reason": "input/CNY reservation exceeds remaining budget",
                "input_bound": bound,
                "input_admission": admission,
                "reserved_peak_cny": reservation,
            }
            write_json(self.root / "trials" / self.trial["id"] / "refusal.json", refusal)
            raise PreRequestBudgetExceeded(refusal["reason"])
        kwargs = self.request_kwargs(messages, tools)
        request_id = f"{self.trial['id']}-{len(previous) + 1:02d}"
        request_file = self.root / "provider" / f"{request_id}-request.json"
        write_json(request_file, kwargs)
        row = {
            "trial_id": self.trial["id"],
            "arm": self.trial["arm"],
            "input_admission": admission,
            "official_count": None
            if count is None
            else {
                "tokens": count.tokens,
                "status": count.status,
                "method": count.method,
                "identity": count.identity,
                "request_sha256": count.request_sha256,
            },
            "id": request_id,
            "status": "pending",
            "request_sha256": file_sha(request_file),
            "input_bound": bound,
            "input_bound_kind": "legacy_utf8_bytes_plus_1024_conservative_reservation",
            "reserved_peak_cny": reservation,
            "output_limit": self.config.max_output_tokens,
        }
        ledger["requests"].append(row)
        write_json(self.root / "requests.json", ledger)
        # Any failure after this point leaves pending: transport/usage is unknown.
        started = time.monotonic()
        if count is not None and hasattr(self.client, "fixture_input_tokens"):
            # Explicit zero-provider fixtures emulate usage for admission tests only.
            self.client.fixture_input_tokens = count.tokens
        try:
            response = super().complete(messages, tools)
        except LLMResponseFormatError as exc:
            write_json(
                self.root / "provider" / f"{request_id}-response-error.json",
                {"error": str(exc), "context": exc.context},
            )
            raise
        response_file = self.root / "provider" / f"{request_id}-response.json"
        write_json(response_file, response.model_dump(mode="json"))
        usage = response.usage
        cost = (usage.input_tokens * 2 + usage.output_tokens * 8) / 1_000_000
        row.update(
            status="completed",
            usage=usage.model_dump(mode="json"),
            peak_cost_cny=cost,
            response_sha256=file_sha(response_file),
            response_model=response.model_name,
            seconds=time.monotonic() - started,
        )
        if self.profile["name"] == HOLDOUT_PROFILE:
            row["supplier_raw_usage"] = response.raw_response.get("usage", {})
            if self.root.joinpath("protocol.json").exists():
                mode = read_json(self.root / "protocol.json").get("mode")
                if mode == "live" and response.model_name != "deepseek-flash":
                    ledger["halt_reason"] = "supplier model identity changed"
        if count is not None:
            row["official_count_delta"] = count.tokens - usage.input_tokens
            if row["official_count_delta"] != 0:
                ledger["halt_reason"] = "official count differs from supplier usage"
            if used_input + usage.input_tokens > self.limits["input_tokens"]:
                ledger["halt_reason"] = "actual cumulative input exceeds trial limit"
        if usage.input_tokens > bound or usage.output_tokens > row["output_limit"]:
            row["status"] = "reservation_violation"
        write_json(self.root / "requests.json", ledger)
        if ledger.get("halt_reason"):
            raise LLMProviderError(ledger["halt_reason"])
        if row["status"] != "completed":
            raise LLMProviderError("supplier usage exceeds reservation; stop campaign")
        return response


def simple_loop(llm: ComparisonBudget, tools, task: str, trace: Path) -> dict:
    """No phase policy, reminders, caching, compression, repo map or retry."""
    history = [
        Message(role=MessageRole.SYSTEM, content=DEFAULT_SYSTEM_PROMPT),
        Message(role=MessageRole.USER, content=task),
    ]
    tests = 0
    limits = getattr(llm, "limits", LIMITS)
    for _ in range(limits["requests"]):
        response = llm.complete(history, tools.specs())
        history.append(response.message)
        if not response.message.tool_calls:
            write_json(trace, [m.model_dump(mode="json") for m in history])
            return {"status": "completed", "final_output": response.message.content}
        for call in response.message.tool_calls:
            if call.name == "run_tests" and tests >= limits["tests"]:
                result = ToolResult(
                    call_id=call.id, tool_name=call.name, success=False, error="test limit reached"
                )
            else:
                tests += int(call.name == "run_tests")
                started = time.monotonic()
                try:
                    result = tools.get(call.name).execute(call)
                except Exception as exc:
                    result = ToolResult(
                        call_id=call.id,
                        tool_name=call.name,
                        success=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                finally:
                    if hasattr(llm, "tools_seconds"):
                        llm.tools_seconds += time.monotonic() - started
            history.append(
                Message(
                    role=MessageRole.TOOL,
                    tool_call_id=call.id,
                    content=json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
                )
            )
        write_json(trace, [m.model_dump(mode="json") for m in history])
    return {"status": "budget_exhausted", "reason": "request limit"}


def summarize(protocol: dict, trials: list[dict], requests: list[dict]) -> dict:
    selected = protocol_profile(protocol)
    if selected["name"] == HOLDOUT_PROFILE:
        from tracefix.comparison_holdout import holdout_summary

        return holdout_summary(protocol, trials, requests)
    arms = selected["arms"]
    repetitions = selected["repetitions"]
    planned = len(protocol.get("schedule", schedule(selected)))
    by_task = []
    for task in TASK_IDS:
        row = {"task_id": task}
        for arm in arms:
            subset = [r for r in trials if r["task_id"] == task and r["arm"] == arm]
            row[arm] = {
                "started": len(subset),
                "finished": sum(r.get("finished", False) for r in subset),
                "successes": sum(r.get("passed", False) for r in subset),
            }
        by_task.append(row)
    aggregates = {}
    for arm in arms:
        subset = [r for r in trials if r["arm"] == arm]
        ids = {r["id"] for r in subset}
        usage = [r for r in requests if r["trial_id"] in ids and r["status"] == "completed"]
        successes = sum(bool(r.get("passed")) for r in subset)
        cost = sum(r["peak_cost_cny"] for r in usage)
        aggregates[arm] = {
            "planned": len(TASK_IDS) * repetitions,
            "normal_completions": sum(r.get("status") == "completed" for r in subset),
            "completed_and_passed": sum(
                r.get("status") == "completed" and r.get("passed", False) for r in subset
            ),
            "started": len(subset),
            "successes": successes,
            "success_rate": successes / len(subset) if subset else None,
            "input_tokens": sum(r["usage"]["input_tokens"] for r in usage),
            "output_tokens": sum(r["usage"]["output_tokens"] for r in usage),
            "conservative_peak_cny": cost,
            "cost_per_success_cny": cost / successes if successes else None,
            "seconds": sum(r.get("seconds", 0) for r in subset),
            "infrastructure_failures": sum(bool(r.get("infrastructure_failure")) for r in subset),
            "failure_reasons": dict(
                Counter(
                    r.get("reason")
                    or (r.get("verification") or {}).get("reason")
                    or (
                        "independent_test_failed"
                        if r.get("status") == "completed"
                        else r.get("status")
                    )
                    for r in subset
                    if not r.get("passed")
                )
            ),
        }
    comparisons = {}
    for baseline in [a for a in arms if a != "C"]:
        matched = [
            row
            for row in by_task
            if all(row[a]["finished"] == repetitions for a in (baseline, "C"))
        ]
        values = [(r["C"]["successes"] - r[baseline]["successes"]) / repetitions for r in matched]
        if values:
            rng = random.Random(20261005)
            samples = sorted(
                sum(rng.choices(values, k=len(values))) / len(values) for _ in range(10000)
            )
            delta = sum(values) / len(values)
            base_rate = sum(r[baseline]["successes"] for r in matched) / (
                repetitions * len(matched)
            )
            comparisons[f"C-{baseline}"] = {
                "matched_tasks": len(matched),
                "percentage_point_difference": delta * 100,
                "bootstrap_95_percent_points": [samples[249] * 100, samples[9749] * 100],
                "relative_improvement": delta / base_rate if base_rate else None,
                "wins": sum(v > 0 for v in values),
                "losses": sum(v < 0 for v in values),
                "ties": sum(v == 0 for v in values),
            }
        else:
            comparisons[f"C-{baseline}"] = {"matched_tasks": 0, "difference": None}
    return {
        "protocol_sha256": digest(protocol),
        "mode": protocol["mode"],
        "complete": len(trials) == planned and all(r.get("finished") for r in trials),
        "planned": planned,
        "started": len(trials),
        "unstarted": planned - len(trials),
        "arms": aggregates,
        "comparisons": comparisons,
        "per_task": by_task,
        "unknown_requests": [r["id"] for r in requests if r["status"] != "completed"],
        "interpretation": "Known development tasks; cold start; exploratory, not holdout accuracy.",
    }
