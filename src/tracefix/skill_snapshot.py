"""Validated, immutable snapshots for custom Markdown skill bundles."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from tracefix.exceptions import RunConfigurationError
from tracefix.tools.skills import SkillActivationTool, SkillLimits

MAX_SKILL_BUNDLE_FILES = 1_024
MAX_SKILL_BUNDLE_BYTES = 8 * 1024 * 1024
EMPTY_SKILL_BUNDLE_SHA256 = hashlib.sha256(b"{}").hexdigest()
EMPTY_SKILL_ARCHIVE_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class SkillBundleSnapshot:
    files: Mapping[str, bytes]
    sha256: str
    catalog_sha256: str


_SNAPSHOT_MANIFEST = ".tracefix-skill-manifest.json"
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _safe_skill_path(name: str) -> bool:
    path = Path(name)
    parts = path.parts
    return (
        not path.is_absolute()
        and "\\" not in name
        and all(part not in {"", ".", ".."} for part in parts)
        and bool(parts)
        and bool(_SKILL_NAME.fullmatch(parts[0]))
        and (
            len(parts) == 2 and parts[1] == "SKILL.md"
            or len(parts) >= 3 and parts[1] == "references"
            and Path(name).suffix.casefold() == ".md"
        )
    )


def _is_link_or_reparse(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def snapshot_custom_skills(
    root: Path, *, limits: SkillLimits | None = None
) -> SkillBundleSnapshot:
    """Validate the complete custom root, then freeze only skill and Markdown text."""
    limits = limits or SkillLimits()
    if _is_link_or_reparse(root):
        raise RunConfigurationError("custom Skills root cannot be a link or junction")
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise RunConfigurationError("custom Skills root must be a directory")
    try:
        activator = SkillActivationTool(resolved_root, limits=limits)
    except Exception as exc:
        raise RunConfigurationError("custom Skills catalog is invalid") from exc
    allowed_skills = set(activator.catalog)
    root_entries = tuple(resolved_root.iterdir())
    if {entry.name for entry in root_entries} != allowed_skills:
        raise RunConfigurationError("custom Skills root contains entries outside its catalog")
    if any(
        _is_link_or_reparse(entry) or not entry.is_dir()
        for entry in root_entries
    ):
        raise RunConfigurationError("custom Skills root may contain only regular skill directories")
    files: dict[str, bytes] = {}
    total = 0

    def walk(directory: Path, skill_name: str) -> None:
        nonlocal total
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            path = Path(entry.path)
            try:
                linked = _is_link_or_reparse(path)
            except OSError as exc:
                raise RunConfigurationError("custom Skills tree changed during validation") from exc
            if linked:
                raise RunConfigurationError("custom Skills cannot contain links or junctions")
            relative = path.relative_to(resolved_root).as_posix()
            parts = Path(relative).parts
            if entry.is_dir(follow_symlinks=False):
                if parts == (skill_name, "references") or (
                    len(parts) > 2 and parts[0] == skill_name and parts[1] == "references"
                ):
                    walk(path, skill_name)
                    continue
                if len(parts) == 1 and parts[0] in allowed_skills:
                    walk(path, skill_name)
                    continue
                raise RunConfigurationError("custom Skills contain an unsupported directory")
            if not entry.is_file(follow_symlinks=False):
                raise RunConfigurationError("custom Skills contain a special file")
            allowed_body = parts == (skill_name, "SKILL.md")
            allowed_reference = (
                len(parts) >= 3
                and parts[0] == skill_name
                and parts[1] == "references"
                and path.suffix.casefold() == ".md"
            )
            if not (allowed_body or allowed_reference):
                raise RunConfigurationError(
                    "custom Skills allow only SKILL.md and Markdown references"
                )
            maximum = limits.max_skill_bytes + 8192 if allowed_body else limits.max_reference_bytes
            info = path.lstat()
            if info.st_size > maximum:
                raise RunConfigurationError("custom Skill text exceeds its configured byte limit")
            try:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if (opened.st_ino, opened.st_dev, opened.st_size) != (
                        info.st_ino, info.st_dev, info.st_size
                    ):
                        raise RunConfigurationError("custom Skill changed during validation")
                    content = stream.read(maximum + 1)
                    after = path.lstat()
                    if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) != (
                        info.st_ino, info.st_dev, info.st_size, info.st_mtime_ns
                    ):
                        raise RunConfigurationError("custom Skill changed during validation")
                content.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise RunConfigurationError("custom Skill text must be readable UTF-8") from exc
            if len(content) > maximum:
                raise RunConfigurationError("custom Skill text changed during validation")
            files[relative] = content
            total += len(content)
            if total > MAX_SKILL_BUNDLE_BYTES or len(files) > MAX_SKILL_BUNDLE_FILES:
                raise RunConfigurationError("custom Skills snapshot exceeds its bundle limit")

    for skill_name in sorted(allowed_skills):
        directory = resolved_root / skill_name
        if _is_link_or_reparse(directory):
            raise RunConfigurationError("custom Skill directory cannot be a link or junction")
        walk(directory, skill_name)
    canonical = json.dumps(
        {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    catalog_payload = [item.model_dump(mode="json") for item in activator.skill_catalog]
    return SkillBundleSnapshot(
        files=MappingProxyType(files),
        sha256=hashlib.sha256(canonical).hexdigest(),
        catalog_sha256=hashlib.sha256(
            json.dumps(catalog_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    )


def write_skill_snapshot(snapshot: SkillBundleSnapshot, destination: Path) -> str:
    """Persist the immutable bundle using an atomic, bounded archive replace."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    manifest = {
        "schema_version": 1,
        "sha256": snapshot.sha256,
        "catalog_sha256": snapshot.catalog_sha256,
        "files": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in sorted(snapshot.files.items())
        },
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with tarfile.open(temporary, "w:", format=tarfile.PAX_FORMAT) as archive:
        for name, data in sorted(snapshot.files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o444, 0
            archive.addfile(info, io.BytesIO(data))
        info = tarfile.TarInfo(_SNAPSHOT_MANIFEST)
        info.size, info.mode, info.mtime = len(encoded), 0o444, 0
        archive.addfile(info, io.BytesIO(encoded))
    if temporary.stat().st_size > MAX_SKILL_BUNDLE_BYTES + 2 * 1024 * 1024:
        temporary.unlink(missing_ok=True)
        raise RunConfigurationError("custom Skills snapshot archive exceeds its storage limit")
    os.replace(temporary, destination)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def read_skill_snapshot(
    path: Path, *, expected_archive_sha256: str, expected_bundle_sha256: str
) -> SkillBundleSnapshot:
    """Load a saved snapshot without consulting the live Skills directory."""
    try:
        if _is_link_or_reparse(path) or path.stat().st_size > (
            MAX_SKILL_BUNDLE_BYTES + 2 * 1024 * 1024
        ):
            raise ValueError("saved Skills archive is not a bounded regular file")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_archive_sha256:
            raise ValueError("saved Skills archive identity changed")
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            members = archive.getmembers()
            if len(members) > MAX_SKILL_BUNDLE_FILES + 1:
                raise ValueError("saved Skills archive has too many files")
            names = [member.name for member in members]
            if len(names) != len(set(names)) or names.count(_SNAPSHOT_MANIFEST) != 1:
                raise ValueError("saved Skills archive membership is invalid")
            files: dict[str, bytes] = {}
            for member in members:
                if member.name == _SNAPSHOT_MANIFEST:
                    continue
                if not member.isfile() or not _safe_skill_path(member.name):
                    raise ValueError("saved Skills archive contains an unsupported entry")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("saved Skills archive content is missing")
                files[member.name] = stream.read(MAX_SKILL_BUNDLE_BYTES + 1)
            manifest_member = next(
                member for member in members if member.name == _SNAPSHOT_MANIFEST
            )
            manifest_stream = archive.extractfile(manifest_member)
            if manifest_stream is None:
                raise ValueError("saved Skills manifest is missing")
            manifest = json.loads(manifest_stream.read().decode("utf-8"))
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
        bundle_identity = hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        total = sum(len(data) for data in files.values())
        if (
            total > MAX_SKILL_BUNDLE_BYTES
            or manifest.get("schema_version") != 1
            or manifest.get("files") != hashes
            or manifest.get("sha256") != bundle_identity
            or bundle_identity != expected_bundle_sha256
            or not isinstance(manifest.get("catalog_sha256"), str)
            or len(manifest["catalog_sha256"]) != 64
        ):
            raise ValueError("saved Skills snapshot content identity changed")
        return SkillBundleSnapshot(
            MappingProxyType(files), bundle_identity, manifest["catalog_sha256"]
        )
    except (
        OSError, ValueError, tarfile.TarError, UnicodeError, json.JSONDecodeError,
        KeyError, TypeError,
    ) as exc:
        raise RunConfigurationError("saved Skills snapshot is invalid") from exc


def materialize_skill_snapshot(snapshot: SkillBundleSnapshot, destination: Path) -> None:
    """Write the already validated immutable text into an empty private directory."""
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise RunConfigurationError("Skills snapshot destination must be an empty directory")
    for name, content in sorted(snapshot.files.items()):
        if not _safe_skill_path(name):
            raise RunConfigurationError("Skills snapshot contains an unsafe relative path")
        target = destination / Path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
        target.chmod(0o444)
