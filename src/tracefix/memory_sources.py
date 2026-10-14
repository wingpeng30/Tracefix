"""Local adapters for repository identity and bounded source snapshots."""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PureWindowsPath

from tracefix.memory_domain import RepositoryIdentity, SourceSnapshot

_DEPENDENCY_FILES = (
    "pyproject.toml", "requirements.txt", "requirements.lock", "uv.lock",
    "poetry.lock", "Pipfile.lock", "environment.yml", "conda-lock.yml",
)
_MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(value: str) -> bool:
    path = Path(value)
    windows = PureWindowsPath(value)
    return bool(value.strip()) and not any(ord(char) < 32 for char in value) and not (
        path.is_absolute() or windows.drive or windows.root or ".." in path.parts
        or ".." in windows.parts or ".git" in path.parts or ".git" in windows.parts
    )


class GitRepositoryIdentityResolver:
    """Use Git's common directory for logical identity and checkout path for locality."""

    def resolve(self, repository: object) -> RepositoryIdentity:
        root = Path(repository).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("repository identity requires a directory")
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"], cwd=root,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5, check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                raise ValueError("not a Git worktree")
            common = Path(result.stdout.strip())
            if not common.is_absolute():
                common = root / common
            common = common.resolve(strict=True)
            git_backed = True
        except (OSError, subprocess.SubprocessError, ValueError):
            # Non-Git fixtures remain isolated by their canonical directory. Remote URLs are
            # intentionally never consulted because they do not prove shared source identity.
            common = root
            git_backed = False
        normalize = lambda path: os.path.normcase(str(path)).casefold()
        return RepositoryIdentity(
            repository_id=_sha256(normalize(common).encode("utf-8")),
            checkout_id=_sha256(normalize(root).encode("utf-8")),
            git_backed=git_backed,
        )


class LocalSourceSnapshotProvider:
    """Hash only named regular files and a small explicit environment fingerprint."""

    def __init__(self, resolver: GitRepositoryIdentityResolver | None = None) -> None:
        self.resolver = resolver or GitRepositoryIdentityResolver()

    @staticmethod
    def _read_hash(root: Path, relative: str) -> str | None:
        if not _safe_relative(relative):
            raise ValueError(f"unsafe source snapshot path: {relative}")
        candidate = root / relative
        current = root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"source snapshot path contains a symbolic link: {relative}")
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            return None
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        if resolved.stat().st_size > _MAX_SOURCE_FILE_BYTES:
            raise ValueError(f"source snapshot file exceeds size limit: {relative}")
        return _sha256(resolved.read_bytes())

    def snapshot(
        self,
        repository: object,
        paths: Sequence[str],
        *,
        content_root: object | None = None,
        patch_sha256: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> SourceSnapshot:
        root = Path(repository).expanduser().resolve(strict=True)
        content = Path(content_root).expanduser().resolve(strict=True) if content_root else root
        if not content.is_dir():
            raise ValueError("source snapshot content root must be a directory")
        identity = self.resolver.resolve(root)
        files = {path: self._read_hash(content, path) for path in sorted(set(paths))}
        dependency_hashes = {
            name: self._read_hash(content, name) for name in _DEPENDENCY_FILES
            if (content / name).exists()
        }
        dependency_sha256 = (
            _sha256(repr(sorted(dependency_hashes.items())).encode("utf-8"))
            if dependency_hashes else None
        )
        environment_values = {
            "implementation": sys.implementation.name,
            "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
            "platform": platform.system().casefold(),
            "machine": platform.machine().casefold(),
            **dict(environment or {}),
        }
        environment_sha256 = _sha256(repr(sorted(environment_values.items())).encode("utf-8"))
        return SourceSnapshot(
            identity=identity,
            files=files,
            dependency_sha256=dependency_sha256,
            environment_sha256=environment_sha256,
            patch_sha256=patch_sha256,
        )
