from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_new_task_multiple_products_targets_and_invalid_contract(tmp_path):
    manifest = qualification.load_manifest(qualification.TASKS["markdown-quoted-braces-1414"])
    assert len(manifest["products"]) == 2
    assert len(manifest["regression_targets"]) == 2
    assert manifest["diagnostic_expected"] == ["passed", "failed", "passed"]
    manifest["files_sha256"] = {}
    manifest["regression_targets"].append(manifest["task_target"])
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="distinct"):
        qualification.load_manifest(tmp_path)
    manifest["regression_targets"].pop()
    manifest["products"][0]["path"] = "../outside.py"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="product path"):
        qualification.load_manifest(tmp_path)


def test_unreviewed_task_is_rejected_before_creating_output(tmp_path):
    with pytest.raises(ValueError, match="unreviewed"):
        qualification.qualify(tmp_path / "source", Path(sys.executable), tmp_path / "out", "../x")
    assert not (tmp_path / "out").exists()


def test_product_patch_cannot_qualify_by_modifying_tests(tmp_path, monkeypatch):
    from tracefix.tools.base import ToolResult
    from tracefix.tools.builtin import ApplyPatchTool

    monkeypatch.setattr(
        ApplyPatchTool,
        "execute",
        lambda *_args: ToolResult(
            call_id="qualification-product-patch",
            tool_name="apply_patch",
            success=True,
            output={"changed_files": ["tests/test_contract.py"]},
        ),
    )
    with pytest.raises(ValueError, match="outside declared"):
        qualification.apply_product(tmp_path, "mock patch", {"product.py"})


def test_package_unrecorded_file_is_rejected(tmp_path):
    (tmp_path / "task.md").write_text("contract")
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files_sha256": {"task.md": qualification.sha(tmp_path / "task.md")},
            }
        )
    )
    (tmp_path / "unexpected.py").write_text("unexpected")
    with pytest.raises(ValueError, match="unrecorded"):
        qualification.load_manifest(tmp_path)


def test_missing_extension_metadata_is_preflight_blocker(monkeypatch):
    monkeypatch.setattr(
        qualification.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout='{"markdown.extensions": []}'),
    )
    with pytest.raises(ValueError, match="entry-point metadata"):
        qualification.check_task_metadata(
            Path(sys.executable),
            {
                "required_entry_points": {"markdown.extensions": ["attr_list"]},
            },
        )


@pytest.mark.parametrize(
    "message,accepted",
    [
        ("assert 1 == 2", True),
        ("ModuleNotFoundError: attr_list", False),
        ("ImportError: cannot import AssertionError from optional_dep", False),
    ],
)
def test_baseline_body_exception_cannot_count_as_expected_assertion(tmp_path, message, accepted):
    evidence = tmp_path / "junit.xml"
    evidence.write_text(
        f'<testsuite><testcase><failure message="{message}"/></testcase></testsuite>'
    )
    test = {"output": {"junit_path": str(evidence)}}
    if accepted:
        qualification.assert_failure_evidence(test)
    else:
        with pytest.raises(ValueError, match="non-assertion"):
            qualification.assert_failure_evidence(test)


def test_chained_assertion_followed_by_import_error_is_not_a_valid_baseline(tmp_path):
    evidence = tmp_path / "junit.xml"
    evidence.write_text(
        '<testsuite><testcase><failure message="ImportError: later">'
        "E   AssertionError: earlier\nE   ImportError: later"
        "</failure></testcase></testsuite>"
    )
    with pytest.raises(ValueError, match="non-assertion"):
        qualification.assert_failure_evidence({"output": {"junit_path": str(evidence)}})


@pytest.mark.parametrize("dirty", [False, True])
def test_dirty_implementation_or_wrong_parent_stops_before_tools(tmp_path, monkeypatch, dirty):
    monkeypatch.setattr(qualification, "upstream_state", lambda *_args: {"head": "unchanged"})

    def git(_repo, *args):
        if args[0] == "status":
            return " M runtime.py" if dirty else ""
        if args[0] == "rev-list":
            return "reference wrong-parent"
        return "HEAD"

    monkeypatch.setattr(qualification, "git", git)
    monkeypatch.setattr(qualification, "run_case", lambda *_args: pytest.fail("tools must not run"))
    result = qualification.qualify(
        tmp_path / "source",
        Path(sys.executable),
        tmp_path / "output",
        "markdown-quoted-braces-1414",
    )
    assert not result["qualified"]
    assert ("clean tracked" if dirty else "actual parent") in result["error"]


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
