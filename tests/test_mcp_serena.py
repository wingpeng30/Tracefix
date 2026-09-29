"""Security and lifecycle checks for the optional Serena MCP tool adapter."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix import RunConfig, TraceFixRunner
from tracefix.mcp_serena import SerenaMCP, _content_text
from tracefix.messages import ToolCall
from tracefix.tools.base import ToolRegistry


def _repo(root: Path) -> Path:
    root.mkdir()
    (root / "widget.py").write_text("def next_page():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "widget.py"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "fixture"], cwd=root, check=True,
    )
    return root


def _result(value: str, *, error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        isError=error,
        content=[SimpleNamespace(type="text", text=value)],
    )


def test_serena_tool_uses_fresh_source_snapshot_and_pairs_call_ids(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "source")
    manager = SerenaMCP(repo, tmp_path, "sha256:" + "a" * 64, max_output_chars=20)
    queried: list[tuple[str, str]] = []

    async def fake_call(name, snapshot, method, arguments):
        assert name.startswith("tracefix-mcp-")
        assert method == "get_symbols_overview"
        assert arguments == {"relative_path": "widget.py", "max_answer_chars": 40}
        queried.append((name, (snapshot / "widget.py").read_text(encoding="utf-8")))
        return _result("next_page " * 10)

    monkeypatch.setattr(manager, "_call_async", fake_call)
    monkeypatch.setattr(
        manager, "_remove_container", lambda name: manager._active.discard(name) or True,
    )
    registry = ToolRegistry(manager.tools())
    assert len(registry) == 3
    first = registry.get("mcp_serena_symbols").execute(ToolCall(
        id="call-1", name="mcp_serena_symbols", arguments={"relative_path": "widget.py"},
    ))
    assert first.success and first.call_id == "call-1"
    assert first.output["text"].endswith("[TraceFix MCP output truncated]")
    previous = first.output["source_sha256"]
    (repo / "widget.py").write_text("def next_page():\n    return 2\n", encoding="utf-8")
    second = registry.get("mcp_serena_symbols").execute(ToolCall(
        id="call-2", name="mcp_serena_symbols", arguments={"relative_path": "widget.py"},
    ))
    assert second.success and second.output["source_sha256"] != previous
    assert "return 1" in queried[0][1] and "return 2" in queried[1][1]
    assert not list(tmp_path.glob("mcp-source-*"))
    assert not manager._active


@pytest.mark.parametrize("arguments", [
    {"relative_path": "../secret.py"},
    {"relative_path": "C:\\secret.py"},
    {"relative_path": 3},
    {"relative_path": "widget.py", "execute_shell_command": "id"},
    {},
])
def test_serena_rejects_invalid_arguments_before_starting_container(tmp_path, arguments):
    manager = SerenaMCP(tmp_path, tmp_path, "sha256:" + "a" * 64)
    result = manager.tools()[0].execute(ToolCall(
        id="invalid", name="mcp_serena_symbols", arguments=arguments,
    ))
    assert not result.success
    assert result.call_id == "invalid"
    assert not manager._active


def test_serena_service_error_and_cleanup_failure_are_failures(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "source")
    manager = SerenaMCP(repo, tmp_path, "sha256:" + "a" * 64)

    async def rejected(*_args):
        return _result("service unavailable", error=True)

    monkeypatch.setattr(manager, "_call_async", rejected)
    monkeypatch.setattr(
        manager, "_remove_container", lambda name: manager._active.discard(name) or True,
    )
    tool = manager.tools()[0]
    rejected_result = tool.execute(ToolCall(
        id="rejected", name=tool.spec.name, arguments={"relative_path": "widget.py"},
    ))
    assert not rejected_result.success and "service unavailable" in rejected_result.error

    monkeypatch.setattr(manager, "_remove_container", lambda _name: False)
    failed_cleanup = tool.execute(ToolCall(
        id="cleanup", name=tool.spec.name, arguments={"relative_path": "widget.py"},
    ))
    assert not failed_cleanup.success and "cleanup failed" in failed_cleanup.error
    assert manager._active


def test_serena_snapshot_rejects_symlink_and_image_identity(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "source")
    manager = SerenaMCP(repo, tmp_path, "mutable-tag")
    with pytest.raises(ValueError, match="immutable"):
        manager.preflight()
    link = repo / "link.py"
    link.write_text("SECRET = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "link.py"], cwd=repo, check=True)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == link or original(path))
    with pytest.raises(ValueError, match="symlink"):
        manager._snapshot(tmp_path / "snapshot")


def test_serena_tool_definitions_are_stable_and_noncolliding(tmp_path):
    manager = SerenaMCP(tmp_path, tmp_path, "sha256:" + "a" * 64)
    names = [tool.spec.name for tool in manager.tools()]
    assert names == [
        "mcp_serena_symbols", "mcp_serena_find_symbol", "mcp_serena_references"
    ]
    assert all("execute_shell_command" not in name for name in names)
    assert len(set(names)) == 3


def test_runner_mcp_preflight_stops_before_model_construction(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "source")
    constructed = []

    def denied(_self):
        raise RuntimeError("isolated image unavailable")

    monkeypatch.setattr(SerenaMCP, "preflight", denied)
    result = TraceFixRunner(lambda config: constructed.append(config)).run(RunConfig(
        repo=repo, task="inspect next_page", model_name="offline/replay",
        output_dir=tmp_path / "runs", env_file=None,
        mcp_serena_image_id="sha256:" + "a" * 64,
    ))
    assert result.status.value == "failed"
    assert "isolated image unavailable" in str(result.error)
    assert not constructed


def test_serena_preflight_checks_sdk_cli_and_exact_image(tmp_path, monkeypatch):
    manager = SerenaMCP(tmp_path, tmp_path, "sha256:" + "a" * 64)
    monkeypatch.setitem(sys.modules, "mcp", SimpleNamespace(ClientSession=object))
    monkeypatch.setattr("tracefix.mcp_serena.shutil.which", lambda name: None)
    with pytest.raises(RuntimeError, match="Docker CLI"):
        manager.preflight()
    monkeypatch.setattr("tracefix.mcp_serena.shutil.which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        "tracefix.mcp_serena.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="sha256:" + "b" * 64),
    )
    with pytest.raises(RuntimeError, match="changed"):
        manager.preflight()
    monkeypatch.setattr(
        "tracefix.mcp_serena.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=manager.image_id),
    )
    manager.preflight()


def test_serena_cleanup_is_bounded_and_reports_unremoved_container(tmp_path, monkeypatch):
    manager = SerenaMCP(tmp_path, tmp_path, "sha256:" + "a" * 64)
    manager._active.add("tracefix-mcp-test")
    monkeypatch.setattr(
        "tracefix.mcp_serena.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr=b"daemon offline"),
    )
    with pytest.raises(RuntimeError, match="cleanup failed"):
        manager.close()
    assert manager._active
    monkeypatch.setattr(
        "tracefix.mcp_serena.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stderr=b"No such container: tracefix-mcp-test"
        ),
    )
    manager.close()
    assert not manager._active
    manager._active.add("tracefix-mcp-second")

    def timed_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("docker", 10)

    monkeypatch.setattr("tracefix.mcp_serena.subprocess.run", timed_out)
    assert not manager._remove_container("tracefix-mcp-second")


def test_serena_snapshot_bounds_and_text_blocks(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "source")
    manager = SerenaMCP(repo, tmp_path, "sha256:" + "a" * 64)
    monkeypatch.setattr(
        "tracefix.mcp_serena.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=b"../outside.py\0"),
    )
    with pytest.raises(ValueError, match="escaped"):
        manager._snapshot(tmp_path / "snapshot")
    monkeypatch.setattr(
        "tracefix.mcp_serena.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=b"\0".join(f"file-{n}.py".encode() for n in range(5001)) + b"\0"
        ),
    )
    with pytest.raises(ValueError, match="5,000"):
        manager._snapshot(tmp_path / "snapshot")
    assert _content_text(SimpleNamespace(content=[
        SimpleNamespace(type="image"), SimpleNamespace(type="text", text="symbol")
    ])) == "symbol"


def test_serena_official_session_handshake_and_whitelist(tmp_path, monkeypatch):
    manager = SerenaMCP(tmp_path, tmp_path, "sha256:" + "a" * 64, timeout_seconds=2)
    stages = []

    class Session:
        async def __aenter__(self):
            stages.append("session_open")
            return self

        async def __aexit__(self, *_args):
            stages.append("session_close")

        async def initialize(self):
            stages.append("initialize")

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="get_symbols_overview")])

        async def call_tool(self, method, arguments):
            stages.append((method, arguments))
            return _result("next_page")

    @asynccontextmanager
    async def stdio(_params):
        stages.append("stdio_open")
        yield object(), object()
        stages.append("stdio_close")

    monkeypatch.setitem(sys.modules, "mcp", SimpleNamespace(
        ClientSession=lambda *_args: Session(),
        StdioServerParameters=lambda **kwargs: SimpleNamespace(**kwargs),
    ))
    monkeypatch.setitem(sys.modules, "mcp.client", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", SimpleNamespace(stdio_client=stdio))
    response = asyncio.run(manager._call_async(
        "tracefix-mcp-test", tmp_path, "get_symbols_overview", {"relative_path": "widget.py"}
    ))
    assert _content_text(response) == "next_page"
    assert stages == [
        "stdio_open", "session_open", "initialize",
        ("get_symbols_overview", {"relative_path": "widget.py"}),
        "session_close", "stdio_close",
    ]
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(manager._call_async("tracefix-mcp-test", tmp_path, "write_memory", {}))


def test_serena_missing_sdk_and_deleted_tracked_file(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "source")
    manager = SerenaMCP(repo, tmp_path, "sha256:" + "a" * 64)
    monkeypatch.setitem(sys.modules, "mcp", None)
    with pytest.raises(RuntimeError, match="tracefix-agent\\[mcp\\]"):
        manager.preflight()
    (repo / "widget.py").unlink()
    target = tmp_path / "snapshot"
    target.mkdir()
    assert manager._snapshot(target) == hashlib.sha256().hexdigest()


def test_serena_rejects_tool_name_and_long_or_windows_path(tmp_path):
    manager = SerenaMCP(tmp_path, tmp_path, "sha256:" + "a" * 64)
    tool = manager.tools()[0]
    for call in (
        ToolCall(id="wrong", name="write_memory", arguments={"relative_path": "widget.py"}),
        ToolCall(id="long", name=tool.spec.name, arguments={"relative_path": "a" * 501}),
        ToolCall(id="path", name=tool.spec.name, arguments={"relative_path": "foo\\bar.py"}),
    ):
        result = tool.execute(call)
        assert not result.success and result.call_id == call.id
