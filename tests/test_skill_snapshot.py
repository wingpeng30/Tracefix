"""Custom Docker Skills are validated once and recovered from a pinned archive."""

import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import MappingProxyType

import pytest

from tracefix.exceptions import RunConfigurationError
from tracefix.skill_snapshot import (
    EMPTY_SKILL_BUNDLE_SHA256,
    SkillBundleSnapshot,
    SkillLimits,
    _safe_skill_path,
    materialize_skill_snapshot,
    read_skill_snapshot,
    snapshot_custom_skills,
    write_skill_snapshot,
)


def _skill(root, *, reference=True):
    directory = root / "tracefix-debugging"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: tracefix-debugging\ndescription: Diagnose TraceFix\n"
        "metadata:\n  version: 1.0.0\n---\n\nRead the current source first.\n",
        encoding="utf-8",
    )
    if reference:
        refs = directory / "references"
        refs.mkdir()
        (refs / "workflow.md").write_text("Use focused tests.\n", encoding="utf-8")
    return directory


def test_snapshot_keeps_only_skill_and_markdown_reference_text(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    directory = _skill(root)
    (directory / "notes.txt").write_text("not allowed", encoding="utf-8")

    with pytest.raises(RunConfigurationError, match="only SKILL.md"):
        snapshot_custom_skills(root)


def test_snapshot_archive_round_trip_is_independent_of_live_root(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    directory = _skill(root)
    snapshot = snapshot_custom_skills(root)
    archive = tmp_path / "skills-snapshot.tar"

    archive_sha = write_skill_snapshot(snapshot, archive)
    (directory / "SKILL.md").write_text("changed", encoding="utf-8")
    restored = read_skill_snapshot(
        archive,
        expected_archive_sha256=archive_sha,
        expected_bundle_sha256=snapshot.sha256,
    )
    materialized = tmp_path / "materialized"
    materialized.mkdir()
    materialize_skill_snapshot(restored, materialized)

    assert restored == snapshot
    assert (materialized / "tracefix-debugging" / "SKILL.md").read_bytes() == snapshot.files[
        "tracefix-debugging/SKILL.md"
    ]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_sha


def test_skill_snapshot_file_mapping_is_immutable(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _skill(root, reference=False)
    snapshot = snapshot_custom_skills(root)

    with pytest.raises(TypeError):
        snapshot.files["tracefix-debugging/SKILL.md"] = b"modified"


def test_archive_content_and_identity_tampering_are_rejected(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _skill(root)
    snapshot = snapshot_custom_skills(root)
    archive = tmp_path / "skills-snapshot.tar"
    archive_sha = write_skill_snapshot(snapshot, archive)
    raw = bytearray(archive.read_bytes())
    raw[-1024] ^= 0x01
    archive.write_bytes(raw)

    with pytest.raises(RunConfigurationError, match="saved Skills snapshot is invalid"):
        read_skill_snapshot(
            archive,
            expected_archive_sha256=archive_sha,
            expected_bundle_sha256=snapshot.sha256,
        )


def test_root_must_not_include_unregistered_entries(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _skill(root)
    (root / "README.md").write_text("unregistered", encoding="utf-8")

    with pytest.raises(RunConfigurationError, match="outside its catalog"):
        snapshot_custom_skills(root)


def test_references_are_bounded_markdown_only(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    directory = _skill(root, reference=False)
    refs = directory / "references"
    refs.mkdir()
    (refs / "secret.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RunConfigurationError, match="only SKILL.md"):
        snapshot_custom_skills(root)


def test_empty_skill_root_has_a_stable_empty_snapshot(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()

    snapshot = snapshot_custom_skills(root)

    assert snapshot.files == {}
    assert snapshot.sha256 == EMPTY_SKILL_BUNDLE_SHA256


def test_skill_catalog_rejects_non_utf8_and_oversized_instruction_text(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    directory = _skill(root, reference=False)
    (directory / "SKILL.md").write_bytes(b"\xff")
    with pytest.raises(RunConfigurationError, match="catalog is invalid"):
        snapshot_custom_skills(root)

    (directory / "SKILL.md").write_text(
        "---\nname: tracefix-debugging\ndescription: Diagnose TraceFix\n"
        "---\n\nInstructions are intentionally long.\n",
        encoding="utf-8",
    )
    with pytest.raises(RunConfigurationError, match="catalog is invalid"):
        snapshot_custom_skills(root, limits=SkillLimits(max_skill_bytes=4))


def test_skill_root_directory_shape_and_reference_limits_are_enforced(tmp_path, monkeypatch):
    import tracefix.skill_snapshot as skill_snapshot

    file_root = tmp_path / "not-a-directory"
    file_root.write_text("no", encoding="utf-8")
    with pytest.raises(RunConfigurationError, match="must be a directory"):
        snapshot_custom_skills(file_root)

    root = tmp_path / "skills"
    root.mkdir()
    directory = _skill(root, reference=False)
    (directory / "scripts").mkdir()
    with pytest.raises(RunConfigurationError, match="unsupported directory"):
        snapshot_custom_skills(root)
    (directory / "scripts").rmdir()
    refs = directory / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("long reference", encoding="utf-8")
    with pytest.raises(RunConfigurationError, match="configured byte limit"):
        snapshot_custom_skills(root, limits=SkillLimits(max_reference_bytes=2))

    monkeypatch.setattr(skill_snapshot, "_is_link_or_reparse", lambda _path: True)
    with pytest.raises(RunConfigurationError, match="root cannot be a link"):
        snapshot_custom_skills(root)


@pytest.mark.parametrize(
    "fault, message",
    [
        ("linked-directory", "regular skill directories"),
        ("tree-changed", "tree changed during validation"),
        ("linked-file", "cannot contain links"),
        ("opened-file-changed", "changed during validation"),
    ],
)
def test_skill_snapshot_rejects_links_and_files_changed_during_open(
    tmp_path, monkeypatch, fault, message
):
    import types

    import tracefix.skill_snapshot as skill_snapshot

    root = tmp_path / "skills"
    root.mkdir()
    directory = _skill(root, reference=False)
    skill_file = directory / "SKILL.md"
    original_check = skill_snapshot._is_link_or_reparse
    if fault == "linked-directory":
        monkeypatch.setattr(
            skill_snapshot,
            "_is_link_or_reparse",
            lambda path: path == directory or original_check(path),
        )
    elif fault == "tree-changed":
        monkeypatch.setattr(
            skill_snapshot,
            "_is_link_or_reparse",
            lambda path: (_ for _ in ()).throw(OSError("disappeared"))
            if path == skill_file
            else original_check(path),
        )
    elif fault == "linked-file":
        monkeypatch.setattr(
            skill_snapshot,
            "_is_link_or_reparse",
            lambda path: path == skill_file or original_check(path),
        )
    else:
        real_fstat = skill_snapshot.os.fstat

        def changed_file(descriptor):
            info = real_fstat(descriptor)
            return types.SimpleNamespace(
                st_ino=info.st_ino + 1,
                st_dev=info.st_dev,
                st_size=info.st_size,
            )

        monkeypatch.setattr(skill_snapshot.os, "fstat", changed_file)

    with pytest.raises(RunConfigurationError, match=message):
        snapshot_custom_skills(root)


def test_skill_reference_growth_is_detected_after_open(tmp_path, monkeypatch):
    import types

    import tracefix.skill_snapshot as skill_snapshot

    root = tmp_path / "skills"
    root.mkdir()
    directory = _skill(root, reference=False)
    references = directory / "references"
    references.mkdir()
    reference = references / "guide.md"
    reference.write_text("grew", encoding="utf-8")
    original_lstat = Path.lstat
    original_fstat = skill_snapshot.os.fstat

    def stable_small_file(path, *args, **kwargs):
        info = original_lstat(path, *args, **kwargs)
        if path == reference:
            return types.SimpleNamespace(
                st_ino=info.st_ino, st_dev=info.st_dev, st_size=1,
                st_mtime_ns=info.st_mtime_ns, st_mode=info.st_mode,
                st_file_attributes=getattr(info, "st_file_attributes", 0),
            )
        return info

    def opened_small_file(descriptor):
        info = original_fstat(descriptor)
        if info.st_size == 4:
            return types.SimpleNamespace(
                st_ino=info.st_ino, st_dev=info.st_dev, st_size=1
            )
        return info

    monkeypatch.setattr(Path, "lstat", stable_small_file)
    monkeypatch.setattr(skill_snapshot.os, "fstat", opened_small_file)

    with pytest.raises(RunConfigurationError, match="text changed during validation"):
        snapshot_custom_skills(root, limits=SkillLimits(max_reference_bytes=1))


def test_skill_snapshot_rejects_post_read_change_bundle_overflow_and_bad_utf8(
    tmp_path, monkeypatch
):
    import types

    import tracefix.skill_snapshot as skill_snapshot

    root = tmp_path / "skills"
    root.mkdir()
    directory = _skill(root, reference=False)
    skill_file = directory / "SKILL.md"
    original_lstat = Path.lstat
    calls = 0

    def changed_mtime_after_read(path, *args, **kwargs):
        nonlocal calls
        info = original_lstat(path, *args, **kwargs)
        if path == skill_file:
            calls += 1
            if calls == 3:
                return types.SimpleNamespace(
                    st_ino=info.st_ino,
                    st_dev=info.st_dev,
                    st_size=info.st_size,
                    st_mtime_ns=info.st_mtime_ns + 1,
                    st_mode=info.st_mode,
                    st_file_attributes=getattr(info, "st_file_attributes", 0),
                )
        return info

    monkeypatch.setattr(Path, "lstat", changed_mtime_after_read)
    with pytest.raises(RunConfigurationError, match="changed during validation"):
        snapshot_custom_skills(root)

    monkeypatch.setattr(Path, "lstat", original_lstat)
    monkeypatch.setattr(skill_snapshot, "MAX_SKILL_BUNDLE_BYTES", 1)
    with pytest.raises(RunConfigurationError, match="bundle limit"):
        snapshot_custom_skills(root)

    monkeypatch.setattr(skill_snapshot, "MAX_SKILL_BUNDLE_BYTES", 8 * 1024 * 1024)
    references = directory / "references"
    references.mkdir()
    (references / "invalid.md").write_bytes(b"\xff")
    with pytest.raises(RunConfigurationError, match="readable UTF-8"):
        snapshot_custom_skills(root)


def test_skill_directory_change_between_catalog_and_copy_fails_closed(tmp_path, monkeypatch):
    import tracefix.skill_snapshot as skill_snapshot

    root = tmp_path / "skills"
    root.mkdir()
    directory = _skill(root, reference=False)
    original_check = skill_snapshot._is_link_or_reparse
    counts = 0

    def become_link(path):
        nonlocal counts
        if path == directory:
            counts += 1
            return counts >= 2
        return original_check(path)

    monkeypatch.setattr(skill_snapshot, "_is_link_or_reparse", become_link)
    with pytest.raises(RunConfigurationError, match="Skill directory cannot be a link"):
        snapshot_custom_skills(root)


@pytest.mark.parametrize("missing", ["skill", "manifest"])
def test_saved_skill_snapshot_rejects_missing_archive_streams(tmp_path, monkeypatch, missing):
    root = tmp_path / "skills"
    root.mkdir()
    _skill(root, reference=False)
    snapshot = snapshot_custom_skills(root)
    archive = tmp_path / "skills.tar"
    archive_sha = write_skill_snapshot(snapshot, archive)
    original_extract = tarfile.TarFile.extractfile

    def unavailable(tar, member):
        name = member.name if isinstance(member, tarfile.TarInfo) else member
        if (missing == "skill" and name == "tracefix-debugging/SKILL.md") or (
            missing == "manifest" and name == ".tracefix-skill-manifest.json"
        ):
            return None
        return original_extract(tar, member)

    monkeypatch.setattr(tarfile.TarFile, "extractfile", unavailable)
    with pytest.raises(RunConfigurationError, match="saved Skills snapshot is invalid"):
        read_skill_snapshot(
            archive,
            expected_archive_sha256=archive_sha,
            expected_bundle_sha256=snapshot.sha256,
        )


@pytest.mark.parametrize(
    "name",
    [
        "../escape/SKILL.md",
        "/tracefix-debugging/SKILL.md",
        "tracefix-debugging\\SKILL.md",
        "Tracefix-debugging/SKILL.md",
        "tracefix-debugging/references/internal.txt",
        "tracefix-debugging/other/SKILL.md",
        "tracefix-debugging/SKILL.md/extra",
    ],
)
def test_saved_skill_paths_must_stay_in_the_markdown_snapshot_layout(name):
    assert not _safe_skill_path(name)


@pytest.mark.parametrize("kind", ["no-manifest", "duplicate-manifest", "unsafe-path", "link"])
def test_saved_skill_archives_reject_invalid_members(tmp_path, kind):
    from tracefix.skill_snapshot import read_skill_snapshot

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:") as tar:
        if kind != "no-manifest":
            payload = json.dumps({"schema_version": 1, "files": {}}).encode()
            info = tarfile.TarInfo(".tracefix-skill-manifest.json")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        if kind == "duplicate-manifest":
            payload = b"{}"
            info = tarfile.TarInfo(".tracefix-skill-manifest.json")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        elif kind == "unsafe-path":
            payload = b"text"
            info = tarfile.TarInfo("tracefix-debugging/../escape.md")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        elif kind == "link":
            info = tarfile.TarInfo("tracefix-debugging/SKILL.md")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../outside"
            tar.addfile(info)
    path = tmp_path / f"{kind}.tar"
    path.write_bytes(archive.getvalue())

    with pytest.raises(RunConfigurationError, match="saved Skills snapshot is invalid"):
        read_skill_snapshot(
            path,
            expected_archive_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            expected_bundle_sha256="0" * 64,
        )


def test_saved_skill_archive_enforces_file_count_and_manifest_identity(tmp_path, monkeypatch):
    import tracefix.skill_snapshot as skill_snapshot

    root = tmp_path / "skills"
    root.mkdir()
    _skill(root)
    snapshot = snapshot_custom_skills(root)
    archive_path = tmp_path / "skills.tar"
    archive_sha = write_skill_snapshot(snapshot, archive_path)
    monkeypatch.setattr(skill_snapshot, "MAX_SKILL_BUNDLE_FILES", 1)
    with pytest.raises(RunConfigurationError, match="saved Skills snapshot is invalid"):
        read_skill_snapshot(
            archive_path,
            expected_archive_sha256=archive_sha,
            expected_bundle_sha256=snapshot.sha256,
        )

    monkeypatch.setattr(skill_snapshot, "MAX_SKILL_BUNDLE_FILES", 1_024)
    rewritten = io.BytesIO()
    with tarfile.open(archive_path, "r:") as source, tarfile.open(
        fileobj=rewritten, mode="w:"
    ) as target:
        for member in source.getmembers():
            payload = source.extractfile(member).read()
            info = tarfile.TarInfo(member.name)
            info.size, info.mode, info.mtime = len(payload), member.mode, member.mtime
            if member.name == ".tracefix-skill-manifest.json":
                manifest = json.loads(payload)
                manifest["catalog_sha256"] = "invalid"
                payload = json.dumps(manifest).encode()
                info.size = len(payload)
            target.addfile(info, io.BytesIO(payload))
    archive_path.write_bytes(rewritten.getvalue())
    with pytest.raises(RunConfigurationError, match="saved Skills snapshot is invalid"):
        read_skill_snapshot(
            archive_path,
            expected_archive_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            expected_bundle_sha256=snapshot.sha256,
        )


def test_saved_skill_archive_rejects_link_and_cleans_oversized_temp(tmp_path, monkeypatch):
    import types

    import tracefix.skill_snapshot as skill_snapshot

    root = tmp_path / "skills"
    root.mkdir()
    _skill(root, reference=False)
    snapshot = snapshot_custom_skills(root)
    archive = tmp_path / "skills.tar"
    archive_sha = write_skill_snapshot(snapshot, archive)
    monkeypatch.setattr(
        skill_snapshot,
        "_is_link_or_reparse",
        lambda path: path == archive,
    )
    with pytest.raises(RunConfigurationError, match="saved Skills snapshot is invalid"):
        read_skill_snapshot(
            archive,
            expected_archive_sha256=archive_sha,
            expected_bundle_sha256=snapshot.sha256,
        )

    monkeypatch.undo()
    destination = tmp_path / "too-large.tar"
    temporary = destination.with_name(destination.name + ".tmp")
    original_stat = Path.stat

    def oversized_temp(path, *args, **kwargs):
        if path == temporary:
            return types.SimpleNamespace(
                st_size=skill_snapshot.MAX_SKILL_BUNDLE_BYTES + 2 * 1024 * 1024 + 1
            )
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", oversized_temp)
    with pytest.raises(RunConfigurationError, match="archive exceeds its storage limit"):
        write_skill_snapshot(snapshot, destination)
    monkeypatch.setattr(Path, "stat", original_stat)
    assert not destination.exists()
    assert not temporary.exists()


def test_materialization_rejects_nonempty_destination_and_unsafe_snapshot_path(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _skill(root, reference=False)
    snapshot = snapshot_custom_skills(root)
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "occupied").write_text("keep", encoding="utf-8")

    with pytest.raises(RunConfigurationError, match="empty directory"):
        materialize_skill_snapshot(snapshot, destination)

    unsafe = SkillBundleSnapshot(
        MappingProxyType({"../escape.md": b"no"}), "0" * 64, "0" * 64
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RunConfigurationError, match="unsafe relative path"):
        materialize_skill_snapshot(unsafe, empty)

    with pytest.raises(RunConfigurationError, match="empty directory"):
        materialize_skill_snapshot(snapshot, tmp_path / "missing")
