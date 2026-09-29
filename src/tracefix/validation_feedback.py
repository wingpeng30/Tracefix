"""Read-only, redacted diagnosis of the frozen validation-closure comparison."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as element_tree
from collections import Counter
from pathlib import Path
from typing import Any

from tracefix.exceptions import BenchmarkError
from tracefix.p2_protocol import _audit_p2_evidence

_FOCUS_TASKS = frozenset(
    {"pytest-dev__pytest-10081", "pylint-dev__pylint-4661", "pytest-dev__pytest-10356"}
)
_RESOURCE_STOPS = frozenset(
    {"p2_trial_budget_exhausted", "step_limit_exceeded", "benchmark_error"}
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise BenchmarkError("validation feedback artifact is outside experiment") from exc


def _events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"validation feedback trace line {number} is malformed") from exc
        if not isinstance(event, dict) or not isinstance(event.get("event_type"), str):
            raise BenchmarkError(f"validation feedback trace line {number} is incomplete")
        event["_line"] = number
        events.append(event)
    if not events or events[0].get("event_type") != "run_provenance":
        raise BenchmarkError("validation feedback trace lacks run provenance")
    return events


def _selector_tokens(command: Any) -> list[str]:
    if not isinstance(command, list):
        return []
    return [
        token
        for token in command
        if isinstance(token, str)
        and not Path(token).is_absolute()
        and ".py" in token
        and not token.startswith("--")
    ]


def _target_failures(root: Path, verification_path: Path) -> dict[str, Any]:
    payload = json.loads(verification_path.read_text(encoding="utf-8"))
    evidence = payload.get("evidence") or {}
    audit_path = evidence.get("audit_path") if isinstance(evidence, dict) else None
    if not isinstance(audit_path, str):
        return {"failed_tests": None, "junit_sha256": None, "junit_path": None}
    junit = Path(audit_path).with_name("junit.xml")
    if not junit.is_file():
        return {"failed_tests": None, "junit_sha256": None, "junit_path": None}
    names: list[str] = []
    try:
        document = element_tree.parse(junit)
    except element_tree.ParseError as exc:
        raise BenchmarkError("validation feedback target JUnit is malformed") from exc
    for case in document.iter("testcase"):
        if any(child.tag in {"failure", "error"} for child in case):
            names.append(str(case.attrib.get("name", "unknown")))
    return {
        "failed_tests": names,
        "junit_sha256": _sha256(junit),
        "junit_path": _relative(root, junit),
    }


def _analyze_trial(root: Path, row: dict[str, Any], *, focused: bool) -> dict[str, Any]:
    sequence = row["sequence"]
    trial = json.loads((root / "trials" / f"{sequence:03d}.json").read_text(encoding="utf-8"))
    if any(trial.get(key) != row[key] for key in ("sequence", "task_id", "arm", "repetition")):
        raise BenchmarkError(f"validation feedback trial {sequence} identity changed")
    result_path = Path(trial["run_result_path"])
    patch_path = Path(trial["agent_patch_path"])
    verification_path = Path(trial["verification_path"])
    for label, path in (
        ("run_result", result_path),
        ("agent_patch", patch_path),
        ("verification", verification_path),
    ):
        if _sha256(path) != row["artifact_hashes"][label]:
            raise BenchmarkError(f"validation feedback trial {sequence} {label} hash changed")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    trajectory = result_path.with_name("trajectory.jsonl")
    if not trajectory.is_file():
        raise BenchmarkError(f"validation feedback trace missing for trial {sequence}")
    events = _events(trajectory)
    calls: dict[str, dict[str, Any]] = {}
    timeline: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    first_patch_step: int | None = None
    last_patch_step: int | None = None
    last_passing_test_step: int | None = None
    last_nonempty_diff_step: int | None = None
    tests_without_intervening_patch = 0
    last_test_patch_count: int | None = None
    last_test_succeeded: bool | None = None
    reported_patch_count = 0
    invalid_passing_candidates = 0
    selection_only_rejections = 0
    for event in events:
        kind = event["event_type"]
        payload = event.get("payload") or {}
        step = event.get("step")
        if kind == "tool_called":
            call = payload.get("call") or {}
            if isinstance(call, dict):
                calls[str(call.get("id"))] = call
                if call.get("name") == "apply_patch" and first_patch_step is None:
                    first_patch_step = step
        elif kind == "message_added":
            message = payload.get("message") or {}
            if isinstance(message, dict) and (message.get("metadata") or {}).get(
                "kind"
            ) == "validation_required":
                counts["validation_reminders"] += 1
                if focused:
                    timeline.append(
                        {"step": step, "event_id": event.get("id"), "kind": "validation_reminder"}
                    )
        elif kind == "tool_returned":
            returned = payload.get("result") or {}
            if not isinstance(returned, dict):
                raise BenchmarkError(f"trial {sequence} has malformed tool result")
            tool = returned.get("tool_name")
            if not isinstance(tool, str):
                raise BenchmarkError(f"trial {sequence} has unnamed tool result")
            counts[f"{tool}_calls"] += 1
            output = returned.get("output") or {}
            output = output if isinstance(output, dict) else {}
            line: dict[str, Any] = {
                "step": step,
                "event_id": event.get("id"),
                "kind": "tool_result",
                "tool": tool,
                "success": returned.get("success"),
            }
            if tool == "apply_patch":
                if returned.get("success") is True:
                    reported_patch_count += 1
                    last_patch_step = step
                    counts["patch_tool_successes"] += 1
                else:
                    counts["patch_tool_rejections"] += 1
                line["declared_changed_paths"] = output.get("changed_files") or []
                line["error_code"] = (returned.get("metadata") or {}).get("error", {}).get(
                    "code"
                )
                line["error_kind"] = (
                    "no_effect" if "does not change" in str(returned.get("error")) else None
                )
            elif tool == "run_tests":
                status = output.get("test_status")
                last_test_succeeded = returned.get("success") is True and status == "passed"
                line.update(
                    {
                        "test_status": status,
                        "returncode": output.get("returncode"),
                        "diagnostic": output.get("diagnostic"),
                        "selectors": _selector_tokens(output.get("command")),
                        "test_counts": output.get("test_counts"),
                    }
                )
                if returned.get("success") is True and status == "passed":
                    counts["valid_test_passes"] += 1
                    last_passing_test_step = step
                elif output.get("returncode") == 0 and (
                    output.get("test_counts") or {}
                ).get("passed", 0) > 0:
                    invalid_passing_candidates += 1
                    audit = output.get("audit") or {}
                    reports = audit.get("reports") or []
                    reported_nodes = {report.get("nodeid") for report in reports}
                    collected_nodes = set(audit.get("collected_node_ids") or [])
                    by_node: dict[str, set[str]] = {}
                    for report in reports:
                        by_node.setdefault(str(report.get("nodeid")), set()).add(
                            str(report.get("when"))
                        )
                    if (
                        output.get("diagnostic")
                        == "pytest audit lacks complete setup/call/teardown evidence"
                        and collected_nodes - reported_nodes
                        and all(
                            phases == {"setup", "call", "teardown"}
                            for phases in by_node.values()
                        )
                        and (output.get("test_counts") or {}).get("skipped", 0) == 0
                    ):
                        selection_only_rejections += 1
                if last_test_patch_count == reported_patch_count:
                    tests_without_intervening_patch += 1
                last_test_patch_count = reported_patch_count
            elif tool == "get_git_diff":
                diff = output.get("diff")
                line["diff_chars"] = len(diff) if isinstance(diff, str) else None
                if diff:
                    counts["nonempty_diff_views"] += 1
                    last_nonempty_diff_step = step
            if focused:
                timeline.append(line)
    final_patch_empty = patch_path.stat().st_size == 0
    status = result.get("agent_validation_status")
    if status == "verified":
        category = "verified"
    elif counts["apply_patch_calls"] == 0:
        category = "no_patch_attempt"
    elif final_patch_empty:
        category = "empty_final_patch"
    elif counts["run_tests_calls"] == 0:
        category = "no_agent_test"
    elif counts["valid_test_passes"] == 0:
        category = "no_valid_test_pass"
    elif last_test_succeeded is False:
        category = "later_test_failure"
    elif last_patch_step is not None and last_passing_test_step is not None and (
        last_patch_step > last_passing_test_step
    ):
        category = "patch_after_last_pass"
    elif last_nonempty_diff_step is None or (
        last_passing_test_step is not None and last_nonempty_diff_step < last_passing_test_step
    ):
        category = "missing_current_diff_confirmation"
    else:
        category = "unverified_needs_replay"
    verified_evidence_conflict = status == "verified" and (
        final_patch_empty
        or counts["valid_test_passes"] == 0
        or counts["nonempty_diff_views"] == 0
        or (
            last_patch_step is not None
            and last_passing_test_step is not None
            and last_patch_step > last_passing_test_step
        )
    )
    evidence_paths = {
        "run_result": _relative(root, result_path),
        "patch": _relative(root, patch_path),
        "verification": _relative(root, verification_path),
        "trace": _relative(root, trajectory),
    }
    evidence_hashes = {
        **row["artifact_hashes"],
        "trace_current_sha256": _sha256(trajectory),
    }
    record: dict[str, Any] = {
        "sequence": sequence,
        "task_id": row["task_id"],
        "arm": row["arm"],
        "repetition": row["repetition"],
        "independent_passed": row["independent_passed"],
        "agent_validation_status": status,
        "validation_category": category,
        "stop_reason": result.get("stop_reason"),
        "resource_stop": result.get("stop_reason") in _RESOURCE_STOPS,
        "final_patch_empty": final_patch_empty,
        "first_patch_step": first_patch_step,
        "last_patch_step": last_patch_step,
        "last_passing_test_step": last_passing_test_step,
        "last_nonempty_diff_step": last_nonempty_diff_step,
        "tests_without_intervening_reported_patch": tests_without_intervening_patch,
        "invalid_passing_test_candidates": invalid_passing_candidates,
        "selection_only_false_reject_candidates": selection_only_rejections,
        "verified_evidence_conflict": verified_evidence_conflict,
        "potential_false_unverified": category == "unverified_needs_replay",
        "tool_counts": dict(counts),
        "evidence_paths": evidence_paths,
        "evidence_sha256": evidence_hashes,
    }
    if focused:
        record["timeline"] = timeline
        record["verification_reason"] = row["verification_reason"]
        record["changed_paths"] = row["changed_paths"]
        record["target_tests"] = _target_failures(root, verification_path)
        if record["target_tests"]["junit_sha256"]:
            evidence_hashes["target_junit_current_sha256"] = record["target_tests"][
                "junit_sha256"
            ]
    return record


def write_validation_feedback_diagnostic(
    experiment_dir: Path, *, output_dir: Path
) -> tuple[Path, Path, Path]:
    """Analyze existing evidence without touching the batch or provider ledger."""
    root = experiment_dir.expanduser().resolve()
    destination = output_dir.expanduser().resolve()
    if destination == root or root in destination.parents or destination.exists():
        raise BenchmarkError(
            "validation feedback requires a new output directory outside the batch"
        )
    audit = _audit_p2_evidence(root)
    protocol = audit["protocol"]
    if protocol.kind != "p2_validation_closure_protocol" or len(protocol.schedule) != 48:
        raise BenchmarkError("validation feedback requires a 48-position closure protocol")
    if len(audit["rows"]) != 48 or any(not row["evidence_valid"] for row in audit["rows"]):
        raise BenchmarkError("validation feedback requires all 48 trial artifacts to pass audit")
    rows = [
        _analyze_trial(root, row, focused=row["task_id"] in _FOCUS_TASKS)
        for row in audit["rows"]
    ]
    if any(row["verified_evidence_conflict"] for row in rows):
        raise BenchmarkError(
            "validation feedback found verified states without matching trace evidence"
        )
    if len({(row["task_id"], row["arm"], row["repetition"]) for row in rows}) != 48:
        raise BenchmarkError("validation feedback positions conflict")
    focus = [row for row in rows if row["task_id"] in _FOCUS_TASKS]
    if len(focus) != 18:
        raise BenchmarkError("validation feedback lacks the 18 preselected focus cases")
    pairs: list[dict[str, Any]] = []
    for task_id in sorted(_FOCUS_TASKS):
        for repetition in range(1, 4):
            control = next(
                row
                for row in focus
                if row["task_id"] == task_id
                and row["repetition"] == repetition
                and row["arm"] == "no_compaction"
            )
            treatment = next(
                row
                for row in focus
                if row["task_id"] == task_id
                and row["repetition"] == repetition
                and row["arm"] == "validation_closure"
            )
            control_tools = [
                item for item in control["timeline"] if item["kind"] == "tool_result"
            ]
            treatment_tools = [
                item for item in treatment["timeline"] if item["kind"] == "tool_result"
            ]
            first_difference: dict[str, Any] | None = None
            for index in range(max(len(control_tools), len(treatment_tools))):
                c = control_tools[index] if index < len(control_tools) else None
                t = treatment_tools[index] if index < len(treatment_tools) else None
                if c is None or t is None or (c["tool"], c["success"]) != (
                    t["tool"],
                    t["success"],
                ):
                    first_difference = {
                        "tool_result_index": index,
                        "control_event_id": c["event_id"] if c else None,
                        "treatment_event_id": t["event_id"] if t else None,
                        "control_tool": c["tool"] if c else None,
                        "treatment_tool": t["tool"] if t else None,
                    }
                    break
            pairs.append(
                {
                    "task_id": task_id,
                    "repetition": repetition,
                    "control_sequence": control["sequence"],
                    "treatment_sequence": treatment["sequence"],
                    "control_independent_passed": control["independent_passed"],
                    "treatment_independent_passed": treatment["independent_passed"],
                    "control_validation_reminders": control["tool_counts"].get(
                        "validation_reminders", 0
                    ),
                    "treatment_validation_reminders": treatment["tool_counts"].get(
                        "validation_reminders", 0
                    ),
                    "first_tool_result_difference": first_difference,
                }
            )
    trial_records = [
        {key: value for key, value in row.items() if key != "timeline"} for row in rows
    ]
    report = {
        "kind": "p2_validation_feedback_diagnostic",
        "schema_version": 1,
        "execution_commit": protocol.code_commit,
        "protocol_sha256": audit["protocol_sha256"],
        "diagnostic_module_sha256": _sha256(Path(__file__)),
        "positions": 48,
        "valid_evidence_positions": 48,
        "agent_verified": sum(row["agent_validation_status"] == "verified" for row in rows),
        "selection_only_false_reject_candidates": sum(
            row["selection_only_false_reject_candidates"] for row in rows
        ),
        "validation_categories": dict(Counter(row["validation_category"] for row in rows)),
        "focus_case_count": len(focus),
        "focus_cases": focus,
        "focus_pairs": pairs,
        "trial_records": trial_records,
        "notes": [
            "Trace hashes are current-file hashes; the frozen protocol did not "
            "contain a historical trace lock.",
            "A tool-reported patch success does not prove a file changed "
            "in this historical runtime.",
            "Invalid passing-test candidates require individual audit replay "
            "before declaring false rejection.",
            "No historical score or vendor ledger was modified by this diagnostic.",
            "First tool-result differences are observations, not causal attribution.",
        ],
    }
    digest_before = {root / "protocol.json": audit["protocol_sha256"]}
    for row in rows:
        for label in ("run_result", "patch", "verification", "trace"):
            path = root / row["evidence_paths"][label]
            digest_before[path] = (
                row["evidence_sha256"]["trace_current_sha256"]
                if label == "trace"
                else row["evidence_sha256"]["agent_patch" if label == "patch" else label]
            )
        target_tests = row.get("target_tests") or {}
        if target_tests.get("junit_path"):
            digest_before[root / target_tests["junit_path"]] = target_tests["junit_sha256"]
    destination.mkdir(parents=True, exist_ok=False)
    json_path = destination / "validation-feedback.json"
    index_path = destination / "case-index.json"
    markdown_path = destination / "validation-feedback.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol_sha256": audit["protocol_sha256"],
                "cases": [
                    {
                        "sequence": row["sequence"],
                        "task_id": row["task_id"],
                        "paths": row["evidence_paths"],
                        "sha256": row["evidence_sha256"],
                    }
                    for row in rows
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    lines = [
        "# 验证闭环离线诊断",
        "",
        f"原执行提交 `{protocol.code_commit}`；48/48 试次工件有效；Agent 验证状态为已验证 "
        f"{report['agent_verified']}/48。",
        f"历史测试反馈中有 {report['selection_only_false_reject_candidates']} 次仅因未执行的"
        "去选测试仍留在收集集合而被误判无效；本轮仅修正今后的 Agent 审计，旧评分不改。",
        "当前轨迹哈希及相对路径见 `case-index.json`。原协议没有历史轨迹哈希锁，"
        "因此此索引仅证明分析时读取的内容身份。",
        "",
        "## 验证状态分类",
        "",
    ]
    lines.extend(
        f"- `{name}`：{count}"
        for name, count in sorted(report["validation_categories"].items())
    )
    lines.extend(["", "## pytest-10081 六次试次", ""])
    lines.append(
        "| 序号 | 组别 | 补丁调用/工具报告成功 | Agent 测试/有效通过 | "
        "非空 Diff 视图 | 最终补丁 | 停止原因 |"
    )
    lines.append("| ---: | --- | --- | --- | ---: | --- | --- |")
    for row in rows:
        if row["task_id"] != "pytest-dev__pytest-10081":
            continue
        counts = row["tool_counts"]
        lines.append(
            f"| {row['sequence']} | {row['arm']} | {counts.get('apply_patch_calls', 0)}/"
            f"{counts.get('patch_tool_successes', 0)} | {counts.get('run_tests_calls', 0)}/"
            f"{counts.get('valid_test_passes', 0)} | {counts.get('nonempty_diff_views', 0)} | "
            f"{'空' if row['final_patch_empty'] else '非空'} | {row['stop_reason']} |"
        )
    lines.extend(
        [
            "",
            "逐步事件、工具反馈、测试选择器、哈希及另外 12 项重点案例见 JSON。"
            "该报告只描述可观察行为，不推断模型动机。",
            "pytest-10081：第 10 项未尝试补丁或 Agent 测试；第 25 项曾产生两行空白差异，"
            "随后撤回；第 42 项多次收到工具补丁成功反馈但没有非空最终差异。"
            "三项均因试次预算结束，闭环结束提示未在这些轨迹中触发。",
            "Pylint-4661 六次目标断言均指向缓存路径不符。pytest-10356 五次目标断言"
            "在 `test_mark_mro`，补丁位于标记实现，但顺序或返回类型与期望不符。"
            "这些是测试观察，不能直接断言模型意图或代码根因。",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    if any(_sha256(path) != digest for path, digest in digest_before.items()):
        raise BenchmarkError("validation feedback protocol changed during analysis")
    return json_path, markdown_path, index_path
