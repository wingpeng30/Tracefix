from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from scripts.inspect_docker_run import (
    frozen_files_match,
    persisted_tool_call_ids,
    unresolved_tool_calls,
)
from tracefix.docker_backend import (
    _IMAGES,
    DockerToolBackend,
    _BridgeSession,
    _RemoteTool,
    _run,
    _validate_synthetic_profile,
)
from tracefix.exceptions import RunConfigurationError, ToolValidationError, WorkspaceError
from tracefix.messages import ToolCall
from tracefix.tools.base import ToolResult, ToolSpec


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_source_archive_exposes_only_requested_commit(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "base.py")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@invalid", "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "later")
    (repo / "future-answer.py").write_text("ANSWER = True\n", encoding="utf-8")
    _git(repo, "add", "future-answer.py")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@invalid", "commit", "-qm", "later")
    backend = object.__new__(DockerToolBackend)
    backend.run_dir = tmp_path
    archive = backend._source_archive(repo, base)
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert "base.py" in names
    assert "future-answer.py" not in names
    assert all(".git" not in name.split("/") for name in names)


def test_synthetic_profile_requires_fixed_task_and_full_image_id(tmp_path: Path) -> None:
    image = "sha256:" + "a" * 64
    backend = DockerToolBackend(
        task_id="tracefix-synthetic",
        input_root=tmp_path,
        run_dir=tmp_path,
        run_id="smoke",
        profile="synthetic",
        image_id=image,
    )
    assert backend.profile == "synthetic"
    assert backend.requested_image_id == image
    with pytest.raises(RunConfigurationError, match="fixed task ID and image ID"):
        DockerToolBackend(
            task_id="psf__requests-1766",
            input_root=tmp_path,
            run_dir=tmp_path,
            run_id="bad-profile",
            profile="synthetic",
            image_id=image,
        )
    with pytest.raises(RunConfigurationError, match="fixed task ID and image ID"):
        DockerToolBackend(
            task_id="tracefix-synthetic",
            input_root=tmp_path,
            run_dir=tmp_path,
            run_id="short-image",
            profile="synthetic",
            image_id="sha256:abcd",
        )


def test_synthetic_profile_accepts_only_the_fixed_recipe_shape() -> None:
    manifest = {"files": {"source.bundle": "a" * 64}}
    recipe = {"task_id": "tracefix-synthetic", "environment_variables": {}}
    _validate_synthetic_profile("tracefix-synthetic", recipe, manifest)
    with pytest.raises(WorkspaceError, match="fixed smoke profile"):
        _validate_synthetic_profile(
            "tracefix-synthetic",
            {**recipe, "build_commands": ["arbitrary command"]},
            manifest,
        )
    with pytest.raises(WorkspaceError, match="fixed smoke profile"):
        _validate_synthetic_profile(
            "tracefix-synthetic",
            {**recipe, "environment_variables": {"PYTHONPATH": "/host"}},
            manifest,
        )
def test_bridge_correlates_events_and_tool_results(tmp_path: Path) -> None:
    code = """
import json, sys
print(json.dumps({'type':'hello','protocol':1,'run_id':'run-1','tools':[]}), flush=True)
for line in sys.stdin:
    call = json.loads(line)
    if call['name'] == 'run_tests':
        print(json.dumps({'type':'process_started','call_id':call['id']}), flush=True)
    result = {'call_id':call['id'],'tool_name':call['name'],'success':True,
              'output':{'done':True},'error':None,'metadata':{},
              'timestamp':'2026-09-26T00:00:00Z','duration_ms':0}
    print(json.dumps({'type':'result','call_id':call['id'],'result':result}), flush=True)
"""
    journal = tmp_path / "events.jsonl"
    session = _BridgeSession([sys.executable, "-u", "-c", code], timeout=2, journal_path=journal)
    starts: list[bool] = []
    try:
        result = session.call(
            "run_tests", {"command": "pytest"}, lambda: starts.append(True), call_id="agent-call-7"
        )
    finally:
        session.close()
    assert starts == [True]
    assert result.call_id == "agent-call-7"
    assert result.output == {"done": True}
    events = [__import__("json").loads(line) for line in journal.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "handshake",
        "dispatch_started",
        "request_sent",
        "process_started",
        "result_received",
    ]
    assert all(
        event["call_id"] == "agent-call-7" for event in events if event["event"] != "handshake"
    )


def test_bridge_rejects_identity_mismatch(tmp_path: Path) -> None:
    code = """
import json, sys
print(json.dumps({'type':'hello','protocol':1,'run_id':'run-1','tools':[]}), flush=True)
for line in sys.stdin:
    call=json.loads(line)
    result={'call_id':'other','tool_name':call['name'],'success':True,'output':None,
            'error':None,'metadata':{},'timestamp':'2026-09-26T00:00:00Z','duration_ms':0}
    print(json.dumps({'type':'result','call_id':'other','result':result}), flush=True)
"""
    journal = tmp_path / "mismatch-events.jsonl"
    session = _BridgeSession([sys.executable, "-u", "-c", code], timeout=2, journal_path=journal)
    try:
        with pytest.raises(WorkspaceError, match="identity mismatch"):
            session.call("read_file", {}, call_id="expected")
    finally:
        session.close(force=True)
    events = [__import__("json").loads(line) for line in journal.read_text().splitlines()]
    assert events[-1]["event"] == "outcome_unknown"
    assert events[-1]["call_id"] == "expected"


def test_pytest_rpc_deadline_exceeds_remote_test_timeout(tmp_path: Path) -> None:
    code = """
import json, sys, time
print(json.dumps({'type':'hello','protocol':1,'run_id':'run-1','tools':[]}), flush=True)
for line in sys.stdin:
    call = json.loads(line)
    time.sleep(0.3)
    result = {'call_id':call['id'],'tool_name':call['name'],'success':True,
              'output':{'done':True},'error':None,'metadata':{},
              'timestamp':'2026-09-26T00:00:00Z','duration_ms':0}
    print(json.dumps({'type':'result','call_id':call['id'],'result':result}), flush=True)
"""
    session = _BridgeSession(
        [sys.executable, "-u", "-c", code], timeout=0.2, journal_path=tmp_path / "events.jsonl"
    )
    try:
        result = session.call("run_tests", {"timeout_seconds": 0.5}, call_id="slow-test")
    finally:
        session.close()
    assert result.success


def test_recovery_classifies_lost_results_without_replaying_calls() -> None:
    events = [
        {"call_id": "done", "event": "request_sent"},
        {"call_id": "done", "event": "result_received"},
        {"call_id": "lost", "event": "request_sent"},
        {"call_id": "lost", "event": "process_started"},
        {"call_id": "not-sent", "event": "dispatch_started"},
    ]
    assert unresolved_tool_calls(events, {"done"}) == {
        "completed": ["done"],
        "unknown": ["lost", "not-sent"],
        "result_received_not_persisted": [],
    }


def test_recovery_marks_journal_result_without_trajectory_as_unknown() -> None:
    journal = [{"call_id": "received", "event": "result_received"}]
    assert unresolved_tool_calls(journal, set()) == {
        "completed": [],
        "unknown": ["received"],
        "result_received_not_persisted": ["received"],
    }
    trajectory = [{
        "event_type": "tool_returned",
        "payload": {"result": {"call_id": "received"}},
    }]
    assert persisted_tool_call_ids(trajectory) == {"received"}
    assert unresolved_tool_calls(journal, persisted_tool_call_ids(trajectory))["completed"] == [
        "received"
    ]


def test_recovery_rechecks_frozen_input_file_hashes(tmp_path: Path) -> None:
    import hashlib

    payload = tmp_path / "input.json"
    payload.write_text('{"fixed": true}\n', encoding="utf-8")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    assert frozen_files_match(tmp_path, {"input.json": digest})
    assert not frozen_files_match(tmp_path, {"input.json": "0" * 64})
    assert not frozen_files_match(tmp_path, {"../outside": digest})


def test_bridge_rpc_timeout_is_recorded_unknown_and_never_fabricates_result(
    tmp_path: Path,
) -> None:
    code = """
import json, sys
print(json.dumps({'type':'hello','protocol':1,'run_id':'timeout-run','tools':[]}), flush=True)
for _line in sys.stdin:
    pass
"""
    journal = tmp_path / "timeout-events.jsonl"
    session = _BridgeSession(
        [sys.executable, "-u", "-c", code], timeout=3, journal_path=journal
    )
    session.timeout = 0.02
    try:
        with pytest.raises(TimeoutError, match="timed out"):
            session.call("apply_patch", {"patch": "diff --git"}, call_id="uncertain-patch")
    finally:
        session.close(force=True)
    events = [__import__("json").loads(line) for line in journal.read_text().splitlines()]
    assert [event["event"] for event in events[-2:]] == ["request_sent", "outcome_unknown"]
    assert events[-1]["call_id"] == "uncertain-patch"
    assert not any(event["event"] == "result_received" for event in events)


def test_bridge_rejects_wrong_handshake_identity(tmp_path: Path) -> None:
    code = "print('{\"type\":\"hello\",\"protocol\":2,\"tools\":[]}', flush=True)"
    with pytest.raises(WorkspaceError, match="protocol identity mismatch"):
        _BridgeSession([sys.executable, "-u", "-c", code], 1, tmp_path / "bad-hello.jsonl")


def test_bridge_transfers_path_free_skill_catalog_and_rejects_mismatch(tmp_path: Path) -> None:
    import json

    from tracefix.tools import SkillCatalogEntry, ToolRegistry

    digest = "a" * 64
    catalog = [{"name": "demo", "description": "A demo skill", "version": "1", "sha256": digest}]
    spec = {
        "name": "load_skill",
        "description": "Load skill",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": ["demo"]}},
            "required": ["name"],
            "additionalProperties": False,
        },
    }
    payload = {"type": "hello", "protocol": 1, "run_id": "skills", "tools": [spec],
               "skill_catalog": catalog}
    code = f"print({json.dumps(payload)!r}, flush=True)"
    session = _BridgeSession(
        [sys.executable, "-u", "-c", code], 2, tmp_path / "skill-catalog.jsonl",
        skills_enabled=True,
    )
    try:
        remote = _RemoteTool(
            session.tools[0],
            type("Backend", (), {"session": session})(),  # type: ignore[arg-type]
            session.skill_catalog,
        )
        registry = ToolRegistry([remote])
        assert registry.skill_catalog == (SkillCatalogEntry.model_validate(catalog[0]),)
        assert not hasattr(registry.skill_catalog[0], "path")
    finally:
        session.close()

    mismatch = {**payload, "tools": [{**spec, "input_schema": {"type": "object"}}]}
    bad_code = f"print({json.dumps(mismatch)!r}, flush=True)"
    with pytest.raises(WorkspaceError, match="does not match"):
        _BridgeSession(
            [sys.executable, "-u", "-c", bad_code], 2,
            tmp_path / "skill-catalog-mismatch.jsonl", skills_enabled=True,
        )


def test_bridge_startup_timeout_kills_unresponsive_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child that never completes its handshake must not be left running."""
    import tracefix.docker_backend as docker_module

    created: list[subprocess.Popen[bytes]] = []
    original_popen = subprocess.Popen

    def track_child(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        process = original_popen(*args, **kwargs)
        created.append(process)
        return process

    monkeypatch.setattr(docker_module.subprocess, "Popen", track_child)
    code = "import time; time.sleep(10)"
    with pytest.raises(TimeoutError, match="response timed out"):
        _BridgeSession(
            [sys.executable, "-u", "-c", code],
            timeout=0.05,
            journal_path=tmp_path / "startup-timeout.jsonl",
        )

    assert len(created) == 1
    assert created[0].poll() is not None


def test_bridge_close_kills_child_after_graceful_shutdown_timeout() -> None:
    """A bridge that ignores stdin EOF must be killed after the close grace period."""
    events: list[str] = []

    class FakePipe:
        def close(self) -> None:
            events.append("stdin_closed")

    class HangingProcess:
        stdin = FakePipe()
        killed = False

        def poll(self):
            return -9 if self.killed else None

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")
            if not self.killed:
                raise subprocess.TimeoutExpired("bridge", timeout)
            return -9

        def kill(self):
            events.append("killed")
            self.killed = True

    session = object.__new__(_BridgeSession)
    session._closed = False
    session.process = HangingProcess()

    session.close()
    session.close()

    assert events == ["stdin_closed", "wait:10", "killed", "wait:None"]


def test_remote_test_tool_rejects_failed_container_preparation() -> None:
    """The Agent must not start a test when container-side preparation rejected it."""

    class FakeSession:
        def call(self, name: str, arguments: dict[str, object]) -> ToolResult:
            assert name == "__prepare__"
            assert arguments["name"] == "run_tests"
            return ToolResult(
                call_id="prepare-rejected",
                tool_name=name,
                success=False,
                error="invalid pytest selection",
            )

    class FakeBackend:
        session = FakeSession()

    tool = _RemoteTool(
        ToolSpec(name="run_tests", description="Run focused tests"),
        FakeBackend(),  # type: ignore[arg-type]
    )
    call = ToolCall(
        id="rejected-test", name="run_tests", arguments={"command": "pytest -q"}
    )

    with pytest.raises(ToolValidationError, match="invalid pytest selection"):
        tool.prepare(call)


def test_bridge_reports_child_exit_before_handshake(tmp_path: Path) -> None:
    """A bridge process that exits before hello must surface a startup failure."""
    code = (
        "import sys,time; sys.stderr.write('bridge startup failed'); "
        "sys.stderr.flush(); time.sleep(0.05)"
    )
    with pytest.raises(WorkspaceError, match="Docker tool bridge stopped"):
        _BridgeSession(
            [sys.executable, "-u", "-c", code],
            timeout=2,
            journal_path=tmp_path / "early-exit.jsonl",
        )


def test_bridge_rejects_process_started_event_for_another_call(tmp_path: Path) -> None:
    """A process-started frame for another request must not transfer its quota."""
    import json

    code = """
import json, sys
print(json.dumps({'type':'hello','protocol':1,'run_id':'event-run','tools':[]}), flush=True)
for _line in sys.stdin:
    print(json.dumps({'type':'process_started','call_id':'other-call'}), flush=True)
"""
    journal = tmp_path / "wrong-process-event.jsonl"
    session = _BridgeSession([sys.executable, "-u", "-c", code], 2, journal)
    try:
        with pytest.raises(WorkspaceError, match="event call ID mismatch"):
            session.call("run_tests", {"command": "pytest -q"}, call_id="expected-call")
    finally:
        session.close(force=True)

    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "outcome_unknown"
    assert events[-1]["call_id"] == "expected-call"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ("not-json", "invalid Docker tool bridge response"),
        (
            '{"type":"result","call_id":"bad-result","result":{"call_id":"bad-result",'
            '"tool_name":"read_file","success":false,"output":null,"error":null,'
            '"metadata":{},"timestamp":"2026-09-26T00:00:00Z","duration_ms":0}}',
            "invalid tool result from Docker bridge",
        ),
        (
            '{"type":"result","call_id":"bad-result","result":{"call_id":"bad-result",'
            '"tool_name":"other_tool","success":true,"output":null,"error":null,'
            '"metadata":{},"timestamp":"2026-09-26T00:00:00Z","duration_ms":0}}',
            "does not match request",
        ),
    ],
)
def test_bridge_malformed_tool_results_remain_unknown(
    tmp_path: Path, response: str, message: str
) -> None:
    """An unparseable response must fail closed without journaled success."""
    import json

    code = f"""
import json, sys
print(json.dumps({{'type':'hello','protocol':1,'run_id':'malformed-run','tools':[]}}), flush=True)
for _line in sys.stdin:
    print({response!r}, flush=True)
"""
    journal = tmp_path / "malformed-response.jsonl"
    session = _BridgeSession([sys.executable, "-u", "-c", code], 2, journal)
    try:
        with pytest.raises(WorkspaceError, match=message):
            session.call("read_file", {"path": "source.py"}, call_id="bad-result")
    finally:
        session.close(force=True)

    events = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "outcome_unknown"
    assert events[-1]["call_id"] == "bad-result"
    assert not any(event["event"] == "result_received" for event in events)


@pytest.mark.parametrize("returncode,stderr", [(17, b"denied"), (0, b"")])
def test_docker_command_failure_is_reported_without_shell(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stderr: bytes,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["shell"] is False
        if returncode == 0:
            raise FileNotFoundError("docker executable missing")
        return subprocess.CompletedProcess(args[0], returncode, b"", stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(WorkspaceError, match="Docker backend command failed"):
        _run(["docker", "inspect", "id"])


@pytest.mark.parametrize("recorded_label,expected_removals", [("other-run", 0), ("owned-run", 1)])
def test_close_removes_only_container_with_matching_run_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recorded_label: str,
    expected_removals: int,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        return subprocess.CompletedProcess(
            command, 0, recorded_label.encode("utf-8"), b""
        )

    monkeypatch.setattr("tracefix.docker_backend._run", fake_run)
    backend = DockerToolBackend(
        task_id="pytest-dev__pytest-10081",
        input_root=tmp_path,
        run_dir=tmp_path,
        run_id="owned-run",
    )
    backend.container_id = "container-id"
    backend.close(remove=True)
    removals = [command for command in commands if command[1:3] == ["rm", "-f"]]
    assert len(removals) == expected_removals


def test_prepare_rejects_manifest_task_mismatch_before_docker_access(tmp_path: Path) -> None:
    stage = tmp_path / "inputs" / "pytest-dev__pytest-10081"
    (stage / "recipes").mkdir(parents=True)
    (stage / "source.bundle").write_bytes(b"unused for identity rejection")
    (stage / "recipes" / "pytest-dev__pytest-10081.json").write_text("{}", encoding="utf-8")
    (stage / "input-manifest.json").write_text(
        '{"task_id":"other-task","source_commit":"frozen-commit","files":{}}',
        encoding="utf-8",
    )
    backend = DockerToolBackend(
        task_id="pytest-dev__pytest-10081",
        input_root=tmp_path / "inputs",
        run_dir=tmp_path / "run",
        run_id="identity-mismatch",
    )
    with pytest.raises(WorkspaceError, match="input identity mismatch"):
        backend.prepare("frozen-commit", tmp_path, tmp_path)


def test_prepare_rejects_missing_frozen_manifest_before_docker_access(tmp_path: Path) -> None:
    stage = tmp_path / "inputs" / "pytest-dev__pytest-10081"
    stage.mkdir(parents=True)
    backend = DockerToolBackend(
        task_id="pytest-dev__pytest-10081",
        input_root=tmp_path / "inputs",
        run_dir=tmp_path / "run",
        run_id="missing-manifest",
    )
    with pytest.raises(WorkspaceError, match="frozen Docker task input is incomplete"):
        backend.prepare("frozen-commit", tmp_path, tmp_path)


def test_prepare_rejects_frozen_manifest_source_commit_mismatch(tmp_path: Path) -> None:
    stage = tmp_path / "inputs" / "pytest-dev__pytest-10081"
    (stage / "recipes").mkdir(parents=True)
    (stage / "source.bundle").write_bytes(b"bundle sentinel")
    (stage / "recipes" / "pytest-dev__pytest-10081.json").write_text("{}", encoding="utf-8")
    (stage / "input-manifest.json").write_text(
        '{"task_id":"pytest-dev__pytest-10081","source_commit":"wrong-commit","files":{}}',
        encoding="utf-8",
    )
    backend = DockerToolBackend(
        task_id="pytest-dev__pytest-10081",
        input_root=tmp_path / "inputs",
        run_dir=tmp_path / "run",
        run_id="source-mismatch",
    )
    with pytest.raises(WorkspaceError, match="input identity mismatch"):
        backend.prepare("frozen-commit", tmp_path, tmp_path)


def test_prepare_rejects_frozen_manifest_path_escape(tmp_path: Path) -> None:
    import hashlib

    input_root = tmp_path / "inputs"
    stage = input_root / "pytest-dev__pytest-10081"
    (stage / "recipes").mkdir(parents=True)
    (stage / "source.bundle").write_bytes(b"bundle sentinel")
    (stage / "recipes" / "pytest-dev__pytest-10081.json").write_text("{}", encoding="utf-8")
    outside = input_root / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    (stage / "input-manifest.json").write_text(
        '{"task_id":"pytest-dev__pytest-10081","source_commit":"frozen-commit",'
        f'"files":{{"../outside.json":"{digest}"}}}}',
        encoding="utf-8",
    )
    backend = DockerToolBackend(
        task_id="pytest-dev__pytest-10081",
        input_root=input_root,
        run_dir=tmp_path / "run",
        run_id="path-escape",
    )
    with pytest.raises(WorkspaceError, match="path escaped its root"):
        backend.prepare("frozen-commit", tmp_path, tmp_path)


def test_prepare_rejects_frozen_input_digest_mismatch(tmp_path: Path) -> None:
    stage = tmp_path / "inputs" / "pytest-dev__pytest-10081"
    (stage / "recipes").mkdir(parents=True)
    (stage / "source.bundle").write_bytes(b"unused for hash rejection")
    (stage / "recipes" / "pytest-dev__pytest-10081.json").write_text("{}", encoding="utf-8")
    (stage / "input-manifest.json").write_text(
        '{"task_id":"pytest-dev__pytest-10081","source_commit":"frozen-commit",'
        '"files":{"task.json":"bad-hash"}}',
        encoding="utf-8",
    )
    (stage / "task.json").write_text("{}", encoding="utf-8")
    backend = DockerToolBackend(
        task_id="pytest-dev__pytest-10081",
        input_root=tmp_path / "inputs",
        run_dir=tmp_path / "run",
        run_id="digest-mismatch",
    )
    with pytest.raises(WorkspaceError, match="frozen Docker input hash mismatch"):
        backend.prepare("frozen-commit", tmp_path, tmp_path)


def test_prepare_rejects_wrong_image_before_creating_a_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "module.py")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@invalid", "commit", "-qm", "base")
    commit = _git(repo, "rev-parse", "HEAD")
    stage = tmp_path / "inputs" / "pytest-dev__pytest-10081"
    (stage / "recipes").mkdir(parents=True)
    (stage / "source.bundle").write_bytes(b"bundle sentinel")
    (stage / "recipes" / "pytest-dev__pytest-10081.json").write_text("{}", encoding="utf-8")
    (stage / "input-manifest.json").write_text(
        f'{{"task_id":"pytest-dev__pytest-10081","source_commit":"{commit}","files":{{}}}}',
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    real_run = _run
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, b"sha256:wrong-image", b"")
        return real_run(command, **kwargs)

    monkeypatch.setattr("tracefix.docker_backend._run", fake_run)
    backend = DockerToolBackend(
        task_id="pytest-dev__pytest-10081",
        input_root=tmp_path / "inputs",
        run_dir=run_dir,
        run_id="wrong-image",
    )
    with pytest.raises(WorkspaceError, match="Docker image identity mismatch"):
        backend.prepare(commit, repo, tmp_path)
    assert not any(command[1:2] == ["create"] for command in commands)


def test_prepare_rejects_missing_source_tag_before_container_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "module.py")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@invalid", "commit", "-qm", "base")
    commit = _git(repo, "rev-parse", "HEAD")
    stage = tmp_path / "inputs" / "pytest-dev__pytest-10081"
    (stage / "recipes").mkdir(parents=True)
    (stage / "source.bundle").write_bytes(b"bundle sentinel")
    (stage / "recipes" / "pytest-dev__pytest-10081.json").write_text("{}", encoding="utf-8")
    (stage / "input-manifest.json").write_text(
        f'{{"task_id":"pytest-dev__pytest-10081","source_commit":"{commit}","files":{{}}}}',
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    expected_image = _IMAGES["pytest-dev__pytest-10081"][0]
    real_run = _run
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, expected_image.encode(), b"")
        if command[:4] == ["git", "-C", str(repo), "describe"]:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return real_run(command, **kwargs)

    monkeypatch.setattr("tracefix.docker_backend._run", fake_run)
    backend = DockerToolBackend(
        task_id="pytest-dev__pytest-10081",
        input_root=tmp_path / "inputs",
        run_dir=run_dir,
        run_id="missing-source-tag",
    )
    with pytest.raises(WorkspaceError, match="cannot determine version tag"):
        backend.prepare(commit, repo, tmp_path)
    assert not any(command[1:2] == ["create"] for command in commands)


@pytest.mark.parametrize("failure_stage", ["http", "https"])
def test_requests_service_health_failure_stops_agent_bridge_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_stage: str
) -> None:
    """Fail preparation before exposing tools when the Requests service is down."""
    import json

    import tracefix.docker_backend as docker_module

    repo = tmp_path / "requests-source"
    repo.mkdir()
    (repo / "requests.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "requests.py")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@invalid", "commit", "-qm", "base")
    _git(repo, "tag", "v1.0")
    commit = _git(repo, "rev-parse", "HEAD")

    task_id = "psf__requests-1766"
    input_root = tmp_path / "inputs"
    stage = input_root / task_id
    (stage / "recipes").mkdir(parents=True)
    (stage / "source.bundle").write_bytes(b"frozen bundle placeholder")
    recipe = {"service_health_url": "http://127.0.0.1:8765/"}
    if failure_stage == "https":
        recipe["test_pythonpath_entries"] = ["/opt/tracefix/request-test"]
    (stage / "recipes" / f"{task_id}.json").write_text(json.dumps(recipe), encoding="utf-8")
    (stage / "input-manifest.json").write_text(
        json.dumps({"task_id": task_id, "source_commit": commit, "files": {}}),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    expected_image = _IMAGES[task_id][0]
    commands: list[list[str]] = []
    health_checks: list[list[str]] = []
    tls_checks: list[list[str]] = []
    real_subprocess_run = subprocess.run

    def fake_backend_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if command[0] == "git":
            return real_subprocess_run(command, capture_output=True, check=False)
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, expected_image.encode(), b"")
        if command[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(command, 0, b"container-id", b"")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def fake_health_check(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if command[-1] == "/work/httpbin.log":
            return subprocess.CompletedProcess(command, 0, b"service failed to start", b"")
        if "ssl.create_default_context" in command[-1]:
            tls_checks.append(command)
            return subprocess.CompletedProcess(command, 1, b"", b"untrusted certificate")
        health_checks.append(command)
        http_code = 1 if failure_stage == "http" else 0
        return subprocess.CompletedProcess(command, http_code, b"", b"connection refused")

    monkeypatch.setattr(docker_module, "_run", fake_backend_run)
    monkeypatch.setattr(subprocess, "run", fake_health_check)
    monkeypatch.setattr(docker_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(DockerToolBackend, "_check_storage", lambda _self: None)

    backend = DockerToolBackend(
        task_id=task_id,
        input_root=input_root,
        run_dir=run_dir,
        run_id="requests-health-failure",
    )
    with pytest.raises(WorkspaceError, match="local Requests test service did not become healthy"):
        backend.prepare(commit, repo, tmp_path)

    assert backend.session is None
    assert len(health_checks) == (50 if failure_stage == "http" else 1)
    assert len(tls_checks) == (0 if failure_stage == "http" else 1)
    assert not any("agent-bridge.py" in command for command in commands)


def test_prepare_rejects_source_import_resolved_outside_agent_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A successful build cannot qualify if imports resolve to site-packages."""
    import json

    import tracefix.docker_backend as docker_module

    repo = tmp_path / "pytest-source"
    repo.mkdir()
    (repo / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "sample.py")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@invalid", "commit", "-qm", "base")
    _git(repo, "tag", "v1.0")
    commit = _git(repo, "rev-parse", "HEAD")

    task_id = "pytest-dev__pytest-10081"
    input_root = tmp_path / "inputs"
    stage = input_root / task_id
    (stage / "recipes").mkdir(parents=True)
    (stage / "source.bundle").write_bytes(b"frozen bundle placeholder")
    (stage / "recipes" / f"{task_id}.json").write_text(
        json.dumps({"source_import_probe": "pytest"}), encoding="utf-8"
    )
    (stage / "input-manifest.json").write_text(
        json.dumps({"task_id": task_id, "source_commit": commit, "files": {}}),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    expected_image = _IMAGES[task_id][0]
    commands: list[list[str]] = []
    real_subprocess_run = subprocess.run

    def fake_backend_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append(command)
        if command[0] == "git":
            return real_subprocess_run(command, capture_output=True, check=False)
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, expected_image.encode(), b"")
        if command[:2] == ["docker", "inspect"]:
            return subprocess.CompletedProcess(command, 0, b"container-id", b"")
        if command[:4] == ["docker", "exec", "-w", "/work/agent"] and "env" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                b'"/opt/python310/lib/python3.10/site-packages/pytest/__init__.py"\n',
                b"",
            )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(docker_module, "_run", fake_backend_run)
    monkeypatch.setattr(DockerToolBackend, "_check_storage", lambda _self: None)
    backend = DockerToolBackend(
        task_id=task_id,
        input_root=input_root,
        run_dir=run_dir,
        run_id="source-import-escape",
    )

    with pytest.raises(
        WorkspaceError,
        match="source import probe did not resolve inside Agent checkout",
    ):
        backend.prepare(commit, repo, tmp_path)

    assert backend.session is None
    assert any(
        any("importlib.import_module('pytest')" in argument for argument in command)
        for command in commands
    )


def test_source_archive_rejects_reserved_evidence_directory(tmp_path: Path) -> None:
    repo = tmp_path / "reserved-source"
    repo.mkdir()
    reserved = repo / ".tracefix-build-tmp"
    reserved.mkdir()
    (reserved / "unexpected.txt").write_text("must not be shadowed", encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@invalid", "commit", "-qm", "base")
    backend = object.__new__(DockerToolBackend)
    backend.run_dir = tmp_path
    with pytest.raises(WorkspaceError, match="reserved TraceFix evidence paths"):
        backend._source_archive(repo, _git(repo, "rev-parse", "HEAD"))
