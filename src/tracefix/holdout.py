"""Freeze auditable holdout and long-context mechanism sets."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tracefix.benchmark import load_benchmark_tasks
from tracefix.context import ContextConfig, ContextManager
from tracefix.messages import Message, MessageHistory, MessageRole, ToolCall
from tracefix.tools import ApplyPatchTool, ReadFileTool, RunTestsTool, ToolRegistry


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def freeze_holdout(
    candidate_pool: Path,
    behavior_report: Path,
    output_path: Path,
    *,
    per_repository: int = 5,
) -> dict[str, Any]:
    """Select ordinary-qualified tasks in the predeclared candidate order."""
    pool = json.loads(candidate_pool.read_text(encoding="utf-8"))
    behavior = json.loads(behavior_report.read_text(encoding="utf-8"))
    by_id = {str(item.get("task_id")): item for item in behavior}
    records = {item["instance_id"]: item for item in pool.get("selected", ())}
    repositories = tuple(pool["requested_repositories"])
    selected: list[str] = []
    decisions: list[dict[str, Any]] = []
    counts = {repo: 0 for repo in repositories}
    for task_id in pool.get("candidate_order", ()):
        record = records[task_id]
        repo = record["repo"]
        evidence = by_id.get(task_id)
        if evidence is None:
            category = "not_validated"
        elif evidence.get("eligible_for_llm_prescreen") is True:
            category = "ordinary_qualified"
        elif evidence.get("reviewed_collection_failure") is True:
            category = "reviewed_collection_failure"
        elif evidence.get("environment_or_execution_error"):
            category = "environment_blocked"
        else:
            category = "not_qualified"
        accepted = category == "ordinary_qualified" and counts[repo] < per_repository
        if accepted:
            counts[repo] += 1
            selected.append(task_id)
        decisions.append(
            {
                "task_id": task_id,
                "repository": repo,
                "category": category,
                "accepted": accepted,
                "behavior_evidence_present": evidence is not None,
            }
        )
    payload = {
        "kind": "tracefix_holdout_freeze",
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "candidate_pool_sha256": _sha(candidate_pool),
        "behavior_report_sha256": _sha(behavior_report),
        "selection_seed": pool.get("selection_seed"),
        "candidate_order_sha256": pool.get("candidate_order_sha256"),
        "per_repository": per_repository,
        "counts": counts,
        "selected_task_ids": selected,
        "qualified_count": len(selected),
        "target_count": per_repository * len(repositories),
        "deficit": per_repository * len(repositories) - len(selected),
        "decisions": decisions,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def freeze_long_context_mechanism(
    tasks_dir: Path, work_dir: Path, output_path: Path
) -> dict[str, Any]:
    """Replay deterministic context history and verify fixture base/gold behavior."""
    task_rows: list[dict[str, Any]] = []
    for task in load_benchmark_tasks(tasks_dir):
        repo = task.prepare_source_repository(work_dir / task.id)
        contracts = sorted((repo / "contracts").glob("*.md"))
        reader = ReadFileTool(repo)
        history = MessageHistory(
            [
                Message(role=MessageRole.SYSTEM, content="TraceFix long-context mechanism audit"),
                Message(role=MessageRole.USER, content=task.description),
            ]
        )
        read_order: list[str] = []
        contract_paths = [path.relative_to(repo).as_posix() for path in contracts]
        for batch_start in range(0, len(contract_paths), 4):
            calls = tuple(
                ToolCall(
                    id=f"read-contract-{index + 1}",
                    name="read_file",
                    arguments={"path": contract_paths[index]},
                )
                for index in range(batch_start, min(batch_start + 4, len(contract_paths)))
            )
            history.append(Message(role=MessageRole.ASSISTANT, tool_calls=calls))
            for call in calls:
                result = reader.execute(call)
                history.append(
                    Message(
                        role=MessageRole.TOOL,
                        content=result.model_dump_json(),
                        tool_call_id=call.id,
                    )
                )
                read_order.append(str(call.arguments["path"]))
        tools = ToolRegistry([reader]).specs()
        control = ContextManager(ContextConfig(enabled=False)).prepare(history.snapshot(), tools)
        treatment = ContextManager(ContextConfig(enabled=True)).prepare(history.snapshot(), tools)
        tester = RunTestsTool(repo)
        base = tester.execute(
            ToolCall(id="base", name="run_tests", arguments={"command": "pytest -q"})
        )
        patcher = ApplyPatchTool(repo)
        applied = patcher.execute(
            ToolCall(
                id="gold",
                name="apply_patch",
                arguments={"patch": task.gold_patch_path.read_text(encoding="utf-8")},
            )
        )
        gold = tester.execute(
            ToolCall(id="gold-test", name="run_tests", arguments={"command": "pytest -q"})
        )
        task_rows.append(
            {
                "task_id": task.id,
                "task_manifest_sha256": _sha(task.task_dir / "task.json"),
                "gold_patch_sha256": _sha(task.gold_patch_path),
                "contract_count": len(contracts),
                "contract_sha256": {path.name: _sha(path) for path in contracts},
                "read_order": read_order,
                "control": {
                    "estimated_tokens_before": control.estimated_tokens_before,
                    "estimated_tokens_after": control.estimated_tokens_after,
                    "compacted": control.compacted,
                },
                "treatment": {
                    "estimated_tokens_before": treatment.estimated_tokens_before,
                    "estimated_tokens_after": treatment.estimated_tokens_after,
                    "compacted": treatment.compacted,
                    "messages_compacted": treatment.messages_compacted,
                },
                "base_failed": not base.success,
                "gold_applied": applied.success,
                "gold_passed": gold.success,
                "mechanism_valid": (
                    len(contracts) == 24
                    and control.estimated_tokens_before > 32_000
                    and treatment.compacted
                    and treatment.estimated_tokens_after < treatment.estimated_tokens_before
                    and not base.success
                    and applied.success
                    and gold.success
                ),
            }
        )
    payload = {
        "kind": "tracefix_long_context_mechanism_set",
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "context_trigger_tokens": 32_000,
        "estimator": "utf8_json_bytes_divided_by_4_ceiling",
        "purpose": "mechanism_validation_only",
        "included_in_holdout_effect_estimate": False,
        "tasks": task_rows,
        "valid": bool(task_rows) and all(row["mechanism_valid"] for row in task_rows),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
