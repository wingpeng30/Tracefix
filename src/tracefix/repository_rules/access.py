"""Pure access planning. Enumeration and actual reads belong to runtime adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from tracefix.jobs.requests import StartTestJobArguments
from tracefix.messages import ToolCall
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.paths import relative_path, validate_glob
from tracefix.test_commands import parse_pytest_command
from tracefix.tool_arguments import (
    _ApplyPatchArgs,
    _GetGitDiffArgs,
    _ReadFileArgs,
    _RunTestsArgs,
    _SearchCodeArgs,
)

PYTEST_CONFIG_NAMES = ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")
_HEADER_TOKENS = re.compile(r'"(?:[^"\\]|\\.)*"|[^\s]+')


@dataclass(frozen=True)
class PathAccess:
    path: str
    purpose: Literal["read", "write", "test", "config", "search_root"]


@dataclass(frozen=True)
class ToolAccessPlan:
    tool_name: str
    paths: tuple[PathAccess, ...]
    enumeration: Literal["none", "search", "test_targets", "changed_files"] = "none"
    # A search root is not a substitute for gating each enumerated file.
    search_glob: str | None = None


def _git_path(token: str) -> str:
    """Decode Git C-quoted UTF-8 paths without Python escapes or shell execution."""
    if not token.startswith('"'):
        if '"' in token:
            raise RuleError("rules_patch_header_invalid")
        return token
    if len(token) < 2 or not token.endswith('"'):
        raise RuleError("rules_patch_header_invalid")
    data = bytearray()
    text, index = token[1:-1], 0
    escapes = {'"': b'"', "\\": b"\\", "t": b"\t", "n": b"\n", "r": b"\r"}
    while index < len(text):
        char = text[index]
        if char == '"':
            raise RuleError("rules_patch_header_invalid")
        if char != "\\":
            data.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(text):
            raise RuleError("rules_patch_header_invalid")
        if text[index] in "01234567":
            end = index + 1
            while end < min(index + 3, len(text)) and text[end] in "01234567":
                end += 1
            value = int(text[index:end], 8)
            if value > 255:
                raise RuleError("rules_patch_header_invalid")
            data.append(value)
            index = end
        else:
            if text[index] not in escapes:
                raise RuleError("rules_patch_header_invalid")
            data.extend(escapes[text[index]])
            index += 1
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise RuleError("rules_patch_header_invalid") from exc


def patch_access_paths(patch: str) -> tuple[str, ...]:
    """Conservatively inspect all path headers before any preimage is available.

    Invalid or ambiguous headers fail closed. This does not normalize hunks,
    validate applicability, read the checkout, or enable new patch formats.
    """
    if "GIT binary patch" in patch or "Binary files " in patch:
        raise RuleError("rules_patch_binary_unsupported")
    paths: set[str] = set()

    def add(token: str, *, strip_prefix: bool) -> None:
        path = _git_path(token)
        if path == "/dev/null" and strip_prefix:
            return
        if strip_prefix and path.startswith(("a/", "b/")):
            path = path[2:]
        paths.add(relative_path(path))

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            tail = line[len("diff --git "):]
            tokens = _HEADER_TOKENS.findall(tail)
            if len(tokens) != 2 or " ".join(tokens) != tail:
                raise RuleError("rules_patch_header_invalid")
            for token in tokens:
                add(token, strip_prefix=True)
        elif line.startswith(("--- ", "+++ ")):
            add(line[4:].split("\t", 1)[0], strip_prefix=True)
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            add(line.split(" ", 2)[2], strip_prefix=False)
        elif line.startswith(("*** Update File: ", "*** Add File: ", "*** Delete File: ",
                              "*** Move to: ")):
            # Begin Patch paths are literal, including any leading a/ or b/.
            paths.add(relative_path(line.split(": ", 1)[1]))
    if not paths:
        raise RuleError("rules_patch_paths_missing")
    return tuple(sorted(paths))


class ToolAccessPlanner:
    """Build access intentions only; a plan grants no permission or delivery credit."""

    def __init__(self, *, pytest_config: str | None = None) -> None:
        if pytest_config is not None:
            relative_path(pytest_config)
            if "/" in pytest_config:
                raise RuleError("rules_pytest_config_must_be_root_file")
        self.config_paths = ((pytest_config,) if pytest_config is not None else PYTEST_CONFIG_NAMES)

    def plan(self, call: ToolCall) -> ToolAccessPlan:
        try:
            return self._plan(call)
        except ValidationError as exc:
            raise RuleError("rules_tool_arguments_invalid") from exc

    def _plan(self, call: ToolCall) -> ToolAccessPlan:
        name = call.name
        if name == "read_file":
            args = _ReadFileArgs.model_validate(call.arguments)
            return ToolAccessPlan(name, (PathAccess(relative_path(args.path), "read"),))
        if name == "apply_patch":
            args = _ApplyPatchArgs.model_validate(call.arguments)
            return ToolAccessPlan(name, tuple(PathAccess(path, "write")
                                              for path in patch_access_paths(args.patch)))
        if name in {"run_tests", "start_test_job"}:
            model = StartTestJobArguments if name == "start_test_job" else _RunTestsArgs
            args = model.model_validate(call.arguments)
            _, targets = parse_pytest_command(args.command)
            paths = tuple(dict.fromkeys(relative_path(target.split("::", 1)[0], allow_root=True)
                                        for target in targets)) or (".",)
            accesses = tuple(PathAccess(path, "test") for path in paths)
            accesses += tuple(PathAccess(path, "config") for path in self.config_paths)
            return ToolAccessPlan(name, accesses, "test_targets")
        if name == "search_code":
            args = _SearchCodeArgs.model_validate(call.arguments)
            root = relative_path(args.path, allow_root=True)
            return ToolAccessPlan(name, (PathAccess(root, "search_root"),), "search",
                                  validate_glob(args.glob))
        if name == "get_git_diff":
            _GetGitDiffArgs.model_validate(call.arguments)
            return ToolAccessPlan(name, (), "changed_files")
        raise RuleError("rules_tool_access_description_missing")
