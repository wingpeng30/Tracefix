from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tracefix.models.litellm_adapter import LiteLLMAdapter

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/qualify_public_task.py"
spec = importlib.util.spec_from_file_location("qualify_public_task", SCRIPT)
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


def test_frozen_task_identity_and_preparation_scope():
    manifest = qualification.load_manifest()
    assert manifest["base_commit"] == "8a27da60d70fbfc348b124ce89733d8a7104977a"
    assert manifest["target_counts"][manifest["task_target"]] == 36
    assert manifest["target_counts"][manifest["regression_targets"][0]] == 6
    preparation = (qualification.PACKAGE / "prepare-tests.patch").read_text(encoding="utf-8")
    assert preparation.count("diff --git") == 1
    assert "new file mode" in preparation and "more_itertools/more.py" not in preparation
    reference = (qualification.PACKAGE / "reference-product.patch").read_text(encoding="utf-8")
    assert reference.count("diff --git") == 1 and "tests/" not in reference


def test_tampered_or_escaping_package_is_rejected(tmp_path):
    content = tmp_path / "task.md"
    content.write_text("contract", encoding="utf-8")
    manifest = {"schema_version": 1, "files_sha256": {"task.md": qualification.sha(content)}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert qualification.load_manifest(tmp_path) == manifest
    content.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        qualification.load_manifest(tmp_path)

    manifest["files_sha256"] = {"../outside": "anything"}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        qualification.load_manifest(tmp_path)


def test_git_preparation_cannot_fetch_remote_or_prompt_for_credentials(tmp_path, monkeypatch):
    def capture(_command, **kwargs):
        assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == "file"
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        return b"local object\n"

    monkeypatch.setattr(qualification.subprocess, "check_output", capture)
    assert qualification.git(tmp_path, "rev-parse", "HEAD") == "local object\n"


def test_output_reuse_or_source_nested_output_does_not_overwrite(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    sentinel = output / "qualification.json"
    sentinel.write_text("original", encoding="utf-8")
    assert (
        qualification.main(
            [
                "--source-repo",
                str(tmp_path / "source"),
                "--test-python",
                "missing",
                "--output",
                str(output),
            ]
        )
        == 2
    )
    assert sentinel.read_text() == "original"
    with pytest.raises(ValueError, match="outside"):
        qualification.qualify(tmp_path, Path("missing"), tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_reference_client_exercises_adapter_parsing_without_provider():
    from tracefix import LLMConfig, Message, MessageRole
    from tracefix.tools.base import ToolSpec

    client = qualification.ReferenceClient("a reference patch")
    with qualification.forbid_live_access():
        model = LiteLLMAdapter(LLMConfig(model_name="offline/reference-patch"), client=client)
        messages = [Message(role=MessageRole.USER, content="repair")]
        schema = [
            ToolSpec(
                name="apply_patch",
                description="Apply product patch",
                input_schema={"type": "object"},
            )
        ]
        first = model.complete(messages, tools=schema)
        assert first.message.tool_calls[0].arguments["patch"] == "a reference patch"
        assert not model.complete(messages, tools=schema).message.tool_calls
        with pytest.raises(AssertionError, match="unexpected"):
            client.completion(messages=[{}], tools=[{}])


@pytest.mark.parametrize("status", ["collection_error", "timeout", "test_failure", "skipped"])
def test_qualification_rejects_incomplete_or_wrong_collection(tmp_path, monkeypatch, status):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_task.py").write_text("def test_one(): pass", encoding="utf-8")
    case = tmp_path / "case"
    case.mkdir()
    monkeypatch.setattr(qualification.TraceFixRunner, "_clone_repository", lambda *_args: workspace)
    monkeypatch.setattr(
        qualification.TraceFixRunner,
        "_prepare_workspace",
        lambda *_args: {
            "success": True,
        },
    )
    monkeypatch.setattr(qualification, "source_state", lambda *_args: {"source_sha256": "fixed"})
    monkeypatch.setattr(
        qualification,
        "_test",
        lambda *_args: {
            "success": False,
            "output": {
                "test_status": status,
                "returncode": 1,
                "test_counts": {
                    "tests": 1 if status == "test_failure" else 36,
                    "skipped": int(status == "skipped"),
                    "failures": 1,
                    "errors": 0,
                },
            },
        },
    )
    with pytest.raises(ValueError, match="collection|expected failed|skips"):
        qualification.run_case(
            workspace,
            case,
            {
                "task_target": "test_task.py",
                "source_import": "test_task",
                "regression_targets": [],
                "target_counts": {"test_task.py": 36},
            },
            Path(sys.executable),
            None,
            ("failed",),
        )
    record = json.loads((case / "record.json").read_text(encoding="utf-8"))
    assert not record["passed"] and record["error"]
