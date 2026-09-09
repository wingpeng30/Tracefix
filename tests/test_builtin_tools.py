import json
import subprocess
from pathlib import Path

import pytest

from tracefix import (
    ApplyPatchTool,
    GetGitDiffTool,
    ReadFileTool,
    RunTestsTool,
    SearchCodeTool,
    ToolCall,
    ToolExecutionError,
    ToolValidationError,
    create_default_tool_registry,
)


def run_git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    run_git(tmp_path, "init", "-q")
    run_git(tmp_path, "config", "user.email", "tracefix@example.com")
    run_git(tmp_path, "config", "user.name", "TraceFix Tests")
    (tmp_path / "sample.py").write_text(
        "def add(a, b):\n    return a - b  # BUG\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text(
        "from sample import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    run_git(tmp_path, "add", ".")
    run_git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_default_registry_contains_five_tools(git_workspace: Path) -> None:
    registry = create_default_tool_registry(git_workspace)
    assert registry.names == (
        "search_code",
        "read_file",
        "apply_patch",
        "run_tests",
        "get_git_diff",
    )


def test_tool_constructor_and_workspace_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SearchCodeTool(tmp_path, max_output_chars=0)
    with pytest.raises(ValueError):
        RunTestsTool(tmp_path, default_timeout_seconds=0)
    with pytest.raises(ToolValidationError):
        create_default_tool_registry(tmp_path / "missing")


def test_workspace_maps_git_start_failure(tmp_path: Path, monkeypatch) -> None:
    def fail_to_start(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr("tracefix.tools.builtin.subprocess.run", fail_to_start)
    with pytest.raises(ToolExecutionError) as captured:
        create_default_tool_registry(tmp_path)
    assert captured.value.code == "tool_execution_error"


def test_registry_requires_existing_commit(tmp_path: Path) -> None:
    run_git(tmp_path, "init", "-q")
    with pytest.raises(ToolValidationError):
        create_default_tool_registry(tmp_path)


def test_search_code_returns_locations_and_skips_binary(git_workspace: Path) -> None:
    (git_workspace / "binary.bin").write_bytes(b"BUG\x00hidden")
    result = SearchCodeTool(git_workspace).execute(
        ToolCall(
            id="search-1",
            name="search_code",
            arguments={"query": "bug", "glob": "**/*", "case_sensitive": False},
        )
    )

    assert result.success is True
    assert result.output["matches"][0]["path"] == "sample.py"
    assert result.output["matches"][0]["line"] == 2
    assert all(match["path"] != "binary.bin" for match in result.output["matches"])


def test_search_code_supports_file_scope_limits_and_case(git_workspace: Path) -> None:
    tool = SearchCodeTool(git_workspace, max_output_chars=100)
    exact = tool.execute(
        ToolCall(
            id="search-exact",
            name="search_code",
            arguments={
                "query": "BUG",
                "path": "sample.py",
                "case_sensitive": True,
                "max_results": 1,
            },
        )
    )
    missing = tool.execute(
        ToolCall(
            id="search-missing",
            name="search_code",
            arguments={"query": "bug", "path": "sample.py", "case_sensitive": True},
        )
    )
    assert exact.output["truncated"] is False
    assert len(exact.output["matches"]) == 1
    assert missing.output["matches"] == []


def test_search_code_marks_result_limit_truncation(git_workspace: Path) -> None:
    (git_workspace / "more.py").write_text("# BUG one\n# BUG two\n", encoding="utf-8")
    result = SearchCodeTool(git_workspace).execute(
        ToolCall(
            id="search-limited",
            name="search_code",
            arguments={"query": "BUG", "max_results": 1},
        )
    )
    assert len(result.output["matches"]) == 1
    assert result.output["truncated"] is True


def test_tool_argument_validation_and_name_mismatch(git_workspace: Path) -> None:
    tool = SearchCodeTool(git_workspace)
    with pytest.raises(ToolValidationError):
        tool.execute(ToolCall(id="wrong", name="read_file", arguments={"query": "x"}))
    with pytest.raises(ToolValidationError):
        tool.execute(
            ToolCall(id="extra", name="search_code", arguments={"query": "x", "extra": 1})
        )


def test_read_file_adds_line_numbers_and_limits_range(git_workspace: Path) -> None:
    result = ReadFileTool(git_workspace).execute(
        ToolCall(
            id="read-1",
            name="read_file",
            arguments={"path": "sample.py", "start_line": 2, "end_line": 2},
        )
    )

    assert result.success is True
    assert result.output["start_line"] == 2
    assert result.output["end_line"] == 2
    assert "2 |     return a - b" in result.output["content"]


def test_read_file_reports_truncation_and_rejects_invalid_content(git_workspace: Path) -> None:
    long_file = git_workspace / "long.py"
    long_file.write_text("\n".join(f"line {index}" for index in range(500)), encoding="utf-8")
    limited = ReadFileTool(git_workspace, max_output_chars=80).execute(
        ToolCall(id="read-long", name="read_file", arguments={"path": "long.py"})
    )
    assert limited.output["end_line"] == 400
    assert limited.output["truncated"] is True
    assert "TraceFix 已截断" in limited.output["content"]

    (git_workspace / "binary.bin").write_bytes(b"text\x00binary")
    with pytest.raises(ToolValidationError):
        ReadFileTool(git_workspace).execute(
            ToolCall(id="read-binary", name="read_file", arguments={"path": "binary.bin"})
        )
    with pytest.raises(ToolValidationError):
        ReadFileTool(git_workspace).execute(
            ToolCall(id="read-dir", name="read_file", arguments={"path": "tests"})
        )
    with pytest.raises(ToolValidationError):
        ReadFileTool(git_workspace).execute(
            ToolCall(
                id="read-range",
                name="read_file",
                arguments={"path": "sample.py", "start_line": 3, "end_line": 1},
            )
        )

    (git_workspace / "too_large.txt").write_bytes(b"x" * 1_000_001)
    with pytest.raises(ToolValidationError):
        ReadFileTool(git_workspace).execute(
            ToolCall(id="read-large", name="read_file", arguments={"path": "too_large.txt"})
        )


@pytest.mark.parametrize("unsafe_path", ["../secret.txt", ".git/config"])
def test_file_tools_reject_unsafe_paths(git_workspace: Path, unsafe_path: str) -> None:
    with pytest.raises(ToolValidationError):
        ReadFileTool(git_workspace).execute(
            ToolCall(
                id="read-unsafe",
                name="read_file",
                arguments={"path": unsafe_path},
            )
        )


def test_read_file_rejects_absolute_and_missing_paths(git_workspace: Path) -> None:
    tool = ReadFileTool(git_workspace)
    with pytest.raises(ToolValidationError):
        tool.execute(
            ToolCall(
                id="read-absolute",
                name="read_file",
                arguments={"path": str((git_workspace / "sample.py").resolve())},
            )
        )
    with pytest.raises(ToolValidationError):
        tool.execute(
            ToolCall(id="read-missing", name="read_file", arguments={"path": "missing.py"})
        )


def test_apply_patch_checks_then_modifies_file(git_workspace: Path) -> None:
    patch = """diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b  # BUG
+    return a + b
"""
    tool = ApplyPatchTool(git_workspace)
    result = tool.execute(
        ToolCall(id="patch-1", name="apply_patch", arguments={"patch": patch})
    )

    assert result.success is True
    assert result.output["changed_files"] == ["sample.py"]
    assert "return a + b" in (git_workspace / "sample.py").read_text(encoding="utf-8")

    second = tool.execute(
        ToolCall(id="patch-2", name="apply_patch", arguments={"patch": patch})
    )
    assert second.success is False
    assert second.error == "patch validation failed"


def test_apply_patch_accepts_begin_patch_update_format(git_workspace: Path) -> None:
    """回归真实轨迹中 DeepSeek 生成的无行号 Begin Patch 格式。"""
    patch = """*** Begin Patch
*** Update File: sample.py
@@
-    return a - b  # BUG
+    return a + b
*** End Patch"""

    result = ApplyPatchTool(git_workspace).execute(
        ToolCall(id="patch-begin", name="apply_patch", arguments={"patch": patch})
    )

    assert result.success is True
    assert result.output["changed_files"] == ["sample.py"]
    assert "return a + b" in (git_workspace / "sample.py").read_text(encoding="utf-8")


def test_apply_patch_recounts_incorrect_unified_hunk_lengths(git_workspace: Path) -> None:
    """hunk 内容可匹配时，模型写错的行数元数据不应导致补丁失败。"""
    patch = """diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,9 +1,9 @@
 def add(a, b):
-    return a - b  # BUG
+    return a + b
"""

    result = ApplyPatchTool(git_workspace).execute(
        ToolCall(id="patch-recount", name="apply_patch", arguments={"patch": patch})
    )

    assert result.success is True
    assert "return a + b" in (git_workspace / "sample.py").read_text(encoding="utf-8")


def test_apply_patch_strips_accidental_end_marker(git_workspace: Path) -> None:
    patch = """--- a/sample.py
+++ b/sample.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b  # BUG
+    return a + b
*** End Patch"""
    result = ApplyPatchTool(git_workspace).execute(
        ToolCall(id="patch-marker", name="apply_patch", arguments={"patch": patch})
    )
    assert result.success is True


def test_begin_patch_rejects_unsafe_or_unmatched_updates(git_workspace: Path) -> None:
    original = (git_workspace / "sample.py").read_text(encoding="utf-8")
    unsafe = """*** Begin Patch
*** Update File: ../outside.py
@@
-old
+new
*** End Patch"""
    unmatched = """*** Begin Patch
*** Update File: sample.py
@@
-text that is not present
+replacement
*** End Patch"""

    for index, patch in enumerate((unsafe, unmatched)):
        with pytest.raises(ToolValidationError):
            ApplyPatchTool(git_workspace).execute(
                ToolCall(
                    id=f"patch-begin-invalid-{index}",
                    name="apply_patch",
                    arguments={"patch": patch},
                )
            )
        assert (git_workspace / "sample.py").read_text(encoding="utf-8") == original


def test_apply_patch_rejects_path_traversal(git_workspace: Path) -> None:
    patch = """diff --git a/../outside.txt b/../outside.txt
--- /dev/null
+++ b/../outside.txt
@@ -0,0 +1 @@
+unsafe
"""
    with pytest.raises(ToolValidationError):
        ApplyPatchTool(git_workspace).execute(
            ToolCall(id="patch-unsafe", name="apply_patch", arguments={"patch": patch})
        )


@pytest.mark.parametrize(
    "patch",
    [
        "GIT binary patch\nliteral 0\n",
        "this is not a patch",
        'diff --git "unterminated\n',
    ],
)
def test_apply_patch_rejects_unsupported_or_malformed_input(
    git_workspace: Path, patch: str
) -> None:
    with pytest.raises(ToolValidationError):
        ApplyPatchTool(git_workspace).execute(
            ToolCall(id="patch-invalid", name="apply_patch", arguments={"patch": patch})
        )


def test_run_tests_reports_failure_and_success(git_workspace: Path) -> None:
    tool = RunTestsTool(git_workspace)
    failed = tool.execute(
        ToolCall(
            id="tests-1",
            name="run_tests",
            arguments={"command": "python -m pytest -q"},
        )
    )
    assert failed.success is False
    assert failed.output["returncode"] == 1

    (git_workspace / "sample.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    passed = tool.execute(
        ToolCall(id="tests-2", name="run_tests", arguments={"command": "pytest -q"})
    )
    assert passed.success is True
    assert passed.output["returncode"] == 0


@pytest.mark.parametrize("command", ["pytest -q; echo unsafe", "python -c pass"])
def test_run_tests_rejects_shell_and_non_pytest_commands(
    git_workspace: Path, command: str
) -> None:
    with pytest.raises(ToolValidationError):
        RunTestsTool(git_workspace).execute(
            ToolCall(id="tests-unsafe", name="run_tests", arguments={"command": command})
        )


@pytest.mark.parametrize("command", ["", 'pytest "unterminated'])
def test_run_tests_rejects_empty_or_malformed_commands(
    git_workspace: Path, command: str
) -> None:
    with pytest.raises(ToolValidationError):
        RunTestsTool(git_workspace).execute(
            ToolCall(id="tests-invalid", name="run_tests", arguments={"command": command})
        )


def test_run_tests_reports_timeout(git_workspace: Path) -> None:
    (git_workspace / "tests" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(2)\n",
        encoding="utf-8",
    )
    result = RunTestsTool(git_workspace).execute(
        ToolCall(
            id="tests-timeout",
            name="run_tests",
            arguments={"command": "python -m pytest tests/test_slow.py -q", "timeout_seconds": 0.1},
        )
    )
    assert result.success is False
    assert result.output["timed_out"] is True


def test_get_git_diff_includes_tracked_and_untracked_files(git_workspace: Path) -> None:
    (git_workspace / "sample.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (git_workspace / "new_module.py").write_text("VALUE = 1\n", encoding="utf-8")

    result = GetGitDiffTool(git_workspace).execute(
        ToolCall(id="diff-1", name="get_git_diff", arguments={})
    )

    assert result.success is True
    assert result.output["changed_files"] == ["new_module.py", "sample.py"]
    assert "return a + b" in result.output["diff"]
    assert "new_module.py" in result.output["diff"]
    assert json.dumps(result.model_dump(mode="json"))


def test_get_git_diff_truncates_long_output(git_workspace: Path) -> None:
    (git_workspace / "sample.py").write_text("VALUE = '" + "x" * 1000 + "'\n", encoding="utf-8")
    result = GetGitDiffTool(git_workspace, max_output_chars=100).execute(
        ToolCall(id="diff-short", name="get_git_diff", arguments={"context_lines": 0})
    )
    assert result.success is True
    assert result.output["truncated"] is True
    assert len(result.output["diff"]) == 100
