from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.jobs import evidence
from tracefix.jobs.evidence import export, validate


def test_bounded_audit_transfer_preserves_nested_receipts_and_raw_bytes(tmp_path):
    root = tmp_path / "evidence"
    (root / "workers/epoch").mkdir(parents=True)
    (root / "workers/epoch/checkpoint.json").write_bytes(b'{"status":"cancelled"}')
    (root / "audit.xml").write_bytes(b"\xff\x00\xe4\xb8\xad")
    raw = export(root)
    assert validate(raw) == {
        "audit.xml": b"\xff\x00\xe4\xb8\xad",
        "workers/epoch/checkpoint.json": b'{"status":"cancelled"}',
    }
    assert export(root) == raw


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/drive", "a\\b", ".",
                                 "a:stream", "CON", "a/../b", "a//b", "line\nbreak"])
def test_untrusted_transfer_paths_cannot_reach_host(tmp_path, name):
    (tmp_path / "safe").write_bytes(b"x")
    payload = json.loads(export(tmp_path))
    payload["files"][0]["name"] = name
    with pytest.raises(ValueError, match="path"):
        validate(json.dumps(payload).encode())


@pytest.mark.parametrize("change", ["digest", "size", "duplicate", "ancestor", "version",
                                   "extra", "base64"])
def test_tampered_audit_transfer_rejects_whole_manifest(tmp_path, change):
    (tmp_path / "a").write_bytes(b"x")
    payload = json.loads(export(tmp_path))
    row = payload["files"][0]
    if change == "digest":
        row["sha256"] = "0" * 64
    elif change == "size":
        payload["bytes"] = 0
    elif change == "duplicate":
        payload["files"].append(dict(row, name="A"))
    elif change == "ancestor":
        payload["files"].append(dict(row, name="a/child"))
        payload["bytes"] = 2
    elif change == "version":
        payload["version"] = True
    elif change == "extra":
        payload["unexpected"] = True
    else:
        row["data"] = "!not-base64"
    with pytest.raises(ValueError):
        validate(json.dumps(payload).encode())


def test_audit_quotas_apply_to_export_and_import(tmp_path, monkeypatch):
    (tmp_path / "a").write_bytes(b"0123456789")
    raw = export(tmp_path)
    monkeypatch.setattr(evidence, "MAX_BYTES", 9)
    with pytest.raises(ValueError, match="quota"):
        export(tmp_path)
    with pytest.raises(ValueError, match="size"):
        validate(raw)
    monkeypatch.setattr(evidence, "MAX_BYTES", 100)
    monkeypatch.setattr(evidence, "MAX_FILES", 0)
    with pytest.raises(ValueError, match="quota"):
        export(tmp_path)
    with pytest.raises(ValueError, match="manifest"):
        validate(raw)
    monkeypatch.setattr(evidence, "MAX_FILES", 10)
    monkeypatch.setattr(evidence, "MAX_ENCODED_BYTES", 1)
    with pytest.raises(ValueError, match="encoded quota"):
        export(tmp_path)
    with pytest.raises(ValueError, match="encoded quota"):
        validate(raw)


def test_export_detects_mutation_before_any_manifest_is_returned(tmp_path, monkeypatch):
    path = tmp_path / "a"
    path.write_bytes(b"before")
    original = os.open

    def changed(name, flags, *args, **kwargs):
        if name == path:
            path.write_bytes(b"changed-longer")
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", changed)
    with pytest.raises(ValueError, match="changed during export"):
        export(tmp_path)


def test_export_rejects_hardlinks_and_special_metadata(tmp_path, monkeypatch):
    path = tmp_path / "a"
    path.write_bytes(b"data")
    original = Path.lstat

    def linked(self):
        value = original(self)
        if self == path:
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0, st_nlink=2)
        return value

    monkeypatch.setattr(Path, "lstat", linked)
    with pytest.raises(ValueError, match="shared file"):
        export(tmp_path)


@pytest.mark.parametrize("failure", [None, "parent", "leaf"])
def test_posix_directory_descriptors_are_anchored_and_closed_even_on_failure(
    tmp_path, monkeypatch, failure,
):
    calls, closed = [], []

    def opened(name, flags, *, dir_fd=None):
        calls.append((name, flags, dir_fd))
        if (failure == "parent" and name == "workers"
                or failure == "leaf" and name == "receipt.json"):
            raise OSError("untrusted link refused")
        return len(calls) + 10

    monkeypatch.setattr(evidence, "os", SimpleNamespace(name="posix", O_RDONLY=1,
        O_DIRECTORY=2, O_NOFOLLOW=4, open=opened, close=closed.append))
    if failure:
        with pytest.raises(OSError, match="link refused"):
            evidence._open_owned(tmp_path, "workers/epoch/receipt.json")
    else:
        assert evidence._open_owned(tmp_path, "workers/epoch/receipt.json") == 14
        assert calls == [(tmp_path, 7, None), ("workers", 7, 11), ("epoch", 7, 12),
                         ("receipt.json", 5, 13)]
    assert closed == ([11] if failure == "parent" else [11, 12, 13])


def test_missing_non_directory_and_excessive_directory_evidence_are_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="root is missing"):
        export(tmp_path / "missing")
    file = tmp_path / "file"
    file.write_bytes(b"not a directory")
    with pytest.raises(ValueError, match="root is missing"):
        export(file)
    for name in ("one", "two", "three"):
        (tmp_path / name).mkdir()
    file.unlink()
    monkeypatch.setattr(evidence, "MAX_FILES", 1)
    with pytest.raises(ValueError, match="directory quota"):
        export(tmp_path)
