"""Real-checkout regression evidence, using recorded responses and no provider calls."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.cli import main
from tracefix.exceptions import ToolValidationError
from tracefix.onboarding import verify_patch
from tracefix.regression import _status, verify_regressions
from tracefix.report import render_report


def test_regression_verification_preserves_distinct_outcomes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "path with spaces"
    script = Path(__file__).resolve().parents[1] / "examples" / "replay_ordinary.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--output", str(root), "--regression-example"],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    summary = json.loads(completed.stdout)
    run = Path(summary["result"]).parent
    first = json.loads(Path(summary["regression_record"]).read_text(encoding="utf-8"))
    assert first["status"] == "regression", first
    assert first["original_status"] == "passed"
    assert first["targets"][0]["outcome"] == "regression"
    assert "追加回归验证：已保存 1 次" in Path(summary["report"]).read_text(encoding="utf-8")

    second = verify_patch(run, regression_targets=(
        "tests/test_preserved.py", "tests/test_widget.py", "tests/test_still_failed.py",
    ))
    assert second["status"] == "failed"
    assert [item["outcome"] for item in second["targets"]] == [
        "preserved", "fixed", "still_failed",
    ]
    assert first["patch_sha256"] == second["patch_sha256"]
    assert first["source_commit"] == second["source_commit"]
    assert first["test_environment_sha256"] == second["test_environment_sha256"]
    assert first["record_path"] != second["record_path"]
    assert Path(first["record_path"]).is_file()
    assert Path(second["record_path"]).is_file()
    assert "still_failed" in render_report(run).read_text(encoding="utf-8")
    legacy = verify_patch(run)
    assert legacy["passed"] is True
    assert main(["verify", "--run", str(run), "--regression-target", "tests/test_backward.py"]) == 2
    capsys.readouterr()

    missing_node = verify_patch(run, regression_targets=(
        "tests/test_preserved.py::missing_test",
    ))
    assert missing_node["status"] == "incomplete"
    assert missing_node["targets"][0]["outcome"] == "incomplete"

    for invalid in ("../outside.py", "tests/missing.py", "tests/test_widget.py;echo bad"):
        with pytest.raises((ValueError, ToolValidationError)):
            verify_patch(run, regression_targets=(invalid,))
    with pytest.raises(ValueError, match="重复"):
        verify_patch(run, regression_targets=("tests/test_widget.py", "tests/test_widget.py"))
    patch = run / "patch.diff"
    original_patch = patch.read_bytes()
    patch.write_bytes(b"changed")
    with pytest.raises(ValueError, match="补丁"):
        verify_patch(run, regression_targets=("tests/test_preserved.py",))
    assert "记录身份与当前补丁或环境不符" in render_report(run).read_text(encoding="utf-8")
    patch.write_bytes(original_patch)
    session = run / "session.json"
    original_session = session.read_bytes()
    session.rename(run / "session.backup")
    with pytest.raises(ValueError, match="session.json"):
        verify_patch(run, regression_targets=("tests/test_preserved.py",))
    (run / "session.backup").rename(session)
    manifest = json.loads(original_session)
    manifest["identity"]["config_sha256"] = "0" * 64
    session.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="配置身份"):
        verify_patch(run, regression_targets=("tests/test_preserved.py",))
    manifest = json.loads(original_session)
    manifest["identity"]["test_environment_sha256"] = "0" * 64
    session.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="依赖身份"):
        verify_patch(run, regression_targets=("tests/test_preserved.py",))
    session.write_bytes(original_session)
    with pytest.raises(ValueError, match="至少指定"):
        verify_regressions(run, ())
    result_path = run / "result.json"
    original_result = result_path.read_bytes()
    result = json.loads(original_result)
    result["source_commit"] = "0" * 40
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="提交与运行记录"):
        verify_patch(run, regression_targets=("tests/test_preserved.py",))
    result_path.write_bytes(original_result)
    with monkeypatch.context() as patcher:
        patcher.setattr(
            "tracefix.regression.TraceFixRunner._prepare_workspace",
            lambda *_args: {"success": False, "failure": "fixture preparation failure"},
        )
        incomplete = verify_patch(run, regression_targets=("tests/test_preserved.py",))
    assert incomplete["status"] == "incomplete"
    assert "fixture preparation failure" in incomplete["error"]
    with monkeypatch.context() as patcher:
        patcher.setattr(
            "tracefix.regression.ApplyPatchTool.execute",
            lambda *_args: SimpleNamespace(success=False, error="fixture patch failure"),
        )
        incomplete = verify_patch(run, regression_targets=("tests/test_preserved.py",))
    assert "fixture patch failure" in incomplete["error"]
    assert not subprocess.run(
        ["git", "status", "--porcelain"], cwd=root / "source",
        capture_output=True, text=True, check=True,
    ).stdout


def test_uncertain_test_results_do_not_count_as_regressions() -> None:
    assert _status({"success": False, "output": {"timed_out": True}}) == "incomplete"
    assert _status({"success": False, "output": {"test_status": "invalid_test_run"}}) == (
        "incomplete"
    )
