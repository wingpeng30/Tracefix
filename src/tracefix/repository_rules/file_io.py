"""Bounded, no-follow file reads inside one approved checkout."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.paths import relative_path


def _linklike(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _directory_stamp(info) -> tuple:
    if info.st_ino <= 0:
        raise RuleError("rules_source_identity_unavailable")
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), _linklike(info)


def _file_stamp(info) -> tuple:
    if info.st_ino <= 0:
        raise RuleError("rules_source_identity_unavailable")
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _same_open_file(path_info, opened_info, *, windows: bool) -> bool:
    path_stamp, opened_stamp = _file_stamp(path_info), _file_stamp(opened_info)
    # Some Windows Python builds report creation time via lstat's ctime,
    # but change time via fstat's ctime. Compare that field only within the
    # same API; the final FD check still detects changes with restored mtime.
    if not windows:
        return path_stamp == opened_stamp
    return (path_stamp[:-1] == opened_stamp[:-1]
            and getattr(path_info, "st_birthtime_ns", None)
            == getattr(opened_info, "st_birthtime_ns", None))


class BoundedWorkspaceReader:
    """Low-level file access; callers choose a fixed cap and permitted source domain."""

    def __init__(self, root: Path, *, protected_dirs: tuple[str, ...] = ()) -> None:
        try:
            original = root.expanduser().lstat()
            if _linklike(original) or not stat.S_ISDIR(original.st_mode):
                raise RuleError("rules_source_root_not_regular")
            self.root = root.expanduser().resolve(strict=True)
            self._root_stamp = _directory_stamp(self.root.lstat())
            if self._root_stamp != _directory_stamp(original):
                raise RuleError("rules_source_root_changed")
        except OSError as exc:
            raise RuleError("rules_source_root_unavailable") from exc
        prefixes = []
        for name in protected_dirs:
            normalized = name.replace("\\", "/")
            path = PurePosixPath(normalized)
            if (not normalized or path.is_absolute() or ":" in normalized
                    or str(path) != normalized or any(part in {".", ".."} for part in path.parts)):
                raise RuleError("rules_invalid_protected_scope")
            prefixes.append(tuple(part.casefold() for part in path.parts))
        self._protected = tuple(prefixes)

    def validate_target(self, target: str) -> str:
        target = relative_path(target)
        parts = tuple(part.casefold() for part in target.split("/"))
        if any(parts[:len(prefix)] == prefix for prefix in self._protected):
            raise RuleError("rules_target_is_internal")
        return target

    def _inspect(self, relative: str, maximum: int):
        root_info = self.root.lstat()
        if _directory_stamp(root_info) != self._root_stamp:
            raise RuleError("rules_source_root_changed")
        parents = [(self.root, root_info)]
        current = self.root
        parts = relative.split("/")
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                return None
            if _linklike(info):
                raise RuleError("rules_source_link_or_reparse")
            if index != len(parts) - 1:
                if not stat.S_ISDIR(info.st_mode):
                    raise RuleError("rules_source_parent_not_directory")
                _directory_stamp(info)
                parents.append((current, info))
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuleError("rules_source_not_regular")
            _file_stamp(info)
            if info.st_size > maximum:
                raise RuleError("rules_source_limit_exceeded")
            return current, info, parents
        raise RuleError("rules_invalid_relative_path")

    def read_bytes(self, target: str, *, maximum: int) -> bytes | None:
        if type(maximum) is not int or not 1 <= maximum <= 1024 * 1024:
            raise RuleError("rules_source_read_limit_invalid")
        relative = self.validate_target(relative_path(target))
        try:
            observed = self._inspect(relative, maximum)
            if observed is None:
                return None
            path, before, parents = observed
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (not _same_open_file(before, opened, windows=os.name == "nt")
                        or not stat.S_ISREG(opened.st_mode) or _linklike(opened)):
                    raise RuleError("rules_source_changed_during_open")
                content = stream.read(maximum + 1)
                if len(content) > maximum:
                    raise RuleError("rules_source_limit_exceeded")
                if (_file_stamp(os.fstat(stream.fileno())) != _file_stamp(opened)
                        or _file_stamp(path.lstat()) != _file_stamp(before)
                        or _linklike(path.lstat())):
                    raise RuleError("rules_source_changed_during_read")
                for parent, original in parents:
                    if _directory_stamp(parent.lstat()) != _directory_stamp(original):
                        raise RuleError("rules_source_parent_changed")
            return content
        except OSError as exc:
            raise RuleError("rules_source_io_or_identity_failure") from exc
