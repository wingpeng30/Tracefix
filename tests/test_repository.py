"""Repository Indexer 与 Repo Map 的确定性和 Agent 接入测试。"""

# 测试夹具中的 Python 源码以单个字符串表达，格式化器无法安全拆分。
# ruff: noqa: E501

from tracefix import (
    AgentConfig,
    LLMConfig,
    LLMResponse,
    Message,
    MessageRole,
    MinimalAgent,
    RepoMapConfig,
    RepositoryIndexer,
    TokenUsage,
    TraceEventType,
)
from tracefix.models import BaseLLM
from tracefix.repository.indexer import IndexedFile, _graph_neighbor_scores
from tracefix.tracing import TraceEvent


def _make_source(root):
    (root / "src" / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "app" / "parser.py").write_text(
        "from .tokenizer import tokenize\n\nclass Parser:\n    def parse(self, text):\n        return tokenize(text)[0]\n",
        encoding="utf-8",
    )
    (root / "src" / "app" / "tokenizer.py").write_text(
        "def tokenize(text):\n    return text.split(',')\n", encoding="utf-8"
    )
    (root / "tests" / "test_parser.py").write_text(
        "from app.parser import Parser\n\ndef test_empty_parser():\n    assert Parser().parse('') == ''\n",
        encoding="utf-8",
    )
    (root / "broken.py").write_text("def bad(:\n", encoding="utf-8")


def test_indexer_extracts_symbols_imports_and_task_focused_map(tmp_path) -> None:
    _make_source(tmp_path)
    indexer = RepositoryIndexer(tmp_path)
    index = indexer.build()
    parser = next(item for item in index.files if item.path.endswith("parser.py"))
    tokenizer = next(item for item in index.files if item.path.endswith("tokenizer.py"))
    assert {symbol.qualified_name for symbol in parser.symbols} == {"Parser", "Parser.parse"}
    assert "tokenizer" in " ".join(parser.imports)
    assert parser.resolved_imports == ("src/app/tokenizer.py",)
    assert parser.related_tests == ("tests/test_parser.py",)
    assert tokenizer.path in index.module_to_files["app.tokenizer"]
    assert parser.path in index.symbol_to_files["parser"]
    assert "broken.py" in index.skipped_files

    repo_map = indexer.make_repo_map(index, "Parser.parse fails on empty token input")
    assert "src/app/parser.py" in repo_map.candidate_files
    assert "Parser.parse" in repo_map.text
    assert "tests/test_parser.py" in repo_map.related_tests
    assert repo_map == indexer.make_repo_map(index, "Parser.parse fails on empty token input")


def test_indexer_expands_import_and_call_graph_neighbors(tmp_path) -> None:
    """初始命中的入口文件应能带出相对导入和调用到的实现文件。"""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "entry.py").write_text(
        "from .worker import worker\n\ndef entry():\n    return worker()\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "worker.py").write_text(
        "def worker():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "noise.py").write_text("def unrelated(): return 0\n", encoding="utf-8")

    indexer = RepositoryIndexer(
        tmp_path,
        RepoMapConfig(max_candidate_files=2, graph_seed_files=1, max_graph_neighbors=1),
    )
    index = indexer.build()
    repo_map = indexer.make_repo_map(index, "entry failure")

    entry = next(item for item in index.files if item.path == "pkg/entry.py")
    assert entry.resolved_imports == ("pkg/worker.py",)
    assert entry.resolved_calls == ("pkg/worker.py",)
    assert repo_map.candidate_files == ("pkg/entry.py", "pkg/worker.py")
    assert repo_map.graph_expanded_file_count == 1


def test_seed_ranking_prefers_explicit_module_over_documentation(tmp_path) -> None:
    """Issue 中出现完整模块名时，包入口应优先于同词文档与示例。"""
    (tmp_path / "pkg" / "autodoc").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "pkg" / "autodoc" / "__init__.py").write_text(
        "def document(): return 1\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "autodoc.py").write_text(
        "def document(): return 0\n", encoding="utf-8"
    )

    repo_map = RepositoryIndexer(tmp_path).make_repo_map(
        RepositoryIndexer(tmp_path).build(), "Failure in pkg.autodoc documenter"
    )

    assert repo_map.candidate_files[0] == "pkg/autodoc/__init__.py"


def test_graph_neighbor_weights_distinguish_import_call_and_test_edges() -> None:
    """不同关系不会被抹平成同一种图边，保证排序可解释。"""
    seed = IndexedFile(
        path="pkg/seed.py",
        module="pkg.seed",
        resolved_imports=("pkg/imported.py",),
        resolved_calls=("pkg/called.py",),
    )
    imported = IndexedFile(path="pkg/imported.py", module="pkg.imported")
    called = IndexedFile(path="pkg/called.py", module="pkg.called")
    test_file = IndexedFile(
        path="tests/test_seed.py",
        module="test_seed",
        is_test=True,
        resolved_imports=("pkg/seed.py",),
    )

    scores = _graph_neighbor_scores([seed], (seed, imported, called, test_file))

    assert scores["pkg/imported.py"] == 12
    assert scores["pkg/called.py"] == 6
    assert scores["tests/test_seed.py"] == 10


def test_indexer_respects_character_limit_and_skips_virtual_environment(tmp_path) -> None:
    _make_source(tmp_path)
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("def ignored(): pass\n", encoding="utf-8")
    indexer = RepositoryIndexer(tmp_path, config=RepoMapConfig(max_chars=512))
    index = indexer.build()
    assert all(".venv" not in item.path for item in index.files)
    assert len(indexer.make_repo_map(index, "parser tokenizer").text) <= 512


class _FinalLLM(BaseLLM):
    """捕获模型视图，验证 Repo Map 作为请求上下文而不是隐藏状态。"""

    def __init__(self) -> None:
        super().__init__(LLMConfig(model_name="test/model"))
        self.messages = ()

    def complete(self, messages, tools=()):
        self.messages = messages
        return LLMResponse(
            message=Message(role=MessageRole.ASSISTANT, content="done"),
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2, cost_usd=0),
            model_name="test/model",
            finish_reason="stop",
        )


class _MemorySink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def write(self, event: TraceEvent) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


def test_agent_injects_repo_map_into_model_visible_system_context() -> None:
    llm = _FinalLLM()
    sink = _MemorySink()
    agent = MinimalAgent(
        llm,
        config=AgentConfig(max_steps=1),
        trace_sink=sink,
        repository_map="[TraceFix Repository Map]\n- src/app/parser.py | symbols: Parser.parse:4",
    )
    agent.run("Fix parser")
    assert any(message.metadata.get("kind") == "repository_map" for message in llm.messages)
    assert TraceEventType.REPO_MAP_ADDED in {event.event_type for event in sink.events}
