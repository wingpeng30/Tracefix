"""Pure portable paths and bounded glob matching, with no filesystem access."""

from __future__ import annotations

import fnmatch
from functools import cache

from tracefix.repository_rules.errors import RuleError
from tracefix.security import DEFAULT_FILE_ACCESS_POLICY

RULE_CONFIG_PATH = ".tracefix/rules.toml"
RULE_BODY_DIRECTORY = ".tracefix/rules"
MAX_PATH_CHARS = 1024
MAX_PATH_COMPONENTS = 64
_INTERNAL = frozenset({".tracefix-build-tmp", ".tracefix-test-tmp"})
_DEVICES = frozenset({"con", "prn", "aux", "nul", "conin$", "conout$",
                      *(f"{kind}{index}" for kind in ("com", "lpt") for index in range(1, 10))})


def relative_path(value: str, *, allow_root: bool = False) -> str:
    if value == "." and allow_root:
        return value
    if (not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS
            or value != value.strip() or value.startswith("/") or "\\" in value
            or ":" in value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise RuleError("rules_invalid_relative_path")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RuleError("rules_invalid_relative_path") from exc
    parts = value.split("/")
    policy = DEFAULT_FILE_ACCESS_POLICY
    folded = tuple(part.casefold() for part in parts)
    if (len(parts) > MAX_PATH_COMPONENTS
            or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts)
            or any(part in policy.blocked_components or part in policy.blocked_names
                   or part in _INTERNAL or part.split(".", 1)[0] in _DEVICES
                   or part == ".env" or part.startswith(".env.") and part != policy.template_name
                   for part in folded)
            or any(folded[:len(prefix)] == prefix for prefix in policy.blocked_prefixes)):
        raise RuleError("rules_protected_or_ambiguous_path")
    return value


def rule_source_path(value: str) -> str:
    value = relative_path(value)
    if (value == RULE_CONFIG_PATH or value.rsplit("/", 1)[-1] == "AGENTS.md"
            or value.startswith(RULE_BODY_DIRECTORY + "/") and value.casefold().endswith(".md")):
        return value
    raise RuleError("rules_unsupported_source_path")


def validate_glob(value: str) -> str:
    relative_path(value)
    if (len(value) > 512 or any(char in value for char in "[]{}!")
            or any("**" in part and part != "**" for part in value.split("/"))):
        raise RuleError("rules_unsupported_glob")
    return value


def matches_glob(pattern: str, target: str, *, case_sensitive: bool = True) -> bool:
    validate_glob(pattern)
    relative_path(target)
    if not case_sensitive:
        pattern, target = pattern.casefold(), target.casefold()
    patterns, parts = pattern.split("/"), target.split("/")

    @cache
    def match(index: int, position: int) -> bool:
        # Both axes are bounded by MAX_PATH_COMPONENTS, including recursive '**'.
        if index == len(patterns):
            return position == len(parts)
        if patterns[index] == "**":
            return match(index + 1, position) or (
                position < len(parts) and match(index, position + 1))
        return (position < len(parts) and fnmatch.fnmatchcase(parts[position], patterns[index])
                and match(index + 1, position + 1))

    return match(0, 0)
