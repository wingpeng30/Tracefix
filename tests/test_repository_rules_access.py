import hashlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tracefix.exceptions import ToolValidationError
from tracefix.messages import ToolCall
from tracefix.repository_rules.access import ToolAccessPlanner, patch_access_paths
from tracefix.repository_rules.errors import RuleError
from tracefix.test_commands import parse_pytest_command
from tracefix.tools.builtin import (
    ApplyPatchTool,
    GetGitDiffTool,
    ReadFileTool,
    RunTestsTool,
    SearchCodeTool,
)


def call(name, **arguments):
    return ToolCall(id="access-contract", name=name, arguments=arguments)


@pytest.mark.parametrize(("tool", "expected"), [
    (SearchCodeTool, "a90062628e66912b1caeb97ece86598074321e032eef110ec984fd8dae1df629"),
    (ReadFileTool, "b08fb5f91856057b67c64f160889cd4d6cb28db949b55ff0552d3e2626fa1b4b"),
    (ApplyPatchTool, "bc45e3276bbf80a6a38153a297d70bfa55aea8dcef2442f1ee48e1818b1bbe86"),
    (RunTestsTool, "3dcac7925373745dfc3b45690a04070eed726c7cc51ed8a9724e9a9f4f54d2b1"),
    (GetGitDiffTool, "8a014cfcfb9e60368efb5a6da2458f673c1060f1651f5b59a48e6f4cf2320d5a"),
])
def test_existing_provider_argument_schemas_match_accepted_g5(tool, expected):
    raw = json.dumps(tool._SPEC.input_schema, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(raw.encode()).hexdigest() == expected


def test_planning_never_reads_checkout_or_starts_a_process(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("planning performed I/O")

    with monkeypatch.context() as patch:
        for name in ("open", "read_bytes", "read_text", "stat", "lstat", "resolve"):
            patch.setattr(Path, name, forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        patch.setattr(ApplyPatchTool, "_normalize_patch", forbidden)
        planner = ToolAccessPlanner()
        plans = [planner.plan(item) for item in (
            call("read_file", path="src/a.py"),
            call("apply_patch", patch=("*** Begin Patch\n*** Update File: src/a.py\n"
                                       "@@\n-a\n+b\n*** End Patch")),
            call("run_tests", command="pytest tests/test_a.py::test_a -q"),
            call("start_test_job", command="pytest tests/test_b.py -x"),
            call("search_code", query="hello", path="src"),
            call("get_git_diff"),
        )]
    assert plans[0].paths[0].path == "src/a.py"
    assert plans[1].paths[0].path == "src/a.py"
    assert plans[2].paths[0].path == "tests/test_a.py"
    assert plans[3].paths[0].path == "tests/test_b.py"
    assert plans[4].enumeration == "search"
    assert plans[5].enumeration == "changed_files"
    with pytest.raises(FrozenInstanceError):
        plans[0].paths[0].path = "other.py"
    with pytest.raises(FrozenInstanceError):
        plans[0].tool_name = "other"


def test_default_tests_require_enumeration_and_every_configuration_candidate():
    plan = ToolAccessPlanner().plan(call("run_tests"))
    assert plan.enumeration == "test_targets"
    assert [(item.path, item.purpose) for item in plan.paths] == [
        (".", "test"), ("pytest.ini", "config"), ("tox.ini", "config"),
        ("setup.cfg", "config"), ("pyproject.toml", "config"),
    ]
    explicit = ToolAccessPlanner(pytest_config="custom.ini").plan(call("run_tests"))
    assert explicit.paths[-1].path == "custom.ini"
    assert len(explicit.paths) == 2


def test_test_nodes_and_multiple_targets_are_deduplicated_without_losing_order():
    plan = ToolAccessPlanner().plan(call("run_tests", command=(
        "python -m pytest tests/a.py::test_one tests/a.py::test_two tests/b.py -k works")))
    assert [item.path for item in plan.paths if item.purpose == "test"] == [
        "tests/a.py", "tests/b.py"]


def test_search_and_diff_are_explicitly_incomplete_until_metadata_enumeration():
    search = ToolAccessPlanner().plan(call("search_code", query="x"))
    assert search.paths[0].path == "."
    assert search.paths[0].purpose == "search_root"
    assert search.search_glob == "**/*"
    assert search.enumeration == "search"
    diff = ToolAccessPlanner().plan(call("get_git_diff", context_lines=0))
    assert diff.paths == () and diff.enumeration == "changed_files"


@pytest.mark.parametrize("invocation", [
    call("read_file", path="../secret"), call("read_file", path=".env"),
    call("read_file", path="a.py", end_line=0),
    call("read_file", path="a.py", start_line=3, end_line=2),
    call("read_file", path="a.py", origin="host"),
    call("search_code", query="x", path="/host"),
    call("search_code", query="x", glob="[ab].py"),
    call("get_git_diff", context_lines=21),
    call("start_test_job", command="pytest", timeout_seconds="20"),
    call("run_tests", command="pytest ../tests"),
    call("run_tests", command="pytest C:/host/test.py"),
    call("internal_source_export"), call("arbitrary_extension"),
], ids=["traversal", "secret", "line-zero", "reverse-lines", "forged-origin", "absolute",
        "unsupported-glob", "diff-limit", "job-type", "test-traversal", "test-drive",
        "internal-control", "unknown-extension"])
def test_invalid_access_intentions_fail_closed(invocation):
    with pytest.raises(RuleError):
        ToolAccessPlanner().plan(invocation)


@pytest.mark.parametrize("config", ["../cfg", "sub/pytest.ini", ".env", "C:/cfg"])
def test_config_must_be_a_safe_root_file(config):
    with pytest.raises(RuleError):
        ToolAccessPlanner(pytest_config=config)


@pytest.mark.parametrize("command", [
    "pytest -c alternate.ini", "pytest --override-ini=x", "pytest -p unsafe",
    "pytest --basetemp=elsewhere", "pytest; echo nope", "python script.py",
    "pytest --maxfail=0", "pytest --tb=unsafe", "pytest --capture=unsafe",
    "pytest -k", "pytest 'unterminated", "", "pytest --", "pytest --maxfail=101",
])
def test_planner_uses_existing_pytest_rejection_rules(command):
    with pytest.raises(ToolValidationError):
        ToolAccessPlanner().plan(call("run_tests", command=command))


@pytest.mark.parametrize(("command", "tail", "targets"), [
    ("pytest -q", ["-q"], []),
    ("py -m pytest tests/a.py -k 'one or two'", ["tests/a.py", "-k", "one or two"],
     ["tests/a.py"]),
    ("pytest --maxfail=2 --tb=short -p no:cacheprovider",
     ["--maxfail=2", "--tb=short", "-p", "no:cacheprovider"], []),
    ("pytest --basetemp .tracefix-test-tmp --capture tee-sys",
     ["--basetemp", ".tracefix-test-tmp", "--capture", "tee-sys"], []),
])
def test_shared_parser_preserves_argv_and_target_contract(command, tail, targets):
    assert parse_pytest_command(command, "trusted-python") == (
        ["trusted-python", "-m", "pytest", *tail], targets)
    assert RunTestsTool._parse_command_details(command, "trusted-python") == (
        ["trusted-python", "-m", "pytest", *tail], targets)
    assert parse_pytest_command(command)[0][0] == sys.executable


@pytest.mark.parametrize(("patch", "paths"), [
    ("diff --git a/old.py b/new.py\nrename from old.py\nrename to new.py",
     ("new.py", "old.py")),
    ("--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+new", ("new.py",)),
    ("--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old", ("old.py",)),
    ('diff --git "a/a b.py" "b/a b.py"\n--- "a/a b.py"\n+++ "b/a b.py"',
     ("a b.py",)),
    ('--- "a/\\344\\270\\255.py"\n+++ "b/\\344\\270\\255.py"', ("中.py",)),
    ("--- a/a.py\told-date\n+++ b/a.py\tnew-date", ("a.py",)),
    ("*** Begin Patch\n*** Update File: a/original.py\n*** Move to: b/moved.py\n"
     "*** Add File: new.py\n*** Delete File: old.py\n*** End Patch",
     ("a/original.py", "b/moved.py", "new.py", "old.py")),
    ("diff --git a/source.py b/copy.py\ncopy from source.py\ncopy to copy.py",
     ("copy.py", "source.py")),
])
def test_all_patch_source_destination_paths_are_lexical(patch, paths):
    assert patch_access_paths(patch) == paths


@pytest.mark.parametrize("patch", [
    "diff --git a/x b/x extra", 'diff --git "a/x b/x', "--- ../outside\n+++ b/x",
    "*** Begin Patch\n*** Update File: ../outside", "--- a/.git/config\n+++ b/x",
    "*** Add File: .env", "GIT binary patch", "Binary files a/x and b/x differ",
    "@@ -1 +1 @@\n-a\n+b", '--- "a/\\777.py"', '--- "a/\\377.py"',
    '--- "a/\\q.py"', '--- "a/\\n.py"', '--- "a/x"ignored',
    "--- /dev/null\n+++ /dev/null", "*** Move to: C:/outside",
])
def test_ambiguous_or_unsafe_patch_paths_never_proceed_to_preimage(patch):
    with pytest.raises(RuleError):
        patch_access_paths(patch)
