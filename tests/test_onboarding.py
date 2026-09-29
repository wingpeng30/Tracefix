"""Ordinary repository onboarding checks exercise saved evidence and CLI wiring."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tracefix.agent.base import AgentStatus
from tracefix.cli import _ordinary_settings, build_parser, main
from tracefix.onboarding import doctor, export_patch, verify_patch


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


def test_toml_approved_skills_directory_resolves_outside_repository(tmp_path):
    repo = _repo(tmp_path / "repo")
    skills = tmp_path / "approved skills"
    skills.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        '[run]\nrepo = "repo"\ntask = "fix"\nsource_import = "sample"\n'
        'test_target = "test_sample.py"\nskills_dir = "approved skills"\n',
        encoding="utf-8",
    )
    args = build_parser().parse_args(["run", "--config", str(config), "--skills"])
    assert _ordinary_settings(args)["skills_root"] == skills
    settings = _ordinary_settings(args)
    settings["test_python_executable"] = Path(sys.executable)
    settings["output_dir"] = tmp_path / "runs"
    assert next(
        item for item in doctor(settings)["checks"] if item["name"] == "skills_directory"
    )["ok"]
    assert repo.is_dir()


def test_toml_exposes_budget_context_and_skills_settings(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[run]\nrepo = "."\ntask = "fix"\nmax_steps = 4\n'
        'context_trigger_tokens = 1000\nrecord_request_views = true\n'
        'skills = true\nskills_max_active = 2\n', encoding="utf-8",
    )
    args = build_parser().parse_args(["run", "--config", str(config)])
    assert _ordinary_settings(args)["shared_toml"] == {
        "max_steps": 4,
        "context_trigger_tokens": 1000,
        "record_request_views": True,
        "skills": True,
        "skills_max_active": 2,
    }


def test_run_applies_toml_then_environment_then_cli_to_effective_config(
    tmp_path, monkeypatch,
):
    repo = _repo(tmp_path / "repo")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[run]\nrepo = "{repo.as_posix()}"\ntask = "fix"\n'
        'source_import = "sample"\ntest_target = "test_sample.py"\n'
        'max_steps = 4\ncontext_trigger_tokens = 1000\n'
        'record_request_views = true\nskills = true\n', encoding="utf-8",
    )
    seen = []
    monkeypatch.setattr(
        "tracefix.cli.TraceFixRunner.run",
        lambda _self, value: seen.append(value) or type(
            "Completed", (), {"status": AgentStatus.COMPLETED}
        )(),
    )
    monkeypatch.setattr("tracefix.cli._print_run_result", lambda _result: None)
    assert main(["run", "--config", str(config)]) == 0
    assert seen[-1].agent_config.max_steps == 4
    assert seen[-1].agent_config.context.compaction_trigger_tokens == 1000
    assert seen[-1].agent_config.record_request_views is True
    assert seen[-1].agent_config.skills_enabled is True
    monkeypatch.setenv("TRACEFIX_MAX_STEPS", "6")
    assert main(["run", "--config", str(config)]) == 0
    assert seen[-1].agent_config.max_steps == 6
    assert main(["run", "--config", str(config), "--max-steps", "8"]) == 0
    assert seen[-1].agent_config.max_steps == 8


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
        lambda _settings, **_kwargs: {
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


def test_doctor_prepare_uses_temporary_checkout_without_model(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path / "source with spaces")

    def never(*args, **kwargs):
        raise AssertionError("provider client must not be constructed")

    monkeypatch.setattr("tracefix.models.litellm_adapter.LiteLLMAdapter.__init__", never)
    argv = [
        "doctor", "--prepare", "--repo", str(repo), "--source-import", "sample",
        "--test-target", "test_sample.py", "--test-python", sys.executable,
        "--model", "offline/replay", "--json",
    ]
    assert main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert next(c for c in report["checks"] if c["name"] == "prepared_checkout")["ok"]
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True,
        text=True, check=True,
    ).stdout == ""


def test_doctor_prepare_rejects_dependency_that_cannot_import(tmp_path):
    repo = _repo(tmp_path / "source")
    (repo / "sample.py").write_text("import absent_tracefix_fixture_dependency\n", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "missing dependency"], cwd=repo, check=True,
    )
    checks = doctor(
        {
            "repo": repo, "test_python_executable": Path(sys.executable),
            "test_target": "test_sample.py", "source_import": "sample",
            "output_dir": tmp_path / "runs", "model_name": "offline/replay", "env_file": None,
        },
        prepare=True,
    )
    prepared = next(c for c in checks["checks"] if c["name"] == "prepared_checkout")
    assert not prepared["ok"]
    assert (
        "source import probe" in prepared["detail"]
        or "absent_tracefix_fixture_dependency" in prepared["detail"]
    )


def test_doctor_prepare_supports_src_layout_and_rejects_installed_copy(tmp_path):
    repo = tmp_path / "src project"
    (repo / "src" / "sample").mkdir(parents=True)
    (repo / "src" / "sample" / "__init__.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    (repo / "test_sample.py").write_text(
        "from sample import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "src fixture"], cwd=repo, check=True,
    )
    settings = {
        "repo": repo, "test_python_executable": Path(sys.executable),
        "test_target": "test_sample.py", "source_import": "sample",
        "output_dir": tmp_path / "runs", "model_name": "offline/replay", "env_file": None,
    }
    good = doctor(settings, prepare=True)
    assert next(c for c in good["checks"] if c["name"] == "prepared_checkout")["ok"]
    outside = doctor({**settings, "source_import": "pytest"}, prepare=True)
    assert not next(
        c for c in outside["checks"] if c["name"] == "prepared_checkout"
    )["ok"]


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


def test_independent_verification_requires_saved_session_identity(tmp_path):
    run = tmp_path / "older-run"
    run.mkdir()
    (run / "result.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="session.json"):
        verify_patch(run)


def test_doctor_prepare_does_not_clone_when_basic_checks_fail(tmp_path, monkeypatch):
    def never(*_args, **_kwargs):
        raise AssertionError("invalid input must not be cloned")

    monkeypatch.setattr("tracefix.onboarding.TraceFixRunner._clone_repository", never)
    result = doctor(
        {
            "repo": None, "test_python_executable": tmp_path / "missing-python",
            "test_target": "missing.py", "source_import": "missing",
            "output_dir": tmp_path / "runs", "model_name": "offline/replay",
            "env_file": None, "skills_root": tmp_path / "unapproved-skills",
        },
        prepare=True,
    )
    failed = {item["name"] for item in result["checks"] if not item["ok"]}
    assert {"repository", "skills_directory", "prepared_checkout"} <= failed


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
