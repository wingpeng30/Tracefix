"""Pure main-thread job interface; implementations own persistence and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from tracefix.jobs.domain import JobRecord
from tracefix.tools.base import ToolResult


@dataclass(frozen=True, slots=True)
class JobCompletion:
    record: JobRecord
    result: ToolResult


class JobSourceState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_diff_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    nonempty_diff: StrictBool


class AgentJobSession(Protocol):
    @property
    def active(self) -> bool: ...

    @property
    def has_unknown(self) -> bool: ...

    @property
    def test_runs(self) -> int: ...

    def completions(self) -> tuple[JobCompletion, ...]: ...
    def source_state(self) -> JobSourceState: ...
    def shutdown(self) -> bool: ...
