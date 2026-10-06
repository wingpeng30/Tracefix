"""Safety checks for the one-off archival deletion tool."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "archive_cleanup", Path(__file__).parents[1] / "scripts" / "archive_cleanup.py"
)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


def test_guard_rejects_outside_and_protected_ancestor(tmp_path):
    owned = tmp_path / "owned"
    owned.mkdir()
    keep = owned / "c-result.json"
    keep.write_text("keep")
    with pytest.raises(ValueError, match="outside"):
        cleanup.guard(tmp_path / "unrelated", [owned], [keep])
    with pytest.raises(ValueError, match="protected"):
        cleanup.guard(owned, [owned], [keep])
    with pytest.raises(ValueError, match="protected"):
        cleanup.guard(keep, [owned], [keep])


def test_snapshot_detects_add_remove_and_modify(tmp_path):
    file = tmp_path / "evidence.json"
    file.write_text("original")
    first = cleanup.snapshot(tmp_path)
    file.write_text("modified evidence")
    assert cleanup.snapshot(tmp_path) != first
    changed = cleanup.snapshot(tmp_path)
    added = tmp_path / "extra.txt"
    added.write_text("extra")
    assert cleanup.snapshot(tmp_path) != changed
    before_removal = cleanup.snapshot(tmp_path)
    added.unlink()
    assert cleanup.snapshot(tmp_path) != before_removal


def test_link_detection_without_privileged_windows_symlinks(tmp_path, monkeypatch):
    nested = tmp_path / "nested"
    nested.mkdir()
    original = cleanup.linked
    monkeypatch.setattr(
        cleanup, "linked", lambda info: info.st_ino == nested.stat().st_ino or original(info)
    )
    with pytest.raises(ValueError, match="reparse"):
        cleanup.snapshot(tmp_path)
    with pytest.raises(ValueError, match="reparse"):
        cleanup.remove_owned_tree(nested)
    assert nested.exists()


def test_archive_roundtrip_and_tamper_detection(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "summary.json").write_text(json.dumps({"passed": False}))
    (source / "patch.diff").write_text("original patch")
    workspace = source / "workspace"
    workspace.mkdir()
    (workspace / "generated.txt").write_text("regenerable")
    output = tmp_path / "evidence.zip"
    manifest = cleanup.zip_archive(source, output, important=True)
    assert {r["member"] for r in manifest["files"]} == {"summary.json", "patch.diff"}
    cleanup.verify_zip(manifest)
    with output.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="digest"):
        cleanup.verify_zip(manifest)


def test_full_backup_preserves_workspace_and_safe_removal(tmp_path):
    source = tmp_path / "c-run"
    (source / "workspace").mkdir(parents=True)
    (source / "workspace" / "code.py").write_text("x = 1\n")
    output = tmp_path / "c.zip"
    manifest = cleanup.zip_archive(source, output, full=True)
    assert manifest["files"][0]["member"] == "workspace/code.py"
    cleanup.verify_zip(manifest)
    cleanup.remove_owned_tree(source)
    assert not source.exists()
    assert output.exists()


def test_cached_archive_rejects_changed_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    original = source / "record.json"
    original.write_text("original")
    output = tmp_path / "saved.zip"
    cleanup.zip_archive(source, output, full=True)
    original.write_text("changed")
    with pytest.raises(ValueError, match="source changed"):
        cleanup.zip_archive(source, output, full=True)


def test_cached_archive_rejects_new_evidence(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "record.json").write_text("original")
    output = tmp_path / "saved.zip"
    cleanup.zip_archive(source, output, full=True)
    (source / "new-result.json").write_text("new")
    with pytest.raises(ValueError, match="membership changed"):
        cleanup.zip_archive(source, output, full=True)


def test_real_symlink_does_not_delete_other_directory(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.txt").write_text("keep")
    link = tmp_path / "link"
    try:
        os.symlink(other, link, target_is_directory=True)
    except OSError:
        return  # Deterministic reparse rejection is also covered above.
    with pytest.raises(ValueError, match="reparse"):
        cleanup.guard(link, [tmp_path], [])
    with pytest.raises(ValueError, match="reparse"):
        cleanup.remove_owned_tree(link)
    assert (other / "keep.txt").read_text() == "keep"
