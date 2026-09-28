from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tracefix.report import main as report_main
from tracefix.report import render_report
from tracefix.reproduction import run_smoke


def _saved_run(root: Path) -> Path:
    run = root / "runs" / "example"
    run.mkdir(parents=True)
    (run / "result.json").write_text(
        json.dumps(
            {
                "model_name": "offline/scripted",
                "status": "completed",
                "agent_validation_status": "passed",
                "source_commit": "abc123",
                "context_metrics": {"compaction_count": 0},
            }
        ),
        encoding="utf-8",
    )
    events = [
        {"event_type": "task_started", "payload": {"task": "<script>alert(1)</script>"}},
        {
            "event_type": "tool_called",
            "payload": {"call": {"id": "a", "name": "run_tests", "arguments": {}}},
        },
        {
            "event_type": "tool_returned",
            "payload": {
                "result": {
                    "call_id": "a",
                    "tool_name": "run_tests",
                    "success": False,
                    "output": {"returncode": 1, "log": "C:\\secret\\file.py"},
                }
            },
        },
        {"event_type": "tool_called", "payload": {"call": {"id": "b", "name": "read_file"}}},
    ]
    (run / "trajectory.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n{broken",
        encoding="utf-8",
    )
    (run / "patch.diff").write_text("+<img src=x onerror=alert(1)>\n-secret\n", encoding="utf-8")
    return run


def test_report_escapes_and_discloses_incomplete_evidence(tmp_path):
    run = _saved_run(tmp_path)
    html = render_report(run).read_text(encoding="utf-8")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "C:\\secret" not in html
    assert "公开测试失败（退出码 1）" in html
    assert "轨迹末尾不完整" in html
    assert "1 个工具调用没有已保存的返回结果" in html
    assert "脚本模型不提供可信 Token 估算" in html
    assert "独立验收：未记录" in html


def test_report_can_be_regenerated_after_copy(tmp_path):
    original = _saved_run(tmp_path / "original")
    copied = tmp_path / "copied" / "runs" / "example"
    shutil.copytree(original, copied)
    target = render_report(tmp_path / "copied", tmp_path / "elsewhere" / "report.html")
    assert target.is_file()
    assert "公开测试失败" in target.read_text(encoding="utf-8")


def test_report_rejects_missing_and_ambiguous_runs(tmp_path):
    with pytest.raises(ValueError, match="exactly one saved run"):
        render_report(tmp_path)
    run = _saved_run(tmp_path)
    (run / "trajectory.jsonl").unlink()
    with pytest.raises(ValueError, match="trajectory.jsonl"):
        render_report(run)
    _saved_run(tmp_path / "another")


def test_report_shows_budget_stop_and_skill_activation_without_usage(tmp_path):
    run = _saved_run(tmp_path)
    result_path = run / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result.update(model_name="provider/example", status="stopped", stop_reason="budget_exceeded")
    result_path.write_text(json.dumps(result), encoding="utf-8")
    trace_path = run / "trajectory.jsonl"
    trace_path.write_text(
        trace_path.read_text(encoding="utf-8").removesuffix("{broken"), encoding="utf-8"
    )
    with trace_path.open("a", encoding="utf-8") as trace:
        trace.write(
            json.dumps(
                {
                    "event_type": "skill_activated",
                    "payload": {"skill_name": "tracefix-debugging", "content_sha256": "a" * 64},
                }
            )
            + "\n"
        )
    html = render_report(run).read_text(encoding="utf-8")
    assert "stopped" in html
    assert "tracefix-debugging" in html
    assert "费用：未记录" in html
    assert "模型 usage：输入 未记录 / 输出 未记录" in html
    result["cost_complete"] = True
    result_path.write_text(json.dumps(result), encoding="utf-8")
    assert "费用：未记录" in render_report(run).read_text(encoding="utf-8")


def test_pagination_demo_records_expected_failure_and_same_patch(tmp_path):
    baseline = run_smoke(tmp_path / "baseline", scenario="pagination")
    skills = run_smoke(tmp_path / "skills", skills_enabled=True, scenario="pagination")
    for report in (baseline, skills):
        assert report["status"] == "completed"
        assert report["test_runs"] == 2
        assert report["changed_files"] == ["catalog/pagination.py"]
        assert report["provider_client_constructions"] == 0
        assert report["provider_request_attempts"] == 0
        assert Path(report["report_path"]).is_file()
        assert report["trajectory_validation"]["tool_names"][-7:] == [
            "run_tests",
            "search_code",
            "read_file",
            "read_file",
            "apply_patch",
            "run_tests",
            "get_git_diff",
        ]
    assert (
        baseline["trajectory_validation"]["diff_sha256"]
        == skills["trajectory_validation"]["diff_sha256"]
    )


def test_report_cli_and_required_evidence_errors(tmp_path, capsys):
    run = _saved_run(tmp_path)
    target = tmp_path / "export" / "report.html"
    assert report_main(["--run", str(run), "--output", str(target)]) == 0
    assert target.is_file()
    assert str(target) in capsys.readouterr().out

    (run / "result.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        render_report(run)
    (run / "result.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        report_main(["--run", str(run)])
    assert exit_info.value.code == 2
    assert "result.json" in capsys.readouterr().err


def test_report_marks_missing_patch_and_other_trajectory_errors(tmp_path):
    run = _saved_run(tmp_path)
    (run / "patch.diff").unlink()
    trace_path = run / "trajectory.jsonl"
    valid = trace_path.read_text(encoding="utf-8").removesuffix("{broken")
    additions = [
        {"event_type": "context_compacted", "payload": {}},
        {"event_type": "tool_returned", "payload": {"result": {"tool_name": "run_tests"}}},
        {"event_type": "error", "payload": {"message": "budget exceeded"}},
        {"event_type": "unknown", "payload": "invalid payload"},
    ]
    trace_path.write_text(
        valid + "".join(json.dumps(event) + "\n" for event in additions), encoding="utf-8"
    )
    html = render_report(run).read_text(encoding="utf-8")
    assert "缺少 patch.diff" in html
    assert "工具 run_tests 有返回但没有配对调用" in html
    assert "budget exceeded" in html
    assert "上下文折叠：1 次" in html
    assert "公开测试未得到退出码" in html

    trace_path.write_text(valid + "{invalid\n" + json.dumps(additions[0]), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid trajectory event on line"):
        render_report(run)
    trace_path.write_text(valid + "[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid trajectory event on line"):
        render_report(run)
