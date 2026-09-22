"""Freeze auditable holdout and long-context mechanism sets."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as element_tree
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


def _text_sha(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode()).hexdigest()


def _evidence_artifacts(
    evidence: dict[str, Any],
    *,
    source_module: str | None = None,
    expected_run_prefix: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Hash every raw pytest artifact needed to independently check a result."""
    candidates: list[Path] = []
    for stage in ("collection", "execution"):
        process = evidence.get(stage) or {}
        candidates.extend(
            Path(value) for key in ("stdout_path", "stderr_path") if (value := process.get(key))
        )
    if audit_path := evidence.get("audit_path"):
        audit = Path(audit_path)
        candidates.extend(
            (audit, audit.with_name("collection.audit.json"), audit.with_name("junit.xml"))
        )
    hashes: dict[str, str] = {}
    errors: list[str] = []
    run_ids: dict[str, str] = {}
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            errors.append(f"missing_artifact:{path.name}")
            continue
        hashes[path.name] = _sha(path)
    for stage in ("collection", "execution"):
        process = evidence.get(stage) or {}
        raw_audit_path = evidence.get("audit_path")
        if not raw_audit_path:
            errors.append(f"missing_{stage}_audit")
            continue
        audit_path = Path(str(raw_audit_path)).with_name(f"{stage}.audit.json")
        if not audit_path.is_file():
            continue
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            errors.append(f"malformed_{stage}_audit")
            continue
        if not isinstance(audit, dict):
            errors.append(f"malformed_{stage}_audit")
            continue
        run_id = audit.get("run_id")
        suffix = f":{stage}"
        if not isinstance(run_id, str) or not run_id.endswith(suffix) or run_id == suffix:
            errors.append(f"invalid_{stage}_run_id")
        else:
            run_ids[stage] = run_id.removesuffix(suffix)
            if expected_run_prefix and not run_id.startswith(expected_run_prefix):
                errors.append(f"{stage}_run_task_identity_mismatch")
        if audit.get("collection_errors"):
            errors.append(f"{stage}_collection_errors")
        if (
            audit.get("format_version") != 2
            or audit.get("stage") != stage
            or audit.get("completed") is not True
            or audit.get("exitstatus") != process.get("returncode")
        ):
            errors.append(f"inconsistent_{stage}_audit")
        if audit.get("collected_node_ids") != evidence.get("collected_node_ids"):
            errors.append(f"{stage}_collected_nodes_mismatch")
        if stage == "execution":
            reports = audit.get("reports")
            if not isinstance(reports, list):
                errors.append("malformed_execution_reports")
            else:
                keys = [
                    (row.get("nodeid"), row.get("when")) for row in reports if isinstance(row, dict)
                ]
                expected_keys = {
                    (node, phase)
                    for node in evidence.get("executed_node_ids", ())
                    for phase in ("setup", "call", "teardown")
                }
                if len(keys) != len(set(keys)) or set(keys) != expected_keys:
                    errors.append("execution_phase_evidence_mismatch")
                failed_calls = 0
                for row in reports:
                    if not isinstance(row, dict):
                        errors.append("malformed_execution_report")
                        continue
                    outcome = row.get("outcome")
                    if row.get("wasxfail") or outcome not in {"passed", "failed"}:
                        errors.append("nonordinary_execution_outcome")
                    if row.get("when") != "call" and outcome != "passed":
                        errors.append("execution_fixture_failure")
                    failed_calls += row.get("when") == "call" and outcome == "failed"
                if failed_calls != evidence.get("failure_count"):
                    errors.append("execution_failure_count_mismatch")
            working_root = Path(str(process.get("working_directory", ""))).resolve()
            imported = audit.get("imported_source_paths")
            source_paths: list[Path] = []
            if isinstance(imported, dict):
                for name, raw in imported.items():
                    if source_module and not (
                        name == source_module or name.startswith(source_module + ".")
                    ):
                        continue
                    if name == "tracefix_pytest_audit" or not isinstance(raw, str):
                        continue
                    try:
                        path = Path(raw).resolve()
                        path.relative_to(working_root)
                    except (OSError, ValueError):
                        if source_module:
                            errors.append("target_source_outside_checkout")
                        continue
                    source_paths.append(path)
            if not source_paths:
                errors.append("execution_source_identity_missing")
    if len(run_ids) == 2 and len(set(run_ids.values())) != 1:
        errors.append("audit_run_identity_mismatch")
    raw_audit_path = evidence.get("audit_path")
    if not raw_audit_path:
        errors.append("missing_junit")
        return hashes, errors
    junit_path = Path(str(raw_audit_path)).with_name("junit.xml")
    if junit_path.is_file():
        try:
            root = element_tree.parse(junit_path).getroot()
            totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
            for suite in root.iter("testsuite"):
                for key in totals:
                    totals[key] += int(suite.attrib.get(key, "0"))
            if totals != {
                "tests": evidence.get("test_count"),
                "failures": evidence.get("failure_count"),
                "errors": evidence.get("error_count"),
                "skipped": evidence.get("skipped_count"),
            }:
                errors.append("junit_summary_mismatch")
        except (OSError, ValueError, element_tree.ParseError):
            errors.append("malformed_junit")
    probe_path = evidence.get("import_probe_path")
    execution_root = (evidence.get("execution") or {}).get("working_directory")
    if probe_path and execution_root:
        try:
            Path(probe_path).resolve().relative_to(Path(execution_root).resolve())
        except (OSError, ValueError):
            errors.append("source_probe_outside_checkout")
    return hashes, errors


def _validate_ordinary_evidence(
    item: dict[str, Any], record: dict[str, Any]
) -> tuple[bool, list[str], dict[str, str]]:
    errors: list[str] = []
    if item.get("qualification_type") != "assertion_failure":
        errors.append("qualification_type_not_assertion_failure")
    if item.get("eligible_for_llm_prescreen") is not True:
        errors.append("not_marked_eligible")
    if item.get("base_commit") != record.get("base_commit"):
        errors.append("base_commit_mismatch")
    if item.get("dependency_drift_detected") is not False:
        errors.append("dependency_drift")
    fingerprints = {
        (item.get(key) or {}).get("fingerprint_sha256")
        for key in (
            "test_environment",
            "environment_before",
            "environment_after_initial",
            "environment_after_gold",
        )
    }
    if None in fingerprints or len(fingerprints) != 1:
        errors.append("dependency_identity_mismatch")
    artifact_hashes: dict[str, str] = {}
    for variant, expected_status, expected_rc in (
        ("initial", "assertion_failed", 1),
        ("gold", "passed", 0),
    ):
        evidence = item.get(f"{variant}_evidence") or {}
        if evidence.get("status") != expected_status or evidence.get("returncode") != expected_rc:
            errors.append(f"{variant}_result_mismatch")
        for flag in (
            "junit_available",
            "collection_audit_available",
            "execution_audit_available",
            "source_import_audit_valid",
        ):
            if evidence.get(flag) is not True:
                errors.append(f"{variant}_{flag}_missing")
        if evidence.get("timed_out") is not False:
            errors.append(f"{variant}_timed_out")
        if any(
            evidence.get(key, 0)
            for key in ("skipped_count", "xfailed_count", "xpassed_count", "error_count")
        ):
            errors.append(f"{variant}_nonordinary_outcome")
        expected = evidence.get("expected_node_ids") or []
        if (
            not expected
            or expected != evidence.get("collected_node_ids")
            or expected != evidence.get("executed_node_ids")
        ):
            errors.append(f"{variant}_target_set_mismatch")
        source_module = {
            "psf/requests": "requests",
            "pytest-dev/pytest": "_pytest",
            "pylint-dev/pylint": "pylint",
            "sphinx-doc/sphinx": "sphinx",
        }.get(record.get("repo"))
        hashes, artifact_errors = _evidence_artifacts(
            evidence,
            source_module=source_module,
            expected_run_prefix=(
                f"behavior-{item.get('task_id')}-{variant}:" if source_module else None
            ),
        )
        artifact_hashes.update({f"{variant}/{name}": value for name, value in hashes.items()})
        errors.extend(f"{variant}_{error}" for error in artifact_errors)
    if item.get("initial_hidden_failed") is not True or item.get("gold_hidden_passed") is not True:
        errors.append("summary_result_mismatch")
    return not errors, sorted(set(errors)), artifact_hashes


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
    if not isinstance(behavior, list):
        raise ValueError("behavior report must be a list")
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_behavior_ids: list[str] = []
    for item in behavior:
        task_id = str(item.get("task_id"))
        if task_id in by_id:
            duplicate_behavior_ids.append(task_id)
        by_id[task_id] = item
    if duplicate_behavior_ids:
        raise ValueError(f"duplicate behavior records: {sorted(set(duplicate_behavior_ids))}")
    records: dict[str, dict[str, Any]] = {}
    for item in pool.get("selected", ()):
        task_id = item["instance_id"]
        if task_id in records:
            raise ValueError(f"duplicate candidate record: {task_id}")
        records[task_id] = item
    repositories = tuple(pool["requested_repositories"])
    candidate_order = list(pool.get("candidate_order", ()))
    actual_order_hash = _text_sha("\n".join(candidate_order))
    if actual_order_hash != pool.get("candidate_order_sha256"):
        raise ValueError("candidate order hash mismatch")
    if len(candidate_order) != len(set(candidate_order)):
        raise ValueError("candidate order contains duplicates")
    missing_records = sorted(set(candidate_order) - set(records))
    if missing_records:
        raise ValueError(f"candidate order references missing records: {missing_records}")
    selected: list[str] = []
    decisions: list[dict[str, Any]] = []
    counts = {repo: 0 for repo in repositories}
    evidence_hashes: dict[str, dict[str, str]] = {}
    for task_id in candidate_order:
        record = records[task_id]
        repo = record["repo"]
        evidence = by_id.get(task_id)
        validation_errors: list[str] = []
        if evidence is None:
            category = "not_validated"
        elif evidence.get("qualification_type") == "assertion_failure":
            valid, validation_errors, hashes = _validate_ordinary_evidence(evidence, record)
            category = "ordinary_qualified" if valid else "evidence_invalid"
            evidence_hashes[task_id] = hashes
        elif (
            evidence.get("qualification_type") == "expected_collection_failure"
            or evidence.get("reviewed_collection_failure") is True
        ):
            category = "reviewed_collection_failure"
        elif evidence.get("qualification_type") == "environment_blocked":
            category = "environment_blocked"
        elif evidence.get("qualification_type") == "business_not_reproduced":
            category = "not_reproduced"
        else:
            category = "not_qualified"
        accepted = category == "ordinary_qualified" and counts[repo] < per_repository
        if accepted:
            counts[repo] += 1
            selected.append(task_id)
        decision = {
            "task_id": task_id,
            "repository": repo,
            "category": category,
            "accepted": accepted,
            "behavior_evidence_present": evidence is not None,
            "evidence_errors": validation_errors,
        }
        if accepted and evidence is not None:
            initial = evidence.get("initial_evidence", {})
            decision.update(
                {
                    "base_commit": evidence.get("base_commit"),
                    "target_node_ids": initial.get("expected_node_ids", []),
                    "dependency_fingerprint": evidence.get("environment_before", {}).get(
                        "fingerprint_sha256"
                    ),
                }
            )
        decisions.append(decision)
    payload = {
        "kind": "tracefix_holdout_freeze",
        "schema_version": 2,
        "created_at": datetime.now(UTC).isoformat(),
        "candidate_pool_sha256": _sha(candidate_pool),
        "behavior_report_sha256": _sha(behavior_report),
        "selection_seed": pool.get("selection_seed"),
        "candidate_order_sha256": actual_order_hash,
        "per_repository": per_repository,
        "counts": counts,
        "selected_task_ids": selected,
        "qualified_count": len(selected),
        "target_count": per_repository * len(repositories),
        "deficit": per_repository * len(repositories) - len(selected),
        "decisions": decisions,
        "evidence_artifact_sha256": evidence_hashes,
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
        manifest = json.loads((task.task_dir / "task.json").read_text(encoding="utf-8"))
        critical_facts = [
            str(series["footer_template"])
            for series in manifest.get("generated_text_series", ())
            if series.get("footer_template")
        ]
        treatment_wire = "\n".join(message.content or "" for message in treatment.messages)
        normalized_view = treatment_wire.casefold()
        fact_checks = []
        for fact in critical_facts:
            # The prefix contains the generated document index.  Freeze and check the
            # invariant semantic clause, which is identical across all 24 documents.
            semantic_clause = fact.split(": ", 1)[-1]
            fact_checks.append(
                {
                    "fact_template_sha256": _text_sha(fact),
                    "semantic_clause": semantic_clause,
                    "semantic_clause_sha256": _text_sha(semantic_clause),
                    "retained_verbatim": semantic_clause.casefold() in normalized_view,
                }
            )
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
                    "model_view_sha256": _text_sha(treatment_wire),
                },
                "critical_fact_checks": fact_checks,
                "critical_facts_retained": bool(fact_checks)
                and all(check["retained_verbatim"] for check in fact_checks),
                "base_failed": not base.success,
                "gold_applied": applied.success,
                "gold_passed": gold.success,
                "mechanism_valid": (
                    len(contracts) == 24
                    and control.estimated_tokens_before > 32_000
                    and treatment.compacted
                    and treatment.estimated_tokens_after < treatment.estimated_tokens_before
                    and bool(fact_checks)
                    and all(check["retained_verbatim"] for check in fact_checks)
                    and not base.success
                    and applied.success
                    and gold.success
                ),
            }
        )
    payload = {
        "kind": "tracefix_long_context_mechanism_set",
        "schema_version": 2,
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
