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
from tracefix.exceptions import LLMProviderError, LLMResponseFormatError, PreRequestBudgetExceeded
from tracefix.messages import Message, MessageRole
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
LIMITS = {
    "input_tokens": 60000,
    "output_tokens": 8000,
    "requests": 20,
    "tests": 16,
    "active_seconds": 900,
    "timeout_seconds": 60,
    "limit_cny": 20.0,
}
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


def schedule() -> list[dict[str, Any]]:
    rows = []
    for repetition in range(1, 4):
        for index, task in enumerate(TASK_IDS):
            order = ("A", "B", "C") if index % 2 == 0 else ("A", "C", "B")
            offset = (index + repetition - 1) % 3
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


def static_material(source: Path, issue: str, tracked: list[str]) -> str:
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

    def __init__(self, config, *, root: Path, trial: dict, protocol_sha: str, client=None):
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

    def ledger(self) -> dict:
        path = self.root / "requests.json"
        data = (
            read_json(path)
            if path.exists()
            else {
                "schema_version": 1,
                "protocol_sha256": self.protocol_sha,
                "limit_cny": 20.0,
                "requests": [],
            }
        )
        if (
            data.get("schema_version") != 1
            or data.get("protocol_sha256") != self.protocol_sha
            or data.get("limit_cny") != 20.0
        ):
            raise ValueError("ledger identity mismatch")
        if any(row.get("status") != "completed" for row in data["requests"]):
            raise LLMProviderError("unknown prior request; no automatic replay")
        return data

    def complete(self, messages, tools=()):
        ledger = self.ledger()
        previous = [r for r in ledger["requests"] if r["trial_id"] == self.trial["id"]]
        used_input = sum(r["usage"]["input_tokens"] for r in previous)
        used_output = sum(r["usage"]["output_tokens"] for r in previous)
        maximum = 1 if self.trial["arm"] == "A" else LIMITS["requests"]
        if len(previous) >= maximum or time.monotonic() - self.started >= 900:
            write_json(
                self.root / "trials" / self.trial["id"] / "refusal.json",
                {"trial_id": self.trial["id"], "sent": False, "reason": "request/time limit"},
            )
            raise PreRequestBudgetExceeded("request/time limit; request not sent")
        remaining = LIMITS["output_tokens"] - used_output
        if remaining <= 0:
            write_json(
                self.root / "trials" / self.trial["id"] / "refusal.json",
                {"trial_id": self.trial["id"], "sent": False, "reason": "output budget exhausted"},
            )
            raise PreRequestBudgetExceeded("output budget exhausted; request not sent")
        self.config.max_output_tokens = min(remaining, 8000 if maximum == 1 else 2048)
        bound = self.count_input_tokens(messages, tools)
        reservation = (bound * 2 + self.config.max_output_tokens * 8) / 1_000_000
        spent = sum(r["peak_cost_cny"] for r in ledger["requests"])
        if used_input + bound > 60000 or spent + reservation > 20.0:
            refusal = {
                "trial_id": self.trial["id"],
                "sent": False,
                "reason": "input/CNY reservation exceeds remaining budget",
                "input_bound": bound,
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
        if usage.input_tokens > bound or usage.output_tokens > row["output_limit"]:
            row["status"] = "reservation_violation"
        write_json(self.root / "requests.json", ledger)
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
    for _ in range(20):
        response = llm.complete(history, tools.specs())
        history.append(response.message)
        if not response.message.tool_calls:
            write_json(trace, [m.model_dump(mode="json") for m in history])
            return {"status": "completed", "final_output": response.message.content}
        for call in response.message.tool_calls:
            if call.name == "run_tests" and tests >= 16:
                result = ToolResult(
                    call_id=call.id, tool_name=call.name, success=False, error="test limit reached"
                )
            else:
                tests += int(call.name == "run_tests")
                try:
                    result = tools.get(call.name).execute(call)
                except Exception as exc:
                    result = ToolResult(
                        call_id=call.id,
                        tool_name=call.name,
                        success=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
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
    by_task = []
    for task in TASK_IDS:
        row = {"task_id": task}
        for arm in ARMS:
            subset = [r for r in trials if r["task_id"] == task and r["arm"] == arm]
            row[arm] = {
                "started": len(subset),
                "finished": sum(r.get("finished", False) for r in subset),
                "successes": sum(r.get("passed", False) for r in subset),
            }
        by_task.append(row)
    aggregates = {}
    for arm in ARMS:
        subset = [r for r in trials if r["arm"] == arm]
        ids = {r["id"] for r in subset}
        usage = [r for r in requests if r["trial_id"] in ids and r["status"] == "completed"]
        successes = sum(bool(r.get("passed")) for r in subset)
        cost = sum(r["peak_cost_cny"] for r in usage)
        aggregates[arm] = {
            "planned": 30,
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
    for baseline in ("A", "B"):
        matched = [row for row in by_task if all(row[a]["finished"] == 3 for a in (baseline, "C"))]
        values = [(r["C"]["successes"] - r[baseline]["successes"]) / 3 for r in matched]
        if values:
            rng = random.Random(20261005)
            samples = sorted(
                sum(rng.choices(values, k=len(values))) / len(values) for _ in range(10000)
            )
            delta = sum(values) / len(values)
            base_rate = sum(r[baseline]["successes"] for r in matched) / (3 * len(matched))
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
        "complete": len(trials) == 90 and all(r.get("finished") for r in trials),
        "planned": 90,
        "started": len(trials),
        "unstarted": 90 - len(trials),
        "arms": aggregates,
        "comparisons": comparisons,
        "per_task": by_task,
        "unknown_requests": [r["id"] for r in requests if r["status"] != "completed"],
        "interpretation": "Known development tasks; cold start; exploratory, not holdout accuracy.",
    }
