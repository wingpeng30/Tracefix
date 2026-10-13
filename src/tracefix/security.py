"""Small, fixed security boundaries shared by TraceFix tools and subprocesses."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

SECURITY_POLICY_VERSION = "tracefix-basic-1"


class UnsafeWorkspacePath(ValueError):
    """A requested path is outside the fixed repository access boundary."""


@dataclass(frozen=True, slots=True)
class FileAccessPolicy:
    """Immutable internal path rules; these are never loaded from model input."""

    blocked_components: frozenset[str] = frozenset(
        {".git", ".aws", ".azure", ".docker", ".gnupg", ".kube", ".ssh", ".terraform"}
    )
    blocked_names: frozenset[str] = frozenset(
        {
            ".netrc", "credentials", "credentials.json", "id_rsa", "id_ed25519",
            "id_ecdsa", "application_default_credentials.json",
            "service-account.json",
        }
    )
    blocked_prefixes: tuple[tuple[str, ...], ...] = (
        (".config", "gcloud"), (".config", "gh"), (".config", "azure"),
        (".local", "share", "keyrings"),
    )
    template_name: str = ".env.example"


DEFAULT_FILE_ACCESS_POLICY = FileAccessPolicy()
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HARNESS_ENV_PREFIXES = ("TRACEFIX_",)
_HARNESS_ENV_NAMES = frozenset(
    {
        "HOME",
        "PATH",
        "PATHEXT",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONINSPECT",
        "PYTHONOPTIMIZE",
        "PYTHONWARNINGS",
        "TMP",
        "TEMP",
        "TMPDIR",
    }
)


def is_sensitive_environment_name(name: str) -> bool:
    """Recognize common credential names without relying on a short allowlist."""
    normalized = name.upper()
    return any(
        marker in normalized
        for marker in (
            "TOKEN", "PASSWORD", "PASSWD", "SECRET", "CREDENTIAL", "PRIVATE_KEY",
            "ACCESS_KEY", "API_KEY", "APIKEY",
        )
    ) or normalized.startswith(("AUTH_", "AUTHORIZATION")) or normalized.endswith("_KEY")


def validate_project_environment(values: Mapping[str, str]) -> dict[str, str]:
    """Validate explicitly configured project variables and detach the mapping."""
    clean: dict[str, str] = {}
    for name, value in values.items():
        normalized = name.upper()
        if (
            not _ENV_NAME.fullmatch(name)
            or normalized.startswith(("PYTEST_", *_HARNESS_ENV_PREFIXES))
            or normalized in _HARNESS_ENV_NAMES
            or is_sensitive_environment_name(name)
            or not isinstance(value, str)
        ):
            raise ValueError(
                "project environment overrides cannot set credentials, test controls, "
                "or harness variables"
            )
        clean[name] = value
    return clean


def minimal_subprocess_environment() -> dict[str, str]:
    """Copy only platform process essentials; task variables must be explicit."""
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "TZ"}
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() in allowed and not is_sensitive_environment_name(name)
    }


def _parts_are_blocked(
    parts: tuple[str, ...], protected_dirs: tuple[str, ...], policy: FileAccessPolicy
) -> bool:
    folded = tuple(part.casefold() for part in parts)
    if any(part in policy.blocked_components for part in folded):
        return True
    if any(part in policy.blocked_names for part in folded):
        return True
    if any(folded[: len(prefix)] == prefix for prefix in policy.blocked_prefixes):
        return True
    if any(
        part == ".env"
        or (part.startswith(".env.") and part != policy.template_name)
        for part in folded
    ):
        return True
    for protected in protected_dirs:
        protected_parts = tuple(
            part.casefold()
            for part in Path(protected.replace("\\", "/")).parts
            if part not in {"", "."}
        )
        if protected_parts and folded[: len(protected_parts)] == protected_parts:
            return True
    return False


def resolve_workspace_path(
    root: str | Path,
    value: str | Path,
    *,
    allow_absolute: bool = False,
    must_exist: bool = False,
    protected_dirs: tuple[str, ...] | set[str] = (),
    policy: FileAccessPolicy = DEFAULT_FILE_ACCESS_POLICY,
) -> Path:
    """Resolve one workspace path and check both its spelling and real target."""
    workspace = Path(root).expanduser().resolve()
    raw = str(value)
    if not raw or "\x00" in raw:
        raise UnsafeWorkspacePath("path is empty or contains a null byte")
    candidate = Path(raw)
    if (candidate.is_absolute() or PureWindowsPath(raw).is_absolute()) and not allow_absolute:
        raise UnsafeWorkspacePath("absolute paths are not allowed")
    if candidate.is_absolute():
        try:
            lexical = candidate.relative_to(workspace)
        except ValueError as exc:
            raise UnsafeWorkspacePath("path is outside the workspace") from exc
    else:
        lexical = candidate
    if any(part == ".." for part in lexical.parts):
        raise UnsafeWorkspacePath("path traversal is not allowed")
    protected = tuple(protected_dirs)
    if _parts_are_blocked(lexical.parts, protected, policy):
        raise UnsafeWorkspacePath("path is protected")
    try:
        resolved = (workspace / lexical).resolve(strict=False)
        relative = resolved.relative_to(workspace)
    except (OSError, RuntimeError, ValueError) as exc:
        raise UnsafeWorkspacePath("path resolves outside the workspace") from exc
    if _parts_are_blocked(relative.parts, protected, policy):
        raise UnsafeWorkspacePath("resolved path is protected")
    if must_exist and not resolved.exists():
        raise UnsafeWorkspacePath("path does not exist")
    return resolved


def pytest_target_path(target: str) -> str:
    """Validate the lexical part of a pytest file or node ID before execution."""
    path_text = target.split("::", 1)[0]
    portable_path = path_text.replace("\\", "/")
    path = Path(portable_path)
    windows_path = PureWindowsPath(path_text)
    if (
        not path_text
        or target.startswith("-")
        or "\x00" in target
        or "\n" in target
        or "\r" in target
        or path.is_absolute()
        or windows_path.is_absolute()
        or any(part == ".." for part in path.parts)
    ):
        raise ValueError("pytest targets must be relative paths or node IDs")
    return portable_path
