from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tracefix.agent_bridge import _execute_job_control, serve_requests
from tracefix.capabilities import CapabilityCatalog, CapabilityPolicy, ToolDispatcher
from tracefix.jobs.constants import JOB_CONTROL_CAPABILITIES
from tracefix.jobs.executions import TestExecution as ExecutionRow
from tracefix.jobs.policy import JobActivityGuard
from tracefix.jobs.protocol import WorkerStartRequest
from tracefix.messages import ToolCall
from tracefix.tools.base import BaseTool, ToolRegistry, ToolResult, ToolSpec


class Tool(BaseTool):
    def __init__(self, name):
        self._spec = ToolSpec(name=name, description="fixture")
        self.prepared, self.executed = [], []

    @property
    def spec(self):
        return self._spec

    def prepare(self, call):
        self.prepared.append(call)

    def execute(self, call):
        self.executed.append(call)
        return ToolResult(call_id=call.id, tool_name=call.name, success=True,
                          output={"file": "text"})


class Controller:
    def __init__(self):
        self.service = SimpleNamespace(active=False, has_unknown=False)
        self.calls = []

    def reply(self, name, request):
        self.calls.append(name)
        return SimpleNamespace(model_dump=lambda **kwargs: {"job_id": request.job_id})

    def start(self, request):
        self.service.active = True
        return self.reply("start", request)

    def status(self, request):
        return self.reply("status", request)

    def cancel(self, request):
        self.service.active = False
        return self.reply("cancel", request)

    def read(self, request, **kwargs):
        self.calls.append("read")
        return {"content": "中段", "next_offset": 2}

    def shutdown(self, **kwargs):
        self.calls.append("shutdown")
        return {"closed": True}


def start_request():
    call = ToolCall(id="logical-test", name="run_tests", arguments={"command": "pytest -q"})
    ticket = ExecutionRow(execution_id="a" * 32, call_id=call.id, owner_epoch="b" * 32,
                           invocation_sha256=hashlib.sha256(call.model_dump_json().encode()).hexdigest(),
                           phase="dispatched", process_started=None)
    return WorkerStartRequest(version=1, worker_epoch="c" * 32, logical_run_id="root-run",
                              job_id="job_" + "d" * 32, ticket=ticket,
                              call_json=call.model_dump_json(),
                              deadline=datetime.now(UTC) + timedelta(seconds=5))


def test_main_rpc_loop_remains_responsive_and_raw_tests_cannot_bypass_tickets(
    tmp_path, monkeypatch, capsys,
):
    controller, request = Controller(), start_request()
    registry = ToolRegistry([Tool(name) for name in ("run_tests", "read_file", "get_git_diff")])
    dispatcher = ToolDispatcher(CapabilityPolicy(CapabilityCatalog.for_tool_names(
        (*registry.names, *JOB_CONTROL_CAPABILITIES)), "workspace_write"),
        execution_guard=JobActivityGuard(controller.service))
    query = {"worker_epoch": request.worker_epoch, "job_id": request.job_id}
    rows = [
        ("__prepare__", request.call.model_dump(mode="json")),
        ("__job_start__", request.model_dump(mode="json")),
        ("run_tests", {"command": "pytest -q"}),
        ("__prepare__", request.call.model_dump(mode="json")),
        ("__recovery_state__", {}), ("__job_read__", query),
        ("read_file", {"path": "sample.py"}), ("__job_status__", query),
        ("__job_cancel__", query), ("run_tests", {"command": "pytest -q"}),
        ("__job_shutdown__", {"worker_epoch": request.worker_epoch}),
    ]
    calls = [ToolCall(id=f"rpc-{index}", name=name, arguments=arguments)
             for index, (name, arguments) in enumerate(rows)]
    monkeypatch.setattr("sys.stdin", io.StringIO(
        "".join(call.model_dump_json() + "\n" for call in calls)))
    assert serve_requests(registry, dispatcher, set(), tmp_path, controller) == 0
    frames = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(frames) == len(calls)
    assert all(frame["type"] == "result" and frame["call_id"] == call.id
               for frame, call in zip(frames, calls, strict=True))
    assert len(registry.get("run_tests").prepared) == 1
    assert registry.get("run_tests").executed == []
    assert registry.get("get_git_diff").executed == []
    assert len(registry.get("read_file").executed) == 1
    assert controller.calls == ["start", "read", "status", "cancel", "shutdown"]
    assert all(not frames[index]["result"]["success"] for index in (2, 3, 4, 9))
    assert "host execution ticket" in frames[9]["result"]["error"]


def test_control_decoder_rejects_unknown_fields_names_and_oversized_reads():
    controller, request = Controller(), start_request()
    for name, arguments in [
        ("__job_start__", {**request.model_dump(mode="json"), "origin": "trusted"}),
        ("__job_read__", {"worker_epoch": request.worker_epoch, "job_id": request.job_id,
                          "limit": 4097}),
        ("__not_a_job_control__", {}),
    ]:
        with pytest.raises(ValueError):
            _execute_job_control(controller, ToolCall(id="bad", name=name, arguments=arguments))
    assert controller.calls == []
