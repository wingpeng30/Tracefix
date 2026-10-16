"""Local metadata-only access resolution; never opens source file contents."""

from __future__ import annotations

import stat
from collections.abc import Callable, Sequence
from pathlib import Path

from tracefix.repository_rules.access import ToolAccessPlan
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.paths import matches_glob, relative_path

_IGNORED = frozenset({".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".venv",
                      "__pycache__", "build", "dist", "node_modules", "venv"})


def _identity(info) -> tuple:
    if info.st_ino <= 0:
        raise RuleError("rules_metadata_identity_unavailable")
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise RuleError("rules_metadata_link_or_reparse")
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise RuleError("rules_metadata_special_file")
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise RuleError("rules_metadata_hardlink")
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


class WorkspaceAccessPaths:
    """Resolve possible access paths before source operations can begin.

    Diff planning conservatively includes all workspace files and trusted index
    paths (including deleted files). Its injected index reader must inspect Git
    metadata only, not obtain a diff or read workspace source bodies.
    """

    def __init__(self, root: Path, *, validate_target: Callable[[str], str] = relative_path,
                 index_paths: Callable[[], Sequence[str]] | None = None,
                 case_sensitive: bool = True, ignored_dirs: frozenset[str] = _IGNORED) -> None:
        self.root = root.absolute()
        self.validate_target, self.index_paths = validate_target, index_paths
        self.case_sensitive = case_sensitive
        if (not isinstance(ignored_dirs, frozenset)
                or any(not isinstance(name, str) or not name or "/" in name or "\\" in name
                       or name in {".", ".."} for name in ignored_dirs)):
            raise RuleError("rules_metadata_ignored_scope_invalid")
        self.ignored_dirs = frozenset(name.casefold() for name in ignored_dirs)
        if type(case_sensitive) is not bool:
            raise RuleError("rules_case_semantics_invalid")
        try:
            info = self.root.lstat()
            self.root_identity = _identity(info)
            if not stat.S_ISDIR(info.st_mode):
                raise RuleError("rules_metadata_root_not_directory")
        except OSError as exc:
            raise RuleError("rules_metadata_io_failure") from exc

    def _inspect(self, target: str, *, missing: bool = False):
        if _identity(self.root.lstat()) != self.root_identity:
            raise RuleError("rules_metadata_root_changed")
        if target == ".":
            return self.root, self.root.lstat()
        target = relative_path(self.validate_target(relative_path(target)))
        path = self.root
        parts = target.split("/")
        for index, part in enumerate(parts):
            path = path / part
            try:
                info = path.lstat()
            except FileNotFoundError:
                if missing:
                    return None
                raise RuleError("rules_metadata_target_missing") from None
            _identity(info)
            if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                raise RuleError("rules_metadata_parent_not_directory")
        return path, info

    def _files(self, target: str) -> tuple[str, ...]:
        path, info = self._inspect(target)
        if stat.S_ISREG(info.st_mode):
            return (target,)
        files, directories = [], [target]
        count = 0
        while directories:
            directory = directories.pop()
            parent, before = self._inspect(directory)
            count += 1
            if count > 5000:
                raise RuleError("rules_metadata_directory_limit")
            children = self._children(parent)
            for child in children:
                candidate = child.relative_to(self.root).as_posix()
                # Lexical exclusions precede even lstat; no skipped target body
                # or metadata is inspected through a link or protected domain.
                try:
                    self.validate_target(relative_path(candidate))
                except RuleError:
                    continue
                if child.name.casefold() in self.ignored_dirs:
                    continue
                _, child_info = self._inspect(candidate)
                if stat.S_ISDIR(child_info.st_mode):
                    directories.append(candidate)
                else:
                    files.append(candidate)
                    if len(files) > 5000:
                        raise RuleError("rules_access_path_limit")
            after = parent.lstat()
            if (_identity(before) != _identity(after) or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                    or tuple(child.name for child in children)
                    != tuple(child.name for child in self._children(parent))):
                raise RuleError("rules_metadata_changed_during_enumeration")
        return tuple(sorted(files))

    @staticmethod
    def _children(parent: Path) -> tuple[Path, ...]:
        entries = []
        for index, child in enumerate(parent.iterdir()):
            if index >= 10000:
                raise RuleError("rules_metadata_directory_entry_limit")
            entries.append(child)
        return tuple(sorted(entries, key=lambda item: item.name))

    def resolve(self, plan: ToolAccessPlan) -> tuple[str, ...]:
        try:
            paths = tuple(dict.fromkeys(self._resolve(plan)))
            if len(paths) > 5000:
                raise RuleError("rules_access_path_limit")
            return paths
        except OSError as exc:
            raise RuleError("rules_metadata_io_failure") from exc

    def _resolve(self, plan: ToolAccessPlan) -> tuple[str, ...]:
        if plan.enumeration == "changed_files":
            if self.index_paths is None:
                raise RuleError("rules_index_metadata_required")
            paths = list(self._files("."))
            for index, target in enumerate(self.index_paths()):
                if index >= 5000:
                    raise RuleError("rules_access_path_limit")
                target = relative_path(self.validate_target(relative_path(target)))
                observed = self._inspect(target, missing=True)
                if observed is not None and not stat.S_ISREG(observed[1].st_mode):
                    raise RuleError("rules_metadata_file_required")
                paths.append(target)
            return tuple(paths)
        if plan.enumeration == "search":
            root = plan.paths[0].path
            _, info = self._inspect(root)
            candidates = self._files(root)
            if stat.S_ISREG(info.st_mode):
                return candidates
            return tuple(path for path in candidates if matches_glob(
                plan.search_glob, path if root == "." else path[len(root) + 1:],
                case_sensitive=self.case_sensitive))
        if plan.enumeration == "test_targets":
            paths = []
            for access in plan.paths:
                if access.purpose == "config":
                    observed = self._inspect(access.path, missing=True)
                    if observed is not None and not stat.S_ISREG(observed[1].st_mode):
                        raise RuleError("rules_metadata_file_required")
                    paths.append(access.path)
                    continue
                paths.extend(self._files(access.path))
                parents = () if access.path == "." else access.path.split("/")[:-1]
                for depth in range(len(parents) + 1):
                    target = "/".join((*parents[:depth], "conftest.py"))
                    observed = self._inspect(target, missing=True)
                    if observed is not None:
                        if not stat.S_ISREG(observed[1].st_mode):
                            raise RuleError("rules_metadata_file_required")
                        paths.append(target)
            return tuple(paths)
        paths = []
        for access in plan.paths:
            observed = self._inspect(access.path, missing=access.purpose == "write")
            if observed is not None and not stat.S_ISREG(observed[1].st_mode):
                raise RuleError("rules_metadata_file_required")
            paths.append(access.path)
        return tuple(paths)
