"""Optional search adapter with per-candidate rule delivery and opaque paging."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from tracefix.exceptions import ToolValidationError
from tracefix.messages import Message, ToolCall
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.search import PagedRepositorySearch, RuleSearchArguments
from tracefix.tools.base import BaseTool, ToolResult, ToolSpec


class RepositorySearchTool(BaseTool):
    _SPEC = ToolSpec(
        name="search_code",
        description=("Search literal text within the workspace. Unseen repository rules pause "
                     "a page before its file is read. After receiving the rules, repeat the same "
                     "query with the returned opaque cursor. "
                     "Changed queries require a new search."),
        input_schema=RuleSearchArguments.model_json_schema(),
    )

    def __init__(self, search: PagedRepositorySearch) -> None:
        self.search = search

    @property
    def spec(self) -> ToolSpec:
        return self._SPEC

    def validate_call(self, call: ToolCall) -> None:
        self.search.validate_call(call)

    @property
    def run_id(self) -> str:
        return self.search.run_id

    def checkpoint(self, history: Sequence[Message]) -> dict:
        return self.search.checkpoint(history)

    def prepare_restore(self, value: object, history: Sequence[Message]) -> Callable[[], None]:
        return self.search.prepare_restore(value, history)

    def execute(self, call: ToolCall) -> ToolResult:
        started = time.monotonic()
        try:
            output = self.search.search(call)
        except RuleError as exc:
            raise ToolValidationError(exc.reason_code, context={
                "reason_code": exc.reason_code,
            }) from exc
        metadata = {"per_candidate_rules": True, "cacheable": False}
        if isinstance(output.get("cursor"), str):
            metadata["repository_search_cursor_sha256"] = self.search.cursor_fingerprint(
                output["cursor"])
        return ToolResult(call_id=call.id, tool_name=call.name, success=True, output=output,
                          metadata=metadata,
                          duration_ms=(time.monotonic() - started) * 1000)
