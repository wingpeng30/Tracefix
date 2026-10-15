"""Live execution constraints can deny a call; they never grant a capability."""

from __future__ import annotations

from typing import Protocol

from tracefix.capabilities import CallerOrigin, CapabilityDescriptor


class JobActivity(Protocol):
    @property
    def active(self) -> bool: ...

    @property
    def has_unknown(self) -> bool: ...


class JobActivityGuard:
    def __init__(self, jobs: JobActivity) -> None:
        self.jobs = jobs

    def check(self, descriptor: CapabilityDescriptor, origin: CallerOrigin) -> str | None:
        name = descriptor.tool_name
        if name == "cancel_job":
            return None
        control_process = name in {"get_git_diff", "source_snapshot_export"}
        if self.jobs.has_unknown and (
            descriptor.effect != "read" or control_process or descriptor.execution_location == "mcp"
        ):
            return "job_outcome_unknown"
        if self.jobs.active and (
            descriptor.effect in {"execute", "workspace_write"}
            or name == "checkpoint_commit" or control_process
            or descriptor.execution_location == "mcp"
        ):
            return "active_test_requires_quiescent_checkout"
        return None
