"""只使用 Python 标准库构建 AST 符号索引和任务相关 Repo Map。"""

# Repo Map 文本和 Pydantic 字段构造为了可读的领域语义会出现不可拆分长字符串。
# ruff 的自动格式化器不会拆分它们，故仅在本文件关闭 E501。
# ruff: noqa: E501

from __future__ import annotations

import ast
import re
import warnings
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_SKIPPED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "build", "dist"}
_WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DOTTED_NAME = re.compile(r"\b(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*\b")
_STOP_WORDS = {
    "and", "are", "but", "can", "does", "for", "from", "has", "have", "into",
    "not", "now", "that", "the", "this", "with", "when", "will", "would",
}
_LOW_PRIORITY_DIRS = {"doc", "docs", "example", "examples", "bench", "testing", "tests"}


class SymbolKind(StrEnum):
    """Repo Map 支持的 Python 声明类型。"""

    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"


class Symbol(BaseModel):
    """一个可定位到源文件和行号的 Python 符号。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    qualified_name: str
    kind: SymbolKind
    path: str
    lineno: int = Field(ge=1)
    end_lineno: int = Field(ge=1)


class IndexedFile(BaseModel):
    """单个 Python 文件的静态分析结果；调用关系仅是保守启发式。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    module: str
    imports: tuple[str, ...] = ()
    resolved_imports: tuple[str, ...] = ()
    calls: tuple[str, ...] = ()
    resolved_calls: tuple[str, ...] = ()
    related_tests: tuple[str, ...] = ()
    symbols: tuple[Symbol, ...] = ()
    is_test: bool = False
    is_package: bool = False


class RepositoryIndex(BaseModel):
    """完整但紧凑的仓库静态索引，不包含源代码正文。"""

    model_config = ConfigDict(extra="forbid")

    files: tuple[IndexedFile, ...] = ()
    skipped_files: tuple[str, ...] = ()
    module_to_files: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    symbol_to_files: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @property
    def symbol_count(self) -> int:
        """返回已解析的符号数量。"""
        return sum(len(item.symbols) for item in self.files)


class RepoMapConfig(BaseModel):
    """索引范围和注入给模型的 Repo Map 上限。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = True
    max_file_bytes: int = Field(default=256_000, ge=1_024)
    max_indexed_files: int = Field(default=5_000, ge=1)
    max_candidate_files: int = Field(default=12, ge=1)
    max_symbols_per_file: int = Field(default=8, ge=1)
    max_chars: int = Field(default=12_000, ge=512)
    graph_seed_files: int = Field(default=6, ge=1)
    max_graph_neighbors: int = Field(default=8, ge=0)


class RepoMap(BaseModel):
    """一次任务对应的确定性静态上下文和选择指标。"""

    model_config = ConfigDict(extra="forbid")

    text: str
    candidate_files: tuple[str, ...] = ()
    related_tests: tuple[str, ...] = ()
    indexed_file_count: int = Field(ge=0)
    symbol_count: int = Field(ge=0)
    skipped_file_count: int = Field(ge=0)
    graph_expanded_file_count: int = Field(default=0, ge=0)


class _Visitor(ast.NodeVisitor):
    """提取声明、导入和简单调用表达式，不尝试做不可靠的类型推断。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.symbols: list[Symbol] = []
        self.imports: list[str] = []
        self.calls: list[str] = []
        self._parents: list[tuple[str, SymbolKind]] = []

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        self.imports.extend(alias.name for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        prefix = "." * node.level + (node.module or "")
        self.imports.extend(
            f"{prefix}.{alias.name}" if prefix else alias.name for alias in node.names
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._add_symbol(node, SymbolKind.CLASS)
        self._parents.append((node.name, SymbolKind.CLASS))
        self.generic_visit(node)
        self._parents.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        kind = (
            SymbolKind.METHOD
            if self._parents and self._parents[-1][1] is SymbolKind.CLASS
            else SymbolKind.FUNCTION
        )
        self._add_symbol(node, kind)
        self._parents.append((node.name, kind))
        self.generic_visit(node)
        self._parents.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = _call_name(node.func)
        if name:
            self.calls.append(name)
        self.generic_visit(node)

    def _add_symbol(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, kind: SymbolKind
    ) -> None:
        prefix = ".".join(parent[0] for parent in self._parents)
        qualified = f"{prefix}.{node.name}" if prefix else node.name
        self.symbols.append(
            Symbol(
                name=node.name,
                qualified_name=qualified,
                kind=kind,
                path=self.path,
                lineno=node.lineno,
                end_lineno=getattr(node, "end_lineno", node.lineno),
            )
        )


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


class RepositoryIndexer:
    """扫描工作区内 Python 文件，并按任务关键词构造保守、可复现的 Repo Map。"""

    def __init__(self, workspace: Path, config: RepoMapConfig | None = None) -> None:
        self.workspace = workspace.resolve()
        self.config = config or RepoMapConfig()

    def build(self) -> RepositoryIndex:
        """构建完整索引；单个坏编码或语法文件只会被记录，不中止任务。"""
        files: list[IndexedFile] = []
        skipped: list[str] = []
        for path in sorted(self.workspace.rglob("*.py")):
            relative = path.relative_to(self.workspace)
            if any(part in _SKIPPED_DIRS for part in relative.parts):
                continue
            if (
                len(files) >= self.config.max_indexed_files
                or path.stat().st_size > self.config.max_file_bytes
            ):
                skipped.append(relative.as_posix())
                continue
            try:
                # 真实仓库常把非法转义写进诊断测试夹具。Python 3.12 会在 AST
                # 解析时输出 SyntaxWarning；索引只需知道是否能构建语法树，不应把
                # 上游夹具的预期警告混入 Agent 运行日志。
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", SyntaxWarning)
                    tree = ast.parse(
                        path.read_text(encoding="utf-8"), filename=relative.as_posix()
                    )
            except (OSError, UnicodeError, SyntaxError):
                skipped.append(relative.as_posix())
                continue
            visitor = _Visitor(relative.as_posix())
            visitor.visit(tree)
            files.append(
                IndexedFile(
                    path=relative.as_posix(),
                    module=_module_name(relative),
                    imports=tuple(sorted(set(visitor.imports))),
                    calls=tuple(sorted(set(visitor.calls))),
                    symbols=tuple(visitor.symbols),
                    is_test=_is_test(relative),
                    is_package=relative.name == "__init__.py",
                )
            )
        module_to_files = _build_module_index(files)
        symbol_to_files = _build_symbol_index(files)
        resolved = tuple(
            item.model_copy(
                update={
                    "resolved_imports": _resolve_references(
                        item.imports, item, module_to_files
                    ),
                    "resolved_calls": _resolve_calls(item.calls, symbol_to_files),
                }
            )
            for item in files
        )
        with_test_links = tuple(
            item.model_copy(
                update={
                    "related_tests": tuple(
                        test.path
                        for test in resolved
                        if test.is_test
                        and item.path in (*test.resolved_imports, *test.resolved_calls)
                    )
                }
            )
            for item in resolved
        )
        return RepositoryIndex(
            files=with_test_links,
            skipped_files=tuple(skipped),
            module_to_files=module_to_files,
            symbol_to_files=symbol_to_files,
        )

    def make_repo_map(self, index: RepositoryIndex, task: str) -> RepoMap:
        """根据任务词汇排序候选文件，附带相邻导入文件和相关测试。"""
        tokens, explicit_modules = _task_features(task)
        ranked = sorted(
            ((self._score(item, tokens, explicit_modules), item) for item in index.files),
            key=lambda pair: (-pair[0], pair[1].path),
        )
        primary = [item for score, item in ranked if score > 0 and not item.is_test][
            : self.config.graph_seed_files
        ]
        if not primary:
            primary = [item for _, item in ranked if not item.is_test][
                : self.config.graph_seed_files
            ]
        selected = list(primary)
        neighbors = _graph_neighbor_scores(primary, index.files)
        for item in sorted(
            (item for item in index.files if item.path in neighbors and not item.is_test),
            key=lambda item: (
                -neighbors[item.path],
                -self._score(item, tokens, explicit_modules),
                item.path,
            ),
        )[: self.config.max_graph_neighbors]:
            if item not in selected:
                selected.append(item)
        # 图扩展不足时，再以原始任务排序填满候选额度，保证地图稳定且有界。
        for _, item in ranked:
            if not item.is_test and item not in selected:
                selected.append(item)
            if len(selected) >= self.config.max_candidate_files:
                break
        selected = selected[: self.config.max_candidate_files]
        names = {symbol.name.casefold() for item in selected for symbol in item.symbols}
        graph_tests = [
            item
            for item in index.files
            if item.is_test and {source.path for source in selected}.intersection(
                (*item.resolved_imports, *item.resolved_calls)
            )
        ]
        lexical_tests = [
            item
            for item in index.files
            if item.is_test and (tokens & _file_terms(item) or names & _file_terms(item))
        ]
        tests = tuple(
            {item.path: item for item in (*graph_tests, *lexical_tests)}.values()
        )[:4]
        lines = [
            "[TraceFix Repository Map]",
            "以下为确定性 Python AST/导入索引，可能不完整；修改前仍须读取文件确认。",
            "行动规则：首轮优先读取排名前 2 的源码候选；只有它们不足以解释问题时，"
            "才使用 search_code 做针对具体符号的定向搜索。",
            "任务相关源码文件（按相关性排序）：",
        ]
        for rank, item in enumerate(selected, start=1):
            symbols = (
                ", ".join(
                    f"{symbol.qualified_name}:{symbol.lineno}"
                    for symbol in item.symbols[: self.config.max_symbols_per_file]
                )
                or "（无顶层符号）"
            )
            reason = _candidate_reason(item, tokens, explicit_modules, primary)
            lines.append(f"{rank}. {item.path} | reason: {reason} | symbols: {symbols}")
        if tests:
            lines.append("相关测试候选：")
            lines.extend(f"- {item.path}" for item in tests)
        lines.append(
            "定位后请立即转向最小补丁和相关测试；不要在已读取高排名候选后重新进行宽泛搜索。"
        )
        text = "\n".join(lines)
        if len(text) > self.config.max_chars:
            text = (
                text[: self.config.max_chars - 80] + "\n[Repo Map 已按字符上限截断；请按需搜索。]"
            )
        return RepoMap(
            text=text,
            candidate_files=tuple(item.path for item in selected),
            related_tests=tuple(item.path for item in tests),
            indexed_file_count=len(index.files),
            symbol_count=index.symbol_count,
            skipped_file_count=len(index.skipped_files),
            graph_expanded_file_count=len(set(item.path for item in selected) - {item.path for item in primary}),
        )

    @staticmethod
    def _score(
        item: IndexedFile,
        tokens: set[str],
        explicit_modules: set[str] | None = None,
    ) -> int:
        """按源码先验、精确模块名、路径、符号与关系分层打分。"""
        path_terms = _terms(item.path)
        symbol_terms = _terms(" ".join(symbol.qualified_name for symbol in item.symbols))
        relation_terms = _terms(" ".join((*item.imports, *item.calls)))
        score = sum(
            16 if token in path_terms else 8 if token in symbol_terms else 3
            for token in tokens
            if token in path_terms or token in symbol_terms or token in relation_terms
        )
        if explicit_modules and item.module.casefold() in explicit_modules:
            score += 64
        if any(part.casefold() in _LOW_PRIORITY_DIRS for part in Path(item.path).parts):
            score -= 48
        return score


def _module_name(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _candidate_reason(
    item: IndexedFile,
    tokens: set[str],
    explicit_modules: set[str],
    primary: list[IndexedFile],
) -> str:
    """生成可解释但不泄露标准答案的候选来源标签。"""
    if item.module.casefold() in explicit_modules:
        return "任务文本直接命中模块"
    symbol_names = {
        value
        for symbol in item.symbols
        for value in _terms(f"{symbol.name} {symbol.qualified_name}")
    }
    if tokens.intersection(symbol_names):
        return "任务文本命中符号"
    if tokens.intersection(_terms(item.path)):
        return "任务文本命中路径"
    if item in primary:
        return "任务关键词综合命中"
    return "import/call 图邻居"


def _build_module_index(files: list[IndexedFile]) -> dict[str, tuple[str, ...]]:
    """建立模块名及其常见包根别名到文件路径的反向索引。"""
    values: dict[str, set[str]] = {}
    for item in files:
        parts = item.module.split(".") if item.module else []
        # 保留完整模块名，也允许 src/package/module 与 package/module 两种布局命中。
        for index in range(len(parts)):
            alias = ".".join(parts[index:])
            if alias:
                values.setdefault(alias, set()).add(item.path)
    return {key: tuple(sorted(paths)) for key, paths in sorted(values.items())}


def _build_symbol_index(files: list[IndexedFile]) -> dict[str, tuple[str, ...]]:
    """建立简单符号名和限定名到定义文件的反向索引。"""
    values: dict[str, set[str]] = {}
    for item in files:
        for symbol in item.symbols:
            for name in (symbol.name, symbol.qualified_name):
                values.setdefault(name.casefold(), set()).add(item.path)
    return {key: tuple(sorted(paths)) for key, paths in sorted(values.items())}


def _resolve_references(
    references: tuple[str, ...],
    item: IndexedFile,
    module_to_files: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    """把绝对或相对 import 转为仓库内模块文件；无法确认时宁可不连边。"""
    targets: set[str] = set()
    for reference in references:
        normalized = _normalize_import_reference(reference, item)
        parts = normalized.split(".") if normalized else []
        # from pkg.module import symbol 的最后一段可能是符号而非模块，故从最长前缀回退。
        for length in range(len(parts), 0, -1):
            module = ".".join(parts[:length])
            paths = module_to_files.get(module)
            if paths:
                targets.update(paths)
                break
    return tuple(sorted(targets))


def _normalize_import_reference(reference: str, item: IndexedFile) -> str:
    """根据当前文件模块解析 ``from .x import y`` 的点号层级。"""
    if not reference.startswith("."):
        return reference
    level = len(reference) - len(reference.lstrip("."))
    suffix = reference[level:]
    current = item.module.split(".") if item.module else []
    package = current if item.is_package else current[:-1]
    up = max(0, level - 1)
    base = package[: len(package) - up] if up <= len(package) else []
    return ".".join((*base, *suffix.split("."))) if suffix else ".".join(base)


def _resolve_calls(
    calls: tuple[str, ...], symbol_to_files: dict[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """把调用表达式映射到同名定义文件；属性调用只使用末段作保守候选。"""
    targets: set[str] = set()
    for call in calls:
        for name in (call.casefold(), call.rsplit(".", maxsplit=1)[-1].casefold()):
            targets.update(symbol_to_files.get(name, ()))
    return tuple(sorted(targets))


def _graph_neighbor_scores(seeds: list[IndexedFile], files: tuple[IndexedFile, ...]) -> dict[str, int]:
    """按边语义给一跳邻居加权：import 高于 call，测试关联最低。"""
    seed_paths = {item.path for item in seeds}
    neighbors: dict[str, int] = {}
    for item in seeds:
        for path in item.resolved_imports:
            if path not in seed_paths:
                neighbors[path] = neighbors.get(path, 0) + 12
        for path in item.resolved_calls:
            if path not in seed_paths:
                neighbors[path] = neighbors.get(path, 0) + 6
    for item in files:
        if item.path in seed_paths:
            continue
        if seed_paths.intersection(item.resolved_imports):
            neighbors[item.path] = neighbors.get(item.path, 0) + 8
        if seed_paths.intersection(item.resolved_calls):
            neighbors[item.path] = neighbors.get(item.path, 0) + 4
        if item.is_test and seed_paths.intersection((*item.resolved_imports, *item.resolved_calls)):
            # 测试边只影响 related_tests 的选择，不会挤占源码候选位置。
            neighbors[item.path] = neighbors.get(item.path, 0) + 2
    return neighbors


def _is_test(path: Path) -> bool:
    return (
        "tests" in path.parts
        or "testing" in path.parts
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    )


def _file_terms(item: IndexedFile) -> set[str]:
    return _terms(
        " ".join(
            (
                item.path,
                item.module,
                *(symbol.qualified_name for symbol in item.symbols),
                *item.imports,
                *item.calls,
            )
        )
    )


def _terms(value: str) -> set[str]:
    """以与任务解析相同的规则产生大小写无关的关键词集合。"""
    return {part.casefold() for part in _WORDS.findall(value)}


def _task_features(task: str) -> tuple[set[str], set[str]]:
    """提取去停用词的任务词和显式模块/限定符号，避免叙述词主导排序。"""
    tokens = {
        part.casefold()
        for part in _WORDS.findall(task)
        if len(part) > 2 and part.casefold() not in _STOP_WORDS
    }
    explicit_modules = {value.casefold() for value in _DOTTED_NAME.findall(task)}
    return tokens, explicit_modules
