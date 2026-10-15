"""Stable identities shared by local and ordinary Docker validation gates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from typing import Any, Protocol

from tracefix.capabilities import CallerOrigin, ToolDispatcher
from tracefix.exceptions import RunConfigurationError
from tracefix.messages import ToolCall
from tracefix.tools.base import BaseTool, ToolRegistry, ToolResult
from tracefix.tools.invocation import PreparedInvocation


class ValidationBackend(Protocol):
    """Execute a validation call without owning budget or qualification rules."""

    def execute(self, call: ToolCall) -> dict[str, Any]: ...


def _authorize(dispatcher: ToolDispatcher) -> None:
    decision = dispatcher.authorize("run_tests", CallerOrigin.VALIDATION_GATE)
    if not decision.allowed:
        raise RunConfigurationError(
            "validation execution denied by capability policy",
            context={"reason_code": decision.reason_code},
        )


class LocalValidationBackend:
    """Acquire a fresh checkout per baseline call and always release it."""

    def __init__(
        self, acquire: Callable[[], AbstractContextManager[BaseTool]],
        dispatcher: ToolDispatcher,
    ) -> None:
        self.acquire = acquire
        self.dispatcher = dispatcher

    def execute(self, call: ToolCall) -> dict[str, Any]:
        _authorize(self.dispatcher)
        with self.acquire() as tool:
            return PreparedInvocation(tool, call).execute().model_dump(mode="json")


class ValidationContainer(Protocol):
    def export_evidence(self) -> Any: ...
    def close(self, *, remove: bool) -> Any: ...


class DockerValidationBackend:
    """Own one disposable container per baseline call, including failed prepare."""

    def __init__(
        self, create: Callable[[], ValidationContainer],
        prepare: Callable[[ValidationContainer], ToolRegistry],
        dispatcher: ToolDispatcher,
    ) -> None:
        self.create = create
        self.prepare = prepare
        self.dispatcher = dispatcher

    def execute(self, call: ToolCall) -> dict[str, Any]:
        _authorize(self.dispatcher)
        backend = self.create()
        try:
            tool = self.prepare(backend).get("run_tests")
            result = PreparedInvocation(tool, call).execute().model_dump(mode="json")
            backend.export_evidence()
            return result
        finally:
            backend.close(remove=True)


class ActiveValidationBackend:
    """Final validation uses the current task tool and its invocation callback."""

    def __init__(self, tool: BaseTool, dispatcher: ToolDispatcher | None) -> None:
        self.tool = tool
        self.dispatcher = dispatcher

    def prepare(self, call: ToolCall) -> PreparedInvocation:
        if self.dispatcher is not None:
            return self.dispatcher.prepare_invocation(
                self.tool, call, CallerOrigin.VALIDATION_GATE
            )
        return PreparedInvocation(self.tool, call)

    def execute(self, call: ToolCall) -> dict[str, Any]:
        result: ToolResult = self.prepare(call).execute()
        return result.model_dump(mode="json")


def docker_validation_environment_sha256(
    image_id: str | None, environment_variables: Mapping[str, str]
) -> str:
    """Bind Docker validation to its immutable image and explicit safe environment."""
    payload = json.dumps(
        {
            "backend": "docker",
            "image_id": image_id,
            "environment_variables": dict(environment_variables),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
