import os
import subprocess
from pathlib import Path

import pytest

from tracefix.messages import ToolCall
from tracefix.repository_rules.access import ToolAccessPlanner
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.metadata import WorkspaceAccessPaths


def plan(name, **args):
    return ToolAccessPlanner().plan(ToolCall(id="metadata-contract", name=name, arguments=args))


def write(root, path, body="fixture"):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def test_metadata_resolves_every_tool_without_opening_source_or_spawning(tmp_path, monkeypatch):
    write(tmp_path, "src/a.py")
    write(tmp_path, "tests/a.py")
    write(tmp_path, "conftest.py")
    write(tmp_path, "tests/conftest.py")
    write(tmp_path, "pyproject.toml")
    metadata = WorkspaceAccessPaths(tmp_path, index_paths=lambda: ("deleted.py", "src/a.py"))

    def forbidden(*args, **kwargs):
        pytest.fail("metadata planning opened a source body or spawned")

    with monkeypatch.context() as patch:
        for method in ("open", "read_bytes", "read_text"):
            patch.setattr(Path, method, forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        assert metadata.resolve(plan("read_file", path="src/a.py")) == ("src/a.py",)
        assert metadata.resolve(plan("apply_patch", patch="--- /dev/null\n+++ b/new/a.py")) == (
            "new/a.py",)
        assert metadata.resolve(plan("search_code", query="fixture", path="src")) == ("src/a.py",)
        assert set(metadata.resolve(plan("run_tests", command="pytest tests/a.py"))) == {
            "tests/a.py", "conftest.py", "tests/conftest.py", "pytest.ini", "tox.ini",
            "setup.cfg", "pyproject.toml"}
        assert "deleted.py" in metadata.resolve(plan("get_git_diff"))


def test_search_filters_relative_glob_and_reaches_nested_scopes(tmp_path):
    write(tmp_path, "src/a.py")
    write(tmp_path, "src/nested/b.py")
    write(tmp_path, "src/nested/doc.md")
    resolver = WorkspaceAccessPaths(tmp_path)
    assert resolver.resolve(plan("search_code", query="x", path="src", glob="**/*.py")) == (
        "src/a.py", "src/nested/b.py")
    assert resolver.resolve(plan("search_code", query="x", path="src/a.py", glob="*.md")) == (
        "src/a.py",)


def test_lexically_excluded_candidates_are_not_statted(tmp_path, monkeypatch):
    write(tmp_path, ".env", "secret")
    write(tmp_path, ".git/config", "private")
    write(tmp_path, "evidence/raw", "audit")
    write(tmp_path, "__pycache__/unused.py")
    write(tmp_path, "src/a.py")
    actual = Path.lstat

    def lstat(path, *args, **kwargs):
        relative = path.relative_to(tmp_path).as_posix()
        if relative.startswith((".git", ".env", "evidence", "__pycache__")):
            pytest.fail("excluded metadata was inspected")
        return actual(path, *args, **kwargs)

    def safe(target):
        if target.startswith("evidence"):
            raise RuleError("rules_target_is_internal")
        return target

    resolver = WorkspaceAccessPaths(tmp_path, validate_target=safe)
    monkeypatch.setattr(Path, "lstat", lstat)
    assert resolver.resolve(plan("search_code", query="x")) == ("src/a.py",)


def test_deleted_index_paths_are_preserved_without_requiring_preimages(tmp_path):
    write(tmp_path, "present.py")
    resolver = WorkspaceAccessPaths(tmp_path, index_paths=lambda: ("deleted/a.py", "present.py"))
    assert resolver.resolve(plan("get_git_diff")) == ("present.py", "deleted/a.py")
    with pytest.raises(RuleError, match="index_metadata_required"):
        WorkspaceAccessPaths(tmp_path).resolve(plan("get_git_diff"))


def test_default_pytest_enumerates_source_and_explicit_configurations(tmp_path):
    write(tmp_path, "pkg/a.py")
    write(tmp_path, "tests/b.py")
    resolver = WorkspaceAccessPaths(tmp_path)
    assert set(resolver.resolve(plan("run_tests"))) == {
        "pkg/a.py", "tests/b.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"}


@pytest.mark.parametrize("case", ["missing", "directory", "parent", "config", "conftest"])
def test_explicit_invalid_access_is_not_silently_skipped(tmp_path, case):
    if case == "missing":
        request = plan("read_file", path="missing.py")
    elif case == "directory":
        (tmp_path / "directory").mkdir()
        request = plan("read_file", path="directory")
    elif case == "parent":
        write(tmp_path, "file.py")
        request = plan("read_file", path="file.py/child.py")
    elif case == "config":
        (tmp_path / "pytest.ini").mkdir()
        write(tmp_path, "tests/a.py")
        request = plan("run_tests", command="pytest tests/a.py")
    else:
        (tmp_path / "conftest.py").mkdir()
        write(tmp_path, "tests/a.py")
        request = plan("run_tests", command="pytest tests/a.py")
    with pytest.raises(RuleError):
        WorkspaceAccessPaths(tmp_path).resolve(request)


@pytest.mark.parametrize("kind", ["file", "parent", "root"])
def test_symbolic_source_aliases_are_refused_before_content_access(tmp_path, kind):
    write(tmp_path, "actual/a.py")
    link = tmp_path / "alias"
    try:
        link.symlink_to(tmp_path / "actual" if kind != "file" else tmp_path / "actual/a.py",
                        target_is_directory=kind != "file")
    except OSError as exc:
        pytest.skip(f"native symlink unavailable: {exc.winerror if os.name == 'nt' else exc.errno}")
    with pytest.raises(RuleError, match="link_or_reparse"):
        if kind == "root":
            WorkspaceAccessPaths(link)
        else:
            WorkspaceAccessPaths(tmp_path).resolve(plan(
                "read_file", path="alias" if kind == "file" else "alias/a.py"))


def test_directory_mutation_during_metadata_enumeration_is_refused(tmp_path, monkeypatch):
    write(tmp_path, "first.py")
    resolver = WorkspaceAccessPaths(tmp_path)
    actual = Path.iterdir

    def changed(path):
        existing = tuple(actual(path))
        write(tmp_path, "raced.py")
        return iter(existing)

    monkeypatch.setattr(Path, "iterdir", changed)
    with pytest.raises(RuleError, match="changed_during_enumeration"):
        resolver.resolve(plan("search_code", query="x"))


def test_index_traversal_and_unbounded_indexes_fail_closed(tmp_path):
    with pytest.raises(RuleError):
        WorkspaceAccessPaths(tmp_path, index_paths=lambda: ("../secret",)).resolve(
            plan("get_git_diff"))
    with pytest.raises(RuleError, match="path_limit"):
        WorkspaceAccessPaths(tmp_path, index_paths=lambda: ("missing.py",) * 5001).resolve(
            plan("get_git_diff"))


def test_directory_entry_collection_is_bounded_before_inspecting_candidates(tmp_path, monkeypatch):
    resolver = WorkspaceAccessPaths(tmp_path)
    consumed = []

    def entries(path):
        for index in range(10002):
            consumed.append(index)
            yield path / f"file{index}.py"

    monkeypatch.setattr(Path, "iterdir", entries)
    with pytest.raises(RuleError, match="directory_entry_limit"):
        resolver.resolve(plan("search_code", query="x"))
    assert len(consumed) == 10001
