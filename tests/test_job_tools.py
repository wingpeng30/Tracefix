from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tracefix.capabilities import CallerOrigin, CapabilityCatalog, CapabilityPolicy, ToolDispatcher
from tracefix.jobs.domain import JobIdentity, JobRecord
from tracefix.messages import ToolCall
from tracefix.tools.base import ToolRegistry, ToolResult
from tracefix.tools.jobs import register_job_tools


class Service:
    run_id = "r1"

    def __init__(self):
        now = datetime.now(UTC)
        self.record = JobRecord(
            identity=JobIdentity(job_id="job_" + "a" * 32, run_id=self.run_id, call_id="start",
                                 execution_id="b" * 32, invocation_sha256="c" * 64),
            created_at=now, deadline=now + timedelta(seconds=30),
        )
        self.starts = self.reads = 0

    def start(self, call):
        assert call.name == "run_tests"
        self.starts += 1
        return self.record

    def get(self, job_id):
        assert job_id == self.record.identity.job_id
        return self.record

    def result(self, job_id):
        return ToolResult(call_id="start", tool_name="run_tests", success=False,
                          output={"stdout": "NOT-A-FREE-LOG-READ", "test_status": "running"},
                          error="not completed")

    def read(self, job_id, *, stream, offset, limit):
        self.reads += 1
        text = "中文🙂 output"
        page = text[offset:offset + limit]
        return {"content": page, "next_offset": offset + len(page), "final": False,
                "complete": True, "stream": stream, "at_committed_end": False}

    def cancel(self, job_id):
        return self.get(job_id)


def test_job_cursor_is_bound_to_job_run_and_stream_and_counts_unicode():
    service = Service()
    registry = ToolRegistry()
    register_job_tools(registry, service)
    tool = registry.get("read_job_output")
    args = {"job_id": service.record.identity.job_id, "limit": 2}
    first = tool.execute(ToolCall(id="r1", name=tool.spec.name, arguments=args))
    assert first.success and first.output["content"] == "中文"
    cursor = first.output["cursor"]
    second = tool.execute(ToolCall(id="r2", name=tool.spec.name,
                                   arguments={**args, "cursor": cursor}))
    assert second.success and second.output["content"] == "🙂 "
    bad = tool.execute(ToolCall(id="r3", name=tool.spec.name,
                                arguments={**args, "cursor": cursor, "stream": "stderr"}))
    assert not bad.success and service.reads == 2
    service.run_id = "different"
    bad = tool.execute(ToolCall(id="r4", name=tool.spec.name,
                                arguments={**args, "cursor": cursor}))
    assert not bad.success and service.reads == 2


def test_policy_denies_start_before_service_and_status_does_not_bypass_log_quota():
    registry = ToolRegistry()
    service = Service()
    register_job_tools(registry, service)
    policy = CapabilityPolicy(CapabilityCatalog.for_tool_names(registry.names), "inspect")
    dispatcher = ToolDispatcher(policy)
    assert not dispatcher.authorize("start_test_job", CallerOrigin.MODEL).allowed
    assert dispatcher.authorize("get_job_status", CallerOrigin.MODEL).allowed
    assert service.starts == 0
    tool = registry.get("get_job_status")
    result = tool.execute(ToolCall(id="query", name=tool.spec.name,
                                  arguments={"job_id": service.record.identity.job_id}))
    assert result.success and "NOT-A-FREE-LOG-READ" not in str(result.output)


def test_start_tool_cannot_accept_origin_pid_or_host_path():
    registry = ToolRegistry()
    service = Service()
    register_job_tools(registry, service)
    tool = registry.get("start_test_job")
    result = tool.execute(ToolCall(id="bad", name=tool.spec.name,
                                  arguments={"command": "pytest -q", "origin": "host_lifecycle"}))
    assert not result.success and service.starts == 0
