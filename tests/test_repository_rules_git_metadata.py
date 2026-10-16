"""Native metadata plumbing must retain deleted paths without opening source."""

import subprocess
import sys

import pytest

from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.git_metadata import GitMetadataPaths
from tracefix.repository_rules.paths import relative_path


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def repository(root):
    git(root, "init")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    (root / "deleted.py").write_text("never read me", encoding="utf-8")
    (root / "删除.py").write_text("never read me either", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")


def test_head_and_index_include_both_deletion_forms_and_added_names(tmp_path):
    repository(tmp_path)
    git(tmp_path, "rm", "deleted.py")
    (tmp_path / "删除.py").unlink()
    (tmp_path / "added.py").write_text("new", encoding="utf-8")
    git(tmp_path, "add", "added.py")
    assert GitMetadataPaths(tmp_path)() == ("added.py", "deleted.py", "删除.py")


def test_not_a_repository_never_borrows_ancestor(tmp_path):
    repository(tmp_path)
    child = tmp_path / "child"
    child.mkdir()
    with pytest.raises(RuleError, match="rules_git_metadata_wrong_checkout"):
        GitMetadataPaths(child)()


def test_unknown_head_is_not_treated_as_empty(tmp_path):
    git(tmp_path, "init")
    with pytest.raises(RuleError, match="rules_git_metadata_failed"):
        GitMetadataPaths(tmp_path)()


@pytest.mark.parametrize("data", [b"file.py", b"\0", b"../no\0", b".git/config\0", b"\xff\0"])
def test_invalid_name_stream_rejected(data):
    with pytest.raises(RuleError):
        GitMetadataPaths._names(data)


def test_name_stream_limit_and_surrogate_path_rejected():
    with pytest.raises(RuleError, match="rules_access_path_limit"):
        GitMetadataPaths._names(b"a\0" * 5001)
    with pytest.raises(RuleError, match="rules_invalid_relative_path"):
        relative_path("bad\ud800.py")


def test_fixed_plumbing_only_and_no_workspace_reads(tmp_path, monkeypatch):
    paths = GitMetadataPaths(tmp_path)
    calls = []

    def run(*args):
        calls.append(args)
        return (str(tmp_path).encode() + b"\n" if args[0] == "rev-parse" else b"a.py\0")

    monkeypatch.setattr(paths, "_run", run)
    assert paths() == ("a.py",)
    assert calls == [("rev-parse", "--show-toplevel"), ("ls-files", "--cached", "-z"),
                     ("ls-tree", "-r", "--name-only", "-z", "HEAD")]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_large_process_stream_is_drained_but_never_returned_as_valid_metadata(
    tmp_path, monkeypatch, stream,
):
    import tracefix.repository_rules.git_metadata as module

    popen = subprocess.Popen
    size = 8 * 1024 * 1024 + 1 if stream == "stdout" else 16 * 1024 + 1
    code = f"import sys; sys.{stream}.buffer.write(b'x'*{size}); sys.{stream}.flush()"
    spawned = []

    def synthetic_git(command, **kwargs):
        assert command[0] == "git" and command[-2:] == ["ls-files", "--cached"]
        process = popen([sys.executable, "-c", code], **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", synthetic_git)
    with pytest.raises(RuleError, match="rules_git_metadata_output_limit"):
        GitMetadataPaths(tmp_path)._run("ls-files", "--cached")
    assert len(spawned) == 1 and spawned[0].poll() == 0
    assert spawned[0].stdout.closed and spawned[0].stderr.closed


def test_metadata_timeout_kills_owned_process_and_closes_readers(tmp_path, monkeypatch):
    import tracefix.repository_rules.git_metadata as module

    popen = subprocess.Popen
    spawned = []

    def synthetic_git(command, **kwargs):
        process = popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
        wait = process.wait

        def deadline(*args, **options):
            if options.get("timeout") == 10:
                raise subprocess.TimeoutExpired(command, 10)
            return wait(*args, **options)

        monkeypatch.setattr(process, "wait", deadline)
        spawned.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", synthetic_git)
    with pytest.raises(RuleError, match="rules_git_metadata_deadline"):
        GitMetadataPaths(tmp_path)._run("ls-files", "--cached")
    assert spawned[0].poll() is not None
    assert spawned[0].stdout.closed and spawned[0].stderr.closed


def test_git_unavailable_is_stable_and_never_an_empty_index(tmp_path, monkeypatch):
    import tracefix.repository_rules.git_metadata as module

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("git is unavailable")

    monkeypatch.setattr(module.subprocess, "Popen", unavailable)
    with pytest.raises(RuleError, match="rules_git_metadata_start_failed"):
        GitMetadataPaths(tmp_path)()
