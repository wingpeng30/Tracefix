"""Single-use prepared calls; process callbacks belong to one invocation only."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock

from tracefix.messages import ToolCall
from tracefix.tools.base import BaseTool, ToolResult


class PreparedInvocation:
    """Freeze arguments after validation and never replay a dispatched call.

    Legacy tools without a prepare method keep their existing execute contract.
    Only test tools with preparation support receive a process-start callback.
    """

    def __init__(self, tool: BaseTool, call: ToolCall) -> None:
        if tool.spec.name != call.name:
            raise ValueError("invocation tool name differs from call")
        self._tool = tool
        self._call_json = call.model_dump_json()
        self._lock = Lock()
        self._dispatched = False
        prepare = getattr(tool, "prepare", None) if call.name == "run_tests" else None
        self.tracks_process_start = callable(prepare)
        if self.tracks_process_start:
            prepare(ToolCall.model_validate_json(self._call_json))

    def execute(self, on_process_started: Callable[[], None] | None = None) -> ToolResult:
        with self._lock:
            if self._dispatched:
                raise RuntimeError("prepared invocation has already been dispatched")
            self._dispatched = True
        if not self.tracks_process_start:
            return self._tool.execute(ToolCall.model_validate_json(self._call_json))
        previous = getattr(self._tool, "on_process_started", None)
        notified = False

        def started(*_args: object) -> None:
            nonlocal notified
            if not notified:
                notified = True
                if on_process_started is not None:
                    on_process_started()

        self._tool.on_process_started = started
        try:
            return self._tool.execute(ToolCall.model_validate_json(self._call_json))
        finally:
            self._tool.on_process_started = previous
