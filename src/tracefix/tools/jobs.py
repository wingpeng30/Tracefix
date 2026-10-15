"""Fixed job tools: opaque run-bound locators and quota-preserving output reads."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, ValidationError

from tracefix.artifacts import MAX_ARTIFACT_BYTES, ArtifactError
from tracefix.exceptions import ToolError
from tracefix.jobs.domain import JobError, JobRecord
from tracefix.messages import ToolCall
from tracefix.tools.base import BaseTool, ToolRegistry, ToolResult, ToolSpec


class JobAccess(Protocol):
    run_id: str

    def start(self, call: ToolCall) -> JobRecord: ...
    def get(self, job_id: str) -> JobRecord: ...
    def result(self, job_id: str) -> ToolResult | None: ...
    def cancel(self, job_id: str) -> JobRecord: ...
    def read(self, job_id: str, *, stream: str, offset: int, limit: int) -> dict: ...


class _StartArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(default="pytest -q", max_length=8192)
    timeout_seconds: StrictFloat | None = Field(default=None, gt=0, le=600)


class _JobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(pattern=r"^job_[a-f0-9]{32}$")


class _ReadArgs(_JobArgs):
    stream: Literal["stdout", "stderr"] = "stdout"
    cursor: str | None = Field(default=None, max_length=2048)
    limit: StrictInt = Field(default=4096, ge=1, le=4096)


def _cursor(record: JobRecord, stream: str, offset: int) -> str:
    value = {"v": 1, "job": record.identity.job_id, "run": record.identity.run_id,
             "stream": stream, "offset": offset}
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return (base64.urlsafe_b64encode(encoded).decode().rstrip("=")
            + "." + hashlib.sha256(encoded).hexdigest()[:16])


def _offset(cursor: str | None, record: JobRecord, stream: str) -> int:
    if cursor is None:
        return 0
    try:
        encoded, checksum = cursor.rsplit(".", 1)
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if len(raw) > 1024 or hashlib.sha256(raw).hexdigest()[:16] != checksum:
            raise ValueError
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get("offset")) is not int
                or not 0 <= value["offset"] <= MAX_ARTIFACT_BYTES
                or type(value.get("v")) is not int
                or value != {"v": 1, "job": record.identity.job_id, "run": record.identity.run_id,
                             "stream": stream, "offset": value["offset"]}):
            raise ValueError
        return value["offset"]
    except (ValueError, UnicodeError) as exc:
        raise JobError("job_output_cursor_invalid_or_mismatched") from exc


_TOOLS = {
    "start_test_job": (_StartArgs,
                       "Start one background pytest test job. Only the existing pytest allowlist "
                       "is supported; an active checkout cannot run another test "
                       "or accept patches."),
    "get_job_status": (_JobArgs,
                       "Query an owned job. Completion means execution ended; the main task's "
                       "test evidence and final validation still determine completion."),
    "read_job_output": (_ReadArgs,
                        "Read at most 4096 Unicode characters of committed redacted job output. "
                        "Reuse the opaque cursor for this job and stream. "
                        "Shared read quotas apply."),
    "cancel_job": (_JobArgs,
                   "Request cancellation of an owned job. A cancellation request does not prove "
                   "that its entire process tree has stopped; query status for confirmation."),
}


class _JobTool(BaseTool):
    def __init__(self, name: str, service: JobAccess) -> None:
        self.service = service
        self.args_type, description = _TOOLS[name]
        self._spec = ToolSpec(name=name, description=description,
                              input_schema=self.args_type.model_json_schema())

    @property
    def spec(self) -> ToolSpec:
        return self._spec.model_copy(deep=True)

    def execute(self, call: ToolCall) -> ToolResult:
        try:
            if call.name != self._spec.name:
                raise JobError("job_tool_name_mismatch")
            args = self.args_type.model_validate(call.arguments)
            if isinstance(args, _StartArgs):
                record = self.service.start(ToolCall(
                    id=call.id, name="run_tests", arguments=args.model_dump(exclude_none=True)))
                record.require_owner(self.service.run_id)
                output = {"job": record.model_dump(mode="json")}
            else:
                record = self.service.get(args.job_id)
                record.require_owner(self.service.run_id)
                if isinstance(args, _ReadArgs):
                    offset = _offset(args.cursor, record, args.stream)
                    page = self.service.read(args.job_id, stream=args.stream,
                                             offset=offset, limit=args.limit)
                    output = {**page, "job_id": args.job_id,
                              "cursor": _cursor(record, args.stream, page["next_offset"]),
                              "evidence_role": "historical_job_output_only"}
                else:
                    if self._spec.name == "cancel_job":
                        record = self.service.cancel(args.job_id)
                    result = self.service.result(args.job_id)
                    details = result.output if result and isinstance(result.output, dict) else {}
                    output = {"job": record.model_dump(mode="json"),
                              "test_status": details.get("test_status"),
                              "artifact_refs": details.get("artifact_refs", [])}
            return ToolResult(call_id=call.id, tool_name=call.name, success=True, output=output)
        except (ValidationError, JobError, ArtifactError, ToolError) as exc:
            error = (exc.reason_code if isinstance(exc, JobError) else exc.message
                     if isinstance(exc, ToolError) else str(exc)
                     if isinstance(exc, ArtifactError) else "invalid job tool arguments")
            return ToolResult(call_id=call.id, tool_name=call.name, success=False, error=error)


def register_job_tools(registry: ToolRegistry, service: JobAccess) -> None:
    """Explicit runtime assembly only; the default tool registry remains unchanged."""
    for name in _TOOLS:
        registry.register(_JobTool(name, service))
