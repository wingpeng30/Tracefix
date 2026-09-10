"""面向本地 Git 工作区的五个基础工具实现。"""

from __future__ import annotations

import difflib
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from tracefix.exceptions import ToolExecutionError, ToolValidationError
from tracefix.messages import ToolCall
from tracefix.tools.base import BaseTool, ReservedToolName, ToolRegistry, ToolResult, ToolSpec

_SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
_SHELL_CONTROL_PATTERN = re.compile(r"[|&;<>`\r\n]")
_SENSITIVE_ENV_MARKERS = ("API_KEY", "ACCESS_TOKEN", "PASSWORD", "SECRET", "CREDENTIAL")


def _truncate_text(value: str, limit: int) -> tuple[str, bool]:
    """保留输出首尾，避免长日志把关键错误栈尾部完全截掉。"""
    if len(value) <= limit:
        return value, False
    marker = "\n... [TraceFix 已截断中间输出] ...\n"
    available = max(0, limit - len(marker))
    head_size = int(available * 0.6)
    tail_size = available - head_size
    return value[:head_size] + marker + value[-tail_size:], True


def _decode_timeout_output(value: str | bytes | None) -> str:
    """统一不同 Python/平台在超时异常中返回的文本类型。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _sanitized_subprocess_env() -> dict[str, str]:
    """复制必要环境并移除模型密钥，避免被目标仓库的测试代码读取。"""
    safe = dict(os.environ)
    for name in tuple(safe):
        normalized = name.upper()
        if any(marker in normalized for marker in _SENSITIVE_ENV_MARKERS):
            safe.pop(name, None)
    return safe


def _is_blocked_secret_file(name: str) -> bool:
    """阻止真实 dotenv 文件，同时允许公开的 .env.example 模板。"""
    normalized = name.casefold()
    return normalized == ".env" or (
        normalized.startswith(".env.") and normalized != ".env.example"
    )


def _resolve_workspace(workspace: str | Path) -> Path:
    """解析并验证工具共享的 Git 仓库根目录。"""
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ToolValidationError(
            "workspace must be an existing directory",
            context={"workspace": str(root)},
        )
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolExecutionError(
            f"cannot inspect Git workspace: {exc}",
            context={"workspace": str(root)},
        ) from exc
    if result.returncode != 0:
        raise ToolValidationError(
            "workspace must be a Git repository with an initial commit",
            context={"workspace": str(root), "stderr": result.stderr.strip()},
        )
    return root


def _resolve_path(
    root: Path,
    value: str,
    *,
    must_exist: bool | None = None,
) -> Path:
    """把模型提供的相对路径限制在仓库内，并阻止访问 Git 元数据。"""
    if not value or "\x00" in value:
        raise ToolValidationError("path cannot be empty or contain null bytes")
    relative = Path(value)
    if relative.is_absolute():
        raise ToolValidationError("absolute paths are not allowed", context={"path": value})
    if any(part.casefold() == ".git" or part == ".." for part in relative.parts):
        raise ToolValidationError(
            "path traversal and .git access are not allowed",
            context={"path": value},
        )
    if _is_blocked_secret_file(relative.name):
        raise ToolValidationError(
            "secret environment files cannot be accessed",
            context={"path": value},
        )

    # resolve(strict=False) 仍会解析已经存在的符号链接，因此可拦截链接逃逸。
    resolved = (root / relative).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolValidationError(
            "path resolves outside the workspace",
            context={"path": value},
        ) from exc

    if must_exist is True and not resolved.exists():
        raise ToolValidationError("path does not exist", context={"path": value})
    if must_exist is False and resolved.exists():
        return resolved
    return resolved


class _WorkspaceTool(BaseTool):
    """为具体工具提供统一的工作区和参数验证逻辑。"""

    _SPEC: ClassVar[ToolSpec]

    def __init__(self, workspace: Path, *, max_output_chars: int = 20_000) -> None:
        if max_output_chars < 1:
            raise ValueError("max_output_chars must be positive")
        self.workspace = workspace
        self.max_output_chars = max_output_chars

    @property
    def spec(self) -> ToolSpec:
        """返回当前工具的稳定模型定义。"""
        return self._SPEC

    def _validate_call(self, call: ToolCall, model: type[BaseModel]) -> BaseModel:
        """校验工具名和 JSON 参数，并映射为稳定参数异常。"""
        if call.name != self.spec.name:
            raise ToolValidationError(
                "tool call name does not match tool",
                context={"expected": self.spec.name, "actual": call.name},
            )
        try:
            return model.model_validate(call.arguments)
        except ValidationError as exc:
            raise ToolValidationError(
                "invalid tool arguments",
                context={"tool_name": call.name, "errors": exc.errors(include_url=False)},
            ) from exc


class _SearchCodeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    path: str = "."
    glob: str = "**/*"
    case_sensitive: bool = False
    max_results: int = Field(default=50, ge=1, le=200)


class SearchCodeTool(_WorkspaceTool):
    """在仓库文本文件中执行确定性的字面量代码搜索。"""

    _SPEC = ToolSpec(
        name=ReservedToolName.SEARCH_CODE.value,
        description="在仓库文本文件中搜索关键词，返回文件路径、行号和匹配文本。",
        input_schema=_SearchCodeArgs.model_json_schema(),
    )

    def execute(self, call: ToolCall) -> ToolResult:
        """搜索代码并返回最多 max_results 条逐行匹配结果。"""
        started = time.monotonic()
        args = self._validate_call(call, _SearchCodeArgs)
        assert isinstance(args, _SearchCodeArgs)
        base = _resolve_path(self.workspace, args.path, must_exist=True)
        if not base.is_dir() and not base.is_file():
            raise ToolValidationError("search path must be a file or directory")

        if base.is_file():
            candidates = [base]
        else:
            try:
                candidates = sorted(base.glob(args.glob))
            except (OSError, ValueError) as exc:
                raise ToolValidationError(
                    f"invalid search glob: {exc}", context={"glob": args.glob}
                ) from exc

        matches: list[dict[str, str | int]] = []
        searched_files = 0
        truncated = False
        stop_search = False
        needle = args.query if args.case_sensitive else args.query.casefold()

        for path in candidates:
            if not path.is_file():
                continue
            relative = path.relative_to(self.workspace)
            if any(part in _SKIPPED_DIRECTORIES for part in relative.parts):
                continue
            if _is_blocked_secret_file(relative.name):
                continue
            # glob 可能命中符号链接文件，读取前再次解析以阻止链接逃逸。
            path = _resolve_path(self.workspace, relative.as_posix(), must_exist=True)
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8_192]:
                continue

            searched_files += 1
            text = raw.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                haystack = line if args.case_sensitive else line.casefold()
                column = haystack.find(needle)
                if column < 0:
                    continue
                shown_line, line_truncated = _truncate_text(line, 500)
                matches.append(
                    {
                        "path": relative.as_posix(),
                        "line": line_number,
                        "column": column + 1,
                        "text": shown_line,
                    }
                )
                if line_truncated:
                    truncated = True
                if len(matches) > args.max_results:
                    matches.pop()
                    truncated = True
                    stop_search = True
                    break
            if stop_search:
                break

        return ToolResult(
            call_id=call.id,
            tool_name=self.spec.name,
            success=True,
            output={
                "query": args.query,
                "matches": matches,
                "searched_files": searched_files,
                "truncated": truncated,
            },
            duration_ms=(time.monotonic() - started) * 1000,
        )


class _ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> _ReadFileArgs:
        """拒绝反向行号范围。"""
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line cannot be earlier than start_line")
        return self


class ReadFileTool(_WorkspaceTool):
    """按行号读取仓库内的 UTF-8 文本文件。"""

    _SPEC = ToolSpec(
        name=ReservedToolName.READ_FILE.value,
        description="读取仓库内文件的指定行范围，并返回带行号的文本。",
        input_schema=_ReadFileArgs.model_json_schema(),
    )

    def execute(self, call: ToolCall) -> ToolResult:
        """读取最多 400 行，超出部分通过 truncated 标记告知模型。"""
        started = time.monotonic()
        args = self._validate_call(call, _ReadFileArgs)
        assert isinstance(args, _ReadFileArgs)
        path = _resolve_path(self.workspace, args.path, must_exist=True)
        if not path.is_file():
            raise ToolValidationError("read_file path must be a file")
        try:
            if path.stat().st_size > 1_000_000:
                raise ToolValidationError(
                    "file is larger than the 1 MB V0.1 limit",
                    context={"path": args.path},
                )
            raw = path.read_bytes()
        except ToolValidationError:
            raise
        except OSError as exc:
            raise ToolExecutionError(
                f"cannot read file: {exc}", context={"path": args.path}
            ) from exc
        if b"\x00" in raw[:8_192]:
            raise ToolValidationError("binary files cannot be read", context={"path": args.path})

        lines = raw.decode("utf-8", errors="replace").splitlines()
        requested_end = args.end_line or len(lines)
        actual_end = min(requested_end, args.start_line + 399, len(lines))
        selected = lines[args.start_line - 1 : actual_end]
        content = "\n".join(
            f"{line_number:>6} | {line}"
            for line_number, line in enumerate(selected, start=args.start_line)
        )
        content, content_truncated = _truncate_text(content, self.max_output_chars)
        return ToolResult(
            call_id=call.id,
            tool_name=self.spec.name,
            success=True,
            output={
                "path": path.relative_to(self.workspace).as_posix(),
                "start_line": args.start_line,
                "end_line": actual_end,
                "total_lines": len(lines),
                "content": content,
                "truncated": (
                    actual_end < requested_end or actual_end < len(lines) or content_truncated
                ),
            },
            duration_ms=(time.monotonic() - started) * 1000,
        )


class _ApplyPatchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patch: str = Field(min_length=1)


class ApplyPatchTool(_WorkspaceTool):
    """预检并应用 Git unified diff 或常见的 Begin Patch 更新补丁。"""

    _SPEC = ToolSpec(
        name=ReservedToolName.APPLY_PATCH.value,
        description=(
            "使用 git apply 预检并应用补丁。patch 可传标准 Git unified diff，"
            "也可传 *** Begin Patch / *** Update File: path / @@ 格式的文件更新块。"
        ),
        input_schema=_ApplyPatchArgs.model_json_schema(),
    )

    def execute(self, call: ToolCall) -> ToolResult:
        """先执行无副作用检查，确认安全且可应用后才修改工作区。"""
        started = time.monotonic()
        args = self._validate_call(call, _ApplyPatchArgs)
        assert isinstance(args, _ApplyPatchArgs)
        # 先把模型常见的 Begin Patch 格式转换为标准 diff。转换只操作内存，
        # 路径或上下文不合法时不会提前写文件。
        patch = self._normalize_patch(args.patch)
        changed_files = self._validate_patch_paths(patch)

        checked = self._run_git_apply(patch, check=True)
        if checked.returncode != 0:
            stdout, _ = _truncate_text(checked.stdout, self.max_output_chars)
            stderr, _ = _truncate_text(checked.stderr, self.max_output_chars)
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=False,
                output={"stdout": stdout, "stderr": stderr},
                error="patch validation failed",
                duration_ms=(time.monotonic() - started) * 1000,
            )

        applied = self._run_git_apply(patch, check=False)
        if applied.returncode != 0:
            stdout, _ = _truncate_text(applied.stdout, self.max_output_chars)
            stderr, _ = _truncate_text(applied.stderr, self.max_output_chars)
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=False,
                output={"stdout": stdout, "stderr": stderr},
                error="git apply failed after validation",
                duration_ms=(time.monotonic() - started) * 1000,
            )
        return ToolResult(
            call_id=call.id,
            tool_name=self.spec.name,
            success=True,
            output={
                "changed_files": changed_files,
                "stdout": applied.stdout,
                "stderr": applied.stderr,
            },
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def _normalize_patch(self, patch: str) -> str:
        """把受支持的模型补丁规范化为可交给 Git 的 unified diff。"""
        stripped = patch.strip()
        if stripped.startswith("*** Begin Patch"):
            return self._convert_begin_patch(stripped)

        # 模型偶尔会在标准 diff 末尾误带 Codex 结束标记。它不是 diff 内容，
        # 可以确定性移除；其他未知控制行仍交由 git apply 拒绝。
        lines = patch.splitlines()
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip() == "*** End Patch":
            lines.pop()
        return "\n".join(lines) + "\n"

    def _convert_begin_patch(self, patch: str) -> str:
        """将仅包含 Update File 的 Begin Patch 安全转换为标准 diff。"""
        lines = patch.splitlines()
        if not lines or lines[0].strip() != "*** Begin Patch":
            raise ToolValidationError("invalid Begin Patch header")
        if len(lines) < 3 or lines[-1].strip() != "*** End Patch":
            raise ToolValidationError("Begin Patch is missing *** End Patch")

        index = 1
        converted: list[str] = []
        updated_files = 0
        while index < len(lines) - 1:
            directive = lines[index]
            if not directive.startswith("*** Update File: "):
                raise ToolValidationError(
                    "only *** Update File is supported inside Begin Patch",
                    context={"directive": directive},
                )
            relative_name = directive.removeprefix("*** Update File: ").strip()
            path = _resolve_path(self.workspace, relative_name, must_exist=True)
            if not path.is_file():
                raise ToolValidationError(
                    "Update File target must be a regular file",
                    context={"path": relative_name},
                )

            index += 1
            body: list[str] = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                body.append(lines[index])
                index += 1
            if not body:
                raise ToolValidationError(
                    "Update File section cannot be empty",
                    context={"path": relative_name},
                )

            original = path.read_text(encoding="utf-8")
            updated = self._apply_update_hunks(original, body, relative_name)
            if updated == original:
                raise ToolValidationError(
                    "patch does not change the target file",
                    context={"path": relative_name},
                )
            # difflib 负责生成准确的 hunk 范围；之后仍由 Git 做完整预检。
            diff = difflib.unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile=f"a/{Path(relative_name).as_posix()}",
                tofile=f"b/{Path(relative_name).as_posix()}",
                lineterm="",
            )
            converted.extend(diff)
            updated_files += 1

        if updated_files == 0:
            raise ToolValidationError("Begin Patch does not contain any Update File section")
        return "\n".join(converted) + "\n"

    @staticmethod
    def _apply_update_hunks(original: str, body: list[str], relative_name: str) -> str:
        """按上下文匹配更新块；找不到或匹配不唯一时拒绝猜测。"""
        current = original.splitlines()
        had_final_newline = original.endswith(("\n", "\r"))
        index = 0
        search_from = 0
        hunk_count = 0

        while index < len(body):
            header = body[index]
            if not header.startswith("@@"):
                raise ToolValidationError(
                    "Update File content must start with an @@ hunk",
                    context={"path": relative_name, "line": header},
                )
            index += 1
            hunk: list[str] = []
            while index < len(body) and not body[index].startswith("@@"):
                line = body[index]
                if line == "\\ No newline at end of file":
                    index += 1
                    continue
                if not line or line[0] not in {" ", "+", "-"}:
                    raise ToolValidationError(
                        "invalid line in Update File hunk",
                        context={"path": relative_name, "line": line},
                    )
                hunk.append(line)
                index += 1

            old_lines = [line[1:] for line in hunk if line[0] in {" ", "-"}]
            new_lines = [line[1:] for line in hunk if line[0] in {" ", "+"}]
            if not old_lines:
                raise ToolValidationError(
                    "Update File hunk needs context or removed lines",
                    context={"path": relative_name},
                )

            candidates = [
                offset
                for offset in range(search_from, len(current) - len(old_lines) + 1)
                if current[offset : offset + len(old_lines)] == old_lines
            ]
            # 若标准范围头给出了旧起始行，优先使用它消除重复上下文的歧义。
            match = re.match(r"@@\s+-(\d+)", header)
            hinted = int(match.group(1)) - 1 if match else None
            if hinted is not None and hinted in candidates:
                target = hinted
            elif len(candidates) == 1:
                target = candidates[0]
            elif not candidates:
                raise ToolValidationError(
                    "Update File hunk context was not found",
                    context={"path": relative_name, "hunk": header},
                )
            else:
                raise ToolValidationError(
                    "Update File hunk context is ambiguous",
                    context={"path": relative_name, "hunk": header},
                )

            current[target : target + len(old_lines)] = new_lines
            search_from = target + len(new_lines)
            hunk_count += 1

        if hunk_count == 0:
            raise ToolValidationError(
                "Update File section does not contain any hunks",
                context={"path": relative_name},
            )
        updated = "\n".join(current)
        if had_final_newline:
            updated += "\n"
        return updated

    def _validate_patch_paths(self, patch: str) -> list[str]:
        """提取补丁头中的路径并拒绝二进制补丁或仓库外路径。"""
        if "GIT binary patch" in patch or "Binary files " in patch:
            raise ToolValidationError("binary patches are not supported")

        paths: set[str] = set()
        for line in patch.splitlines():
            tokens: list[str] = []
            if line.startswith("diff --git "):
                try:
                    tokens = shlex.split(line)[2:4]
                except ValueError as exc:
                    raise ToolValidationError(f"invalid diff header: {exc}") from exc
            elif line.startswith(("--- ", "+++ ")):
                token = line[4:].split("\t", 1)[0].strip()
                if token.startswith('"'):
                    try:
                        parsed = shlex.split(token)
                        token = parsed[0] if parsed else ""
                    except ValueError as exc:
                        raise ToolValidationError(f"invalid patch path: {exc}") from exc
                tokens = [token]

            for token in tokens:
                if token == "/dev/null":
                    continue
                normalized = token[2:] if token.startswith(("a/", "b/")) else token
                _resolve_path(self.workspace, normalized)
                paths.add(Path(normalized).as_posix())

        if not paths:
            raise ToolValidationError("patch does not contain any file paths")
        return sorted(paths)

    def _run_git_apply(self, patch: str, *, check: bool) -> subprocess.CompletedProcess[str]:
        """通过标准输入传递补丁，避免生成临时文件或发生 Shell 插值。"""
        # --recount 只重算 hunk 行数，不放宽上下文匹配；可恢复模型常见的
        # “修改内容正确但 @@ 行数写错”问题，同时维持 check-before-write。
        command = ["git", "apply", "--whitespace=nowarn", "--recount"]
        if check:
            command.append("--check")
        command.append("-")
        try:
            # Windows 文本管道会把 LF 转成 CRLF，使补丁上下文凭空多出 \r；
            # 直接发送 UTF-8 字节可让相同 gold patch 跨平台稳定应用。
            raw = subprocess.run(
                command,
                cwd=self.workspace,
                input=patch.encode("utf-8"),
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolExecutionError(f"cannot run git apply: {exc}") from exc
        return subprocess.CompletedProcess(
            args=raw.args,
            returncode=raw.returncode,
            stdout=raw.stdout.decode("utf-8", errors="replace"),
            stderr=raw.stderr.decode("utf-8", errors="replace"),
        )


class _RunTestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = "pytest -q"
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)


class RunTestsTool(_WorkspaceTool):
    """不经 Shell 启动受限的 pytest 测试命令。"""

    _SPEC = ToolSpec(
        name=ReservedToolName.RUN_TESTS.value,
        description="运行 pytest 测试；命令不支持管道、重定向或其他 Shell 语法。",
        input_schema=_RunTestsArgs.model_json_schema(),
    )

    def __init__(
        self,
        workspace: Path,
        *,
        max_output_chars: int = 20_000,
        default_timeout_seconds: float = 120,
        python_executable: str | Path | None = None,
        pythonpath_entries: tuple[Path, ...] = (),
    ) -> None:
        super().__init__(workspace, max_output_chars=max_output_chars)
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        self.default_timeout_seconds = default_timeout_seconds
        # Agent 进程和历史项目测试环境可以使用不同 Python；默认保持原有行为。
        self.python_executable = str(
            Path(python_executable).expanduser().resolve()
            if python_executable is not None
            else Path(sys.executable).resolve()
        )
        self.pythonpath_entries = tuple(
            Path(entry).expanduser().resolve() for entry in pythonpath_entries
        )
        if any(not entry.is_dir() for entry in self.pythonpath_entries):
            raise ValueError("test PYTHONPATH entries must be existing directories")

    def execute(self, call: ToolCall) -> ToolResult:
        """执行 pytest，并把失败和超时作为模型可以继续处理的结果返回。"""
        started = time.monotonic()
        args = self._validate_call(call, _RunTestsArgs)
        assert isinstance(args, _RunTestsArgs)
        command = self._parse_command(args.command, self.python_executable)
        timeout = args.timeout_seconds or self.default_timeout_seconds

        try:
            environment = _sanitized_subprocess_env()
            # 源码布局项目（如 pytest 的 src/）必须优先导入 Agent 修改后的工作区，
            # 不能误用测试虚拟环境中为准备依赖而安装的旧 editable package。
            import_roots = [str(self.workspace)]
            if (self.workspace / "src").is_dir():
                import_roots.insert(0, str(self.workspace / "src"))
            import_roots[0:0] = [str(path) for path in self.pythonpath_entries]
            inherited_pythonpath = environment.get("PYTHONPATH")
            if inherited_pythonpath:
                # 继承值可能本身包含多个目录，必须作为原始字符串拼接，不能把
                # 整串内容错误地解释成单个 Path。
                import_roots.append(inherited_pythonpath)
            environment["PYTHONPATH"] = os.pathsep.join(import_roots)
            # 临时目录必须位于仓库内：部分项目的嵌套 pytest 会沿父目录寻找
            # pyproject，放到仓库外可能误读 TraceFix 自身配置。通过仅修改本克隆
            # 的 .git/info/exclude 隐藏副产物，不改变受测源码或共享 .gitignore。
            test_tmp = self.workspace / ".tracefix-test-tmp"
            test_tmp.mkdir(parents=True, exist_ok=True)
            exclude = self.workspace / ".git" / "info" / "exclude"
            if exclude.is_file():
                current = exclude.read_text(encoding="utf-8", errors="replace")
                marker = ".tracefix-test-tmp/"
                if marker not in current.splitlines():
                    with exclude.open("a", encoding="utf-8") as stream:
                        if current and not current.endswith("\n"):
                            stream.write("\n")
                        stream.write(f"{marker}\n")
            environment["TMP"] = str(test_tmp)
            environment["TEMP"] = str(test_tmp)
            completed = subprocess.run(
                command,
                cwd=self.workspace,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                shell=False,
            )
            stdout, stdout_truncated = _truncate_text(completed.stdout, self.max_output_chars)
            stderr, stderr_truncated = _truncate_text(completed.stderr, self.max_output_chars)
            passed = completed.returncode == 0
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=passed,
                output={
                    "command": command,
                    "returncode": completed.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "timed_out": False,
                },
                error=None if passed else f"tests failed with exit code {completed.returncode}",
                metadata={"truncated": stdout_truncated or stderr_truncated},
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except subprocess.TimeoutExpired as exc:
            stdout, stdout_truncated = _truncate_text(
                _decode_timeout_output(exc.stdout), self.max_output_chars
            )
            stderr, stderr_truncated = _truncate_text(
                _decode_timeout_output(exc.stderr), self.max_output_chars
            )
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=False,
                output={
                    "command": command,
                    "returncode": None,
                    "stdout": stdout,
                    "stderr": stderr,
                    "timed_out": True,
                },
                error=f"test command timed out after {timeout} seconds",
                metadata={"truncated": stdout_truncated or stderr_truncated},
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except OSError as exc:
            raise ToolExecutionError(
                f"cannot start test command: {exc}", context={"command": command}
            ) from exc

    @staticmethod
    def _parse_command(command: str, python_executable: str | None = None) -> list[str]:
        """只接受 pytest 形态，并明确拒绝 Shell 控制语法。"""
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
        # 裸 pytest 入口不会在所有平台都把 cwd 加入 sys.path；统一到当前解释器。
        selected_python = python_executable or sys.executable
        if direct_pytest:
            return [selected_python, "-m", "pytest", *parts[1:]]
        # 即使模型写了 python/python3/py，也统一替换为任务配置的解释器，确保
        # 同一实验组不会因 PATH 差异悄悄切换测试环境。
        return [selected_python, *parts[1:]]


class _GetGitDiffArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_lines: int = Field(default=3, ge=0, le=20)


class GetGitDiffTool(_WorkspaceTool):
    """读取相对 HEAD 的 tracked 和 untracked 文件差异。"""

    _SPEC = ToolSpec(
        name=ReservedToolName.GET_GIT_DIFF.value,
        description="查看工作区相对 HEAD 的完整 unified diff，包括未跟踪文件。",
        input_schema=_GetGitDiffArgs.model_json_schema(),
    )

    def execute(self, call: ToolCall) -> ToolResult:
        """在不暂存文件的前提下组合 tracked 与 untracked diff。"""
        started = time.monotonic()
        args = self._validate_call(call, _GetGitDiffArgs)
        assert isinstance(args, _GetGitDiffArgs)

        tracked = self._run_git(
            ["diff", "--no-ext-diff", f"--unified={args.context_lines}", "HEAD", "--"]
        )
        if tracked.returncode != 0:
            return self._git_failure(call, tracked, started)

        names = self._run_git(["diff", "--name-only", "HEAD", "--"])
        untracked = self._run_git(["ls-files", "--others", "--exclude-standard", "-z"])
        if names.returncode != 0 or untracked.returncode != 0:
            return self._git_failure(call, names if names.returncode else untracked, started)

        changed_files = [line for line in names.stdout.splitlines() if line]
        untracked_files = sorted(path for path in untracked.stdout.split("\0") if path)
        patches = [tracked.stdout]
        for relative in untracked_files:
            path = _resolve_path(self.workspace, relative, must_exist=True)
            generated = self._run_git(
                [
                    "diff",
                    "--no-index",
                    "--no-ext-diff",
                    f"--unified={args.context_lines}",
                    "--",
                    os.devnull,
                    str(path),
                ]
            )
            # git diff --no-index 用退出码 1 表示“确实存在差异”。
            if generated.returncode not in {0, 1}:
                return self._git_failure(call, generated, started)
            patches.append(generated.stdout)
            changed_files.append(relative)

        full_diff = "".join(patches)
        shown_diff, truncated = _truncate_text(full_diff, self.max_output_chars)
        return ToolResult(
            call_id=call.id,
            tool_name=self.spec.name,
            success=True,
            output={
                "diff": shown_diff,
                "changed_files": sorted(set(changed_files)),
                "diff_chars": len(full_diff),
                "truncated": truncated,
            },
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def _run_git(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        """使用参数数组调用 Git，避免路径或参数进入 Shell。"""
        try:
            return subprocess.run(
                ["git", *arguments],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ToolExecutionError(f"cannot run git command: {exc}") from exc

    def _git_failure(
        self,
        call: ToolCall,
        result: subprocess.CompletedProcess[str],
        started: float,
    ) -> ToolResult:
        """把 Git 非预期退出统一转换为失败结果。"""
        return ToolResult(
            call_id=call.id,
            tool_name=self.spec.name,
            success=False,
            output={
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            error="git diff command failed",
            duration_ms=(time.monotonic() - started) * 1000,
        )


def create_default_tool_registry(
    workspace: str | Path,
    *,
    max_output_chars: int = 20_000,
    test_timeout_seconds: float = 120,
    test_python_executable: str | Path | None = None,
    test_pythonpath_entries: tuple[Path, ...] = (),
) -> ToolRegistry:
    """为一个已有初始提交的 Git 仓库创建五工具注册表。"""
    root = _resolve_workspace(workspace)
    return ToolRegistry(
        [
            SearchCodeTool(root, max_output_chars=max_output_chars),
            ReadFileTool(root, max_output_chars=max_output_chars),
            ApplyPatchTool(root, max_output_chars=max_output_chars),
            RunTestsTool(
                root,
                max_output_chars=max_output_chars,
                default_timeout_seconds=test_timeout_seconds,
                python_executable=test_python_executable,
                pythonpath_entries=test_pythonpath_entries,
            ),
            GetGitDiffTool(root, max_output_chars=max_output_chars),
        ]
    )
