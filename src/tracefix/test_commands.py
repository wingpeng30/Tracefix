"""Pure pytest allowlist shared by preparation and access planning."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from tracefix.exceptions import ToolValidationError

_SHELL_CONTROL_PATTERN = re.compile(r"[|&;<>`\r\n]")


def parse_pytest_command(
    command: str, python_executable: str | None = None
) -> tuple[list[str], list[str]]:
    """Parse a fixed pytest option allowlist and return argv plus file targets."""
    if not command.strip() or _SHELL_CONTROL_PATTERN.search(command):
        raise ToolValidationError("test command contains disallowed shell syntax")
    try:
        parts = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ToolValidationError(f"invalid test command: {exc}") from exc
    if not parts:
        raise ToolValidationError("test command cannot be empty")

    executable = Path(parts[0]).name.casefold()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    direct_pytest = executable in {"pytest", "py.test"}
    module_pytest = (
        executable in {"python", "python3", "py"}
        and len(parts) >= 3
        and parts[1:3] == ["-m", "pytest"]
    )
    if not direct_pytest and not module_pytest:
        raise ToolValidationError(
            "only pytest or python -m pytest commands are allowed",
            context={"command": command},
        )
    remaining = parts[3:] if module_pytest else parts[1:]
    accepted_flags = {
        "-q",
        "-qq",
        "--quiet",
        "-v",
        "-vv",
        "-vvv",
        "--verbose",
        "-x",
        "--exitfirst",
        "-s",
        "-l",
        "--showlocals",
        "--full-trace",
        "--no-header",
        "--no-summary",
        "--disable-warnings",
        "--strict-markers",
        "--strict-config",
        "--continue-on-collection-errors",
        "--collect-only",
        "--co",
        "--version",
        "-V",
        "--help",
        "-h",
        "-ra",
        "-rA",
        "-rN",
        "--trace-config",
    }
    value_options = {"-k", "-m", "--deselect", "--tb", "--color", "--capture", "--maxfail"}
    safe_values = {
        "--tb": {"auto", "long", "short", "line", "native", "no"},
        "--color": {"yes", "no", "auto"},
        "--capture": {"fd", "sys", "tee-sys", "no"},
    }
    parsed: list[str] = []
    targets: list[str] = []
    index = 0
    while index < len(remaining):
        item = remaining[index]
        if item == "-p":
            if remaining[index : index + 2] != ["-p", "no:cacheprovider"]:
                raise ToolValidationError(
                    "pytest configuration and evidence options are managed by TraceFix"
                )
            parsed.extend(remaining[index : index + 2])
            index += 2
            continue
        if item == "--basetemp":
            if remaining[index : index + 2] != ["--basetemp", ".tracefix-test-tmp"]:
                raise ToolValidationError("only the TraceFix-owned pytest basetemp is allowed")
            parsed.extend(remaining[index : index + 2])
            index += 2
            continue
        if item.startswith("--basetemp="):
            if item != "--basetemp=.tracefix-test-tmp":
                raise ToolValidationError("only the TraceFix-owned pytest basetemp is allowed")
            parsed.append(item)
            index += 1
            continue
        if item in value_options:
            if index + 1 >= len(remaining) or remaining[index + 1].startswith("-"):
                raise ToolValidationError(f"pytest option {item} requires a safe value")
            value = remaining[index + 1]
            if item in safe_values and value not in safe_values[item]:
                raise ToolValidationError(f"unsupported value for pytest option {item}")
            if item == "--maxfail" and (not value.isdecimal() or not 1 <= int(value) <= 100):
                raise ToolValidationError("--maxfail must be between 1 and 100")
            parsed.extend((item, value))
            index += 2
            continue
        if item in accepted_flags:
            parsed.append(item)
            index += 1
            continue
        if item.startswith("--"):
            option, separator, value = item.partition("=")
            if option in {"--deselect"} and separator and value and not value.startswith("-"):
                parsed.append(item)
                index += 1
                continue
            if option in {"--maxfail", "--tb", "--color", "--capture"} and separator:
                if option == "--maxfail":
                    if not value.isdecimal() or not 1 <= int(value) <= 100:
                        raise ToolValidationError("--maxfail must be between 1 and 100")
                elif value not in safe_values[option]:
                    raise ToolValidationError(f"unsupported value for pytest option {option}")
                parsed.append(item)
                index += 1
                continue
            raise ToolValidationError(
                "pytest configuration and evidence options are managed by TraceFix",
                context={"option": item},
            )
        if item.startswith("-"):
            raise ToolValidationError(
                "pytest configuration and evidence options are managed by TraceFix",
                context={"option": item},
            )
        targets.append(item)
        parsed.append(item)
        index += 1
    # 裸 pytest 入口不会在所有平台都把 cwd 加入 sys.path；统一到当前解释器。
    selected_python = python_executable or sys.executable
    if direct_pytest:
        return [selected_python, "-m", "pytest", *parsed], targets
    # 即使模型写了 python/python3/py，也统一替换为任务配置的解释器，确保
    # 同一实验组不会因 PATH 差异悄悄切换测试环境。
    return [selected_python, "-m", "pytest", *parsed], targets
