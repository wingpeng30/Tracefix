"""Independent public regression checks for a saved local patch."""

from __future__ import annotations

import hashlib
import json
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from tracefix.messages import ToolCall
from tracefix.provenance import inspect_test_environment
from tracefix.report import _read_run
from tracefix.runtime import RunConfig, TraceFixRunner, config_identity_sha256
from tracefix.tools.builtin import ApplyPatchTool, RunTestsTool


def _target_path(root: Path, target: str) -> Path:
    name = target.split("::", 1)[0]
    path = Path(name)
    if (
        not name or path.is_absolute() or ".." in path.parts
        or "\n" in target or "\r" in target or name.startswith("-")
    ):
        raise ValueError(f"回归目标必须是仓库内相对 pytest 文件或 node ID: {target}")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise ValueError(f"回归目标不在冻结源码中: {target}")
    # Also apply the production pytest command parser's shell and option restrictions.
    RunTestsTool._parse_command(f"pytest -q {shlex.quote(target)}")
    return resolved


def _test(tool: RunTestsTool, target: str, evidence: Path) -> dict[str, Any]:
    tool.evidence_dir = evidence
    result = tool.execute(ToolCall(
        id=uuid4().hex, name="run_tests",
        arguments={"command": f"pytest -q {shlex.quote(target)}"},
    ))
    return result.model_dump(mode="json")


def _status(test: dict[str, Any]) -> str:
    output = test.get("output") or {}
    status = output.get("test_status")
    if status == "passed" and test.get("success") is True:
        return "passed"
    counts = output.get("test_counts") or {}
    if (
        status == "test_failure" and output.get("returncode") == 1
        and isinstance(counts, dict) and counts.get("failures", 0) > 0
        and counts.get("errors", 0) == 0
    ):
        return "failed"
    return "incomplete"


def verify_regressions(run: Path, targets: tuple[str, ...]) -> dict[str, Any]:
    """Run saved and additional targets against fresh base and patched checkouts."""
    run_dir = _read_run(run)
    session = run_dir / "session.json"
    if not session.is_file():
        raise ValueError("运行缺少可核验的 session.json 配置")
    manifest = json.loads(session.read_text(encoding="utf-8"))
    config = RunConfig.model_validate(manifest["config"])
    identity = manifest["identity"]
    if config.execution_backend != "local" or not config.test_target or not config.source_import:
        raise ValueError("追加回归验证仅支持普通本地运行")
    if config_identity_sha256(config) != identity.get("config_sha256"):
        raise ValueError("运行配置身份与 session.json 不符")
    environment_sha = inspect_test_environment(
        config.test_python_executable or sys.executable,
        pythonpath_entries=config.test_pythonpath_entries,
    ).fingerprint_sha256
    if environment_sha != identity.get("test_environment_sha256"):
        raise ValueError("测试解释器及依赖身份与原运行不符")
    source, commit = TraceFixRunner._validate_source_repository(config.repo)
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    if commit != identity.get("source_commit") or commit != result.get("source_commit"):
        raise ValueError("源仓库提交与运行记录不符")
    patch = (run_dir / "patch.diff").read_bytes()
    patch_sha = hashlib.sha256(patch).hexdigest()
    if not patch or patch_sha != result.get("patch_sha256"):
        raise ValueError("补丁为空或与 result.json 身份不符")
    if not targets:
        raise ValueError("至少指定一个追加回归目标")
    if len(set(targets)) != len(targets):
        raise ValueError("回归目标不能重复")
    for target in (config.test_target, *targets):
        _target_path(source, target)

    record_dir = run_dir / "regression-verifications" / uuid4().hex
    record_dir.mkdir(parents=True)
    record: dict[str, Any] = {
        "schema_version": 1, "source_commit": commit,
        "patch_sha256": patch_sha, "config_sha256": identity["config_sha256"],
        "test_environment_sha256": environment_sha,
        "original_target": config.test_target, "regression_targets": list(targets),
        "targets": [], "passed": False, "status": "incomplete",
    }
    try:
        with tempfile.TemporaryDirectory(
            prefix="tf-reg-", dir=run_dir.parent,
        ) as temporary:
            root = Path(temporary)
            base = TraceFixRunner._clone_repository(source, root / "b")
            patched = TraceFixRunner._clone_repository(source, root / "p")
            for workspace in (base, patched):
                preparation = TraceFixRunner._prepare_workspace(config, workspace, root)
                if preparation.get("success") is not True:
                    raise ValueError(f"独立验证环境预检失败: {preparation.get('failure')}")
            applied = ApplyPatchTool(patched).execute(ToolCall(
                id=uuid4().hex, name="apply_patch",
                arguments={"patch": patch.decode("utf-8")},
            ))
            if not applied.success:
                raise ValueError(f"补丁无法应用于原始提交: {applied.error}")
            base_tool = RunTestsTool(
                base, python_executable=config.test_python_executable,
                pythonpath_entries=config.test_pythonpath_entries,
                evidence_dir=record_dir / "base-evidence",
            )
            patched_tool = RunTestsTool(
                patched, python_executable=config.test_python_executable,
                pythonpath_entries=config.test_pythonpath_entries,
                evidence_dir=record_dir / "patched-evidence",
            )
            original = _test(patched_tool, config.test_target, record_dir / "original")
            record["original_test"] = original
            record["original_status"] = _status(original)
            for index, target in enumerate(targets):
                base_test = _test(base_tool, target, record_dir / str(index) / "base")
                patched_test = _test(
                    patched_tool, target, record_dir / str(index) / "patched",
                )
                before, after = _status(base_test), _status(patched_test)
                if "incomplete" in (before, after):
                    outcome = "incomplete"
                elif before == "passed" and after == "failed":
                    outcome = "regression"
                elif before == "failed" and after == "passed":
                    outcome = "fixed"
                elif before == "failed":
                    outcome = "still_failed"
                else:
                    outcome = "preserved"
                record["targets"].append({
                    "target": target, "base_status": before, "patched_status": after,
                    "outcome": outcome, "base_test": base_test, "patched_test": patched_test,
                })
            outcomes = {item["outcome"] for item in record["targets"]}
            record["passed"] = record["original_status"] == "passed" and outcomes <= {
                "preserved", "fixed",
            }
            record["status"] = (
                "incomplete" if (
                    "incomplete" in outcomes or record["original_status"] == "incomplete"
                )
                else "regression" if "regression" in outcomes
                else "failed" if not record["passed"] else "passed"
            )
    except Exception as exc:
        record["error"] = str(exc)
    record_path = record_dir / "record.json"
    record["record_path"] = str(record_path)
    with record_path.open("x", encoding="utf-8") as output:
        json.dump(record, output, ensure_ascii=False, indent=2)
    return record
