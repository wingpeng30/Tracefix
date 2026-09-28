"""Ordinary repository onboarding checks exercise saved evidence and CLI wiring."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tracefix.cli import _ordinary_settings, build_parser, main
from tracefix.onboarding import doctor, export_patch


def _repo(root: Path) -> Path:
    root.mkdir()
    (root / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "test_sample.py").write_text(
        "from sample import VALUE\n\ndef test_value():\n    assert VALUE == 1\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "--all"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "start",
        ],
        cwd=root,
        check=True,
    )
    return root


def test_toml_paths_and_cli_precedence(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    config = tmp_path / "config.toml"
    config.write_text(
        '[run]\nrepo = "repo"\ntask = "fix"\n'
        'test_python = "python.exe"\nsource_import = "sample"\n'
        'test_target = "test_sample.py"\noutput_dir = "saved"\n',
        encoding="utf-8",
    )
    args = build_parser().parse_args(["doctor", "--config", str(config), "--repo", str(repo)])
    settings = _ordinary_settings(args)
    assert settings["repo"] == repo
    assert settings["output_dir"] == tmp_path / "saved"
    monkeypatch.setenv("TRACEFIX_OUTPUT_DIR", str(tmp_path / "env"))
    assert _ordinary_settings(args)["output_dir"] == tmp_path / "env"
    args = build_parser().parse_args(
        ["doctor", "--config", str(config), "--output-dir", str(tmp_path / "cli")]
    )
    assert _ordinary_settings(args)["output_dir"] == tmp_path / "cli"


@pytest.mark.parametrize(
    "content, message",
    [
        ("broken = [", "无法读取 TOML"),
        ("[other]\nrepo = '.'\n", r"\[run\]"),
        ("[run]\nunknown = 1\n", "未知配置字段"),
        ("[run]\nrepo = 42\n", "必须是非空路径"),
    ],
)
def test_toml_rejects_invalid_or_ambiguous_configuration(tmp_path, content, message):
    config = tmp_path / "invalid.toml"
    config.write_text(content, encoding="utf-8")
    args = build_parser().parse_args(["doctor", "--config", str(config)])
    with pytest.raises(ValueError, match=message):
        _ordinary_settings(args)


def test_doctor_text_shows_blocker_and_fix(monkeypatch, capsys):
    monkeypatch.setattr(
        "tracefix.cli.doctor",
        lambda _settings: {
            "ok": False,
            "checks": [
                {"name": "pytest", "ok": False, "detail": "missing", "fix": "install pytest"}
            ],
        },
    )
    assert main(["doctor", "--model", "offline/replay"]) == 2
    output = capsys.readouterr().out
    assert "FAIL pytest: missing" in output
    assert "install pytest" in output


def test_doctor_reports_missing_repo_git_and_credential(tmp_path, monkeypatch):
    monkeypatch.setattr("tracefix.onboarding.shutil.which", lambda _name: None)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("tracefix.onboarding.load_environment_file", lambda _path: None)
    settings = {
        "repo": None,
        "test_python_executable": tmp_path / "absent-python",
        "test_target": "../escape.py",
        "source_import": "not valid",
        "output_dir": tmp_path / "runs",
        "model_name": "deepseek/example",
        "env_file": None,
    }
    checks = doctor(settings)
    failed = {item["name"] for item in checks["checks"] if not item["ok"]}
    assert {
        "git",
        "repository",
        "test_python",
        "pytest",
        "test_target",
        "source_import",
        "credential",
    } <= failed


def test_doctor_reports_unusable_python_and_output_inside_source(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    original_run = subprocess.run

    def fail_interpreter(command, *args, **kwargs):
        if len(command) >= 3 and command[1:3] == ["-c", "import pytest; print(pytest.__version__)"]:
            raise OSError("cannot launch interpreter")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr("tracefix.onboarding.subprocess.run", fail_interpreter)
    checks = doctor(
        {
            "repo": repo,
            "test_python_executable": Path(sys.executable),
            "test_target": "test_sample.py",
            "source_import": "sample",
            "output_dir": repo / "runs",
            "model_name": "offline/replay",
            "env_file": None,
        }
    )
    failed = {item["name"] for item in checks["checks"] if not item["ok"]}
    assert {"pytest", "output"} <= failed


def test_doctor_is_zero_provider_call_and_detects_dirty_repository(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "repo")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")

    def never(*args, **kwargs):
        raise AssertionError("provider client must not be constructed")

    monkeypatch.setattr("tracefix.models.litellm_adapter.LiteLLMAdapter.__init__", never)
    argv = [
        "doctor",
        "--repo",
        str(repo),
        "--source-import",
        "sample",
        "--test-target",
        "test_sample.py",
        "--test-python",
        sys.executable,
        "--json",
    ]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    (repo / "sample.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert main(argv) == 2
    report = json.loads(capsys.readouterr().out)
    assert next(c for c in report["checks"] if c["name"] == "repository")["ok"] is False
    assert "test-only-key" not in json.dumps(report)


def test_doctor_reports_missing_test_environment(tmp_path):
    repo = _repo(tmp_path / "repo")
    checks = doctor(
        {
            "repo": repo,
            "test_python_executable": tmp_path / "absent-python",
            "test_target": "missing_test.py",
            "source_import": "sample",
            "output_dir": tmp_path / "runs",
            "model_name": "offline/replay",
            "env_file": None,
        }
    )
    failed = {item["name"] for item in checks["checks"] if not item["ok"]}
    assert {"test_python", "pytest", "test_target"} <= failed


def test_export_checks_hash_and_does_not_touch_source(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    patch = b"diff --git a/sample.py b/sample.py\n"
    (run / "patch.diff").write_bytes(patch)
    (run / "result.json").write_text(
        json.dumps(
            {
                "source_commit": "a" * 40,
                "changed_files": ["sample.py"],
                "diff_path": str(run / "patch.diff"),
                "patch_sha256": hashlib.sha256(patch).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "export.patch"
    assert export_patch(run, output) == output
    assert output.read_bytes() == patch
    (run / "patch.diff").write_bytes(b"altered")
    with pytest.raises(ValueError, match="SHA-256"):
        export_patch(run, tmp_path / "second.patch")


def test_export_rejects_missing_identity_patch_and_existing_destination(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    result_path = run / "result.json"
    result = {
        "source_commit": "a" * 40,
        "changed_files": ["sample.py"],
        "diff_path": "C:\\saved\\patch.diff",
    }
    result_path.write_text(json.dumps({**result, "source_commit": None}), encoding="utf-8")
    with pytest.raises(ValueError, match="源码提交"):
        export_patch(run, tmp_path / "export.patch")
    result_path.write_text(json.dumps(result), encoding="utf-8")
    with pytest.raises(ValueError, match="patch.diff"):
        export_patch(run, tmp_path / "export.patch")
    (run / "patch.diff").write_bytes(b"patch")
    destination = tmp_path / "export.patch"
    assert main(["export", "--run", str(run), "--output", str(destination)]) == 0
    assert destination.read_bytes() == b"patch"
    with pytest.raises(ValueError, match="尚不存在"):
        export_patch(run, destination)
    (run / "patch.diff").write_bytes(b"")
    with pytest.raises(ValueError, match="没有可导出"):
        export_patch(run, tmp_path / "empty.patch")


def test_recorded_litellm_replay_runs_ordinary_checkout(tmp_path):
    script = Path(__file__).resolve().parents[1] / "examples" / "replay_ordinary.py"
    output = tmp_path / "ordinary"
    completed = subprocess.run(
        [sys.executable, str(script), "--output", str(output)],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    summary = json.loads(completed.stdout)
    assert summary["model_requests"] == 6
    result = json.loads(Path(summary["result"]).read_text(encoding="utf-8"))
    assert result["agent_validation_status"] == "verified"
    assert result["source_commit"]
    assert (
        result["patch_sha256"] == hashlib.sha256(Path(result["diff_path"]).read_bytes()).hexdigest()
    )
    assert Path(summary["report"]).is_file()
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=output / "source",
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )
