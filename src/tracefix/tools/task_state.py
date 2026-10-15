"""Model-facing, bounded task-state tools."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tracefix.messages import ToolCall
from tracefix.task_state import (
    MAX_TASK_STATE_OPERATIONS,
    TaskStateError,
    TaskStateOperation,
    TaskStateRenderer,
    TaskStateRevisionConflict,
    TaskStateSession,
)
from tracefix.tools.base import BaseTool, ToolResult, ToolSpec


class _GetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _UpdateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    operations: list[TaskStateOperation] = Field(min_length=1, max_length=MAX_TASK_STATE_OPERATIONS)


class GetTaskStateTool(BaseTool):
    SPEC = ToolSpec(
        name="get_task_state",
        description=(
            "Read the bounded working summary and evidence references for this task. "
            "This state does not prove that tests passed or that the task is complete."
        ),
        input_schema=_GetArgs.model_json_schema(),
    )

    def __init__(self, session: TaskStateSession) -> None:
        self.session = session

    @property
    def spec(self) -> ToolSpec:
        return self.SPEC.model_copy(deep=True)

    def execute(self, call: ToolCall) -> ToolResult:
        try:
            _GetArgs.model_validate(call.arguments)
            state = self.session.snapshot()
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=True,
                output={
                    "revision": state.revision,
                    "state": json.loads(TaskStateRenderer.render(state)),
                    "evidence_role": "working_state_only",
                },
            )
        except (ValidationError, TaskStateError) as exc:
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=False,
                error=str(exc) if isinstance(exc, TaskStateError) else "invalid task-state read",
            )


class UpdateTaskStateTool(BaseTool):
    SPEC = ToolSpec(
        name="update_task_state",
        description=(
            "Add or update plan items, hypotheses, questions, and blockers. "
            "User requirements, evidence, permissions, budgets, and completion "
            "status are read-only."
        ),
        input_schema=_UpdateArgs.model_json_schema(),
    )

    def __init__(self, session: TaskStateSession) -> None:
        self.session = session

    @property
    def spec(self) -> ToolSpec:
        return self.SPEC.model_copy(deep=True)

    def execute(self, call: ToolCall) -> ToolResult:
        try:
            args = _UpdateArgs.model_validate(call.arguments)
            state, created_ids = self.session.update(args.expected_revision, args.operations)
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=True,
                output={
                    "revision": state.revision,
                    "created_ids": list(created_ids),
                    "evidence_role": "working_state_only",
                },
            )
        except (ValidationError, TaskStateError) as exc:
            conflict = isinstance(exc, TaskStateRevisionConflict)
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=False,
                error=(
                    "task state revision conflict; read the latest revision"
                    if conflict
                    else (
                        str(exc) if isinstance(exc, TaskStateError) else "invalid task-state update"
                    )
                ),
                metadata={"reason": "revision_conflict" if conflict else "invalid_update"},
            )
