"""Version-one job argument normalization, shared by execution and reference verification."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictFloat

from tracefix.messages import ToolCall


class StartTestJobArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(default="pytest -q", max_length=8192)
    timeout_seconds: StrictFloat | None = Field(default=None, gt=0, le=600)


def normalize_job_start(call: ToolCall) -> ToolCall:
    if call.name != "start_test_job":
        raise ValueError("job start source must name the model-facing start tool")
    args = StartTestJobArguments.model_validate(call.arguments)
    return ToolCall(id=call.id, name="run_tests", arguments=args.model_dump(exclude_none=True))
