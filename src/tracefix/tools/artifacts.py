"""Tool adapters for the run-scoped artifact read and literal search APIs."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tracefix.artifacts import (
    MAX_PAGE_CHARS,
    MAX_SEARCH_MATCHES,
    ArtifactError,
    ArtifactStore,
)
from tracefix.messages import ToolCall
from tracefix.tools.base import BaseTool, ReservedToolName, ToolResult, ToolSpec


class _ReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    stream: Literal["stdout", "stderr"] = "stdout"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=MAX_PAGE_CHARS, ge=1, le=MAX_PAGE_CHARS)


class _SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=256)
    max_matches: int = Field(default=MAX_SEARCH_MATCHES, ge=1, le=MAX_SEARCH_MATCHES)


class ReadToolArtifactTool(BaseTool):
    """Read one bounded page from a redacted immutable artifact view."""

    SPEC = ToolSpec(
        name="read_tool_artifact",
        description=(
            "Read a bounded page from a previously captured tool artifact. "
            "Use the opaque artifact_id returned by a tool; offsets count Unicode characters."
        ),
        input_schema=_ReadArgs.model_json_schema(),
    )

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    @property
    def spec(self) -> ToolSpec:
        return self.SPEC.model_copy(deep=True)

    def execute(self, call: ToolCall) -> ToolResult:
        try:
            args = _ReadArgs.model_validate(call.arguments)
            page = self.store.read(
                args.artifact_id,
                stream=args.stream,
                offset=args.offset,
                limit=args.limit,
            )
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=True,
                output={
                    "artifact_id": page.artifact.artifact_id,
                    "source_identity": page.artifact.source_identity,
                    "integrity": page.artifact.integrity,
                    "complete": page.artifact.complete,
                    "stream": page.stream,
                    "content": page.content,
                    "offset": page.offset,
                    "next_offset": page.next_offset,
                    "eof": page.eof,
                    "evidence_role": "historical_tool_output_only",
                },
            )
        except (ValidationError, ArtifactError) as exc:
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=False,
                error=(
                    str(exc)
                    if isinstance(exc, ArtifactError)
                    else "invalid artifact page request"
                ),
            )


class SearchToolArtifactTool(BaseTool):
    """Search artifact read views using literal case-insensitive text only."""

    SPEC = ToolSpec(
        name="search_tool_artifact",
        description=(
            "Search previously captured tool artifacts with literal text. "
            "Search is bounded and does not accept regular expressions or paths."
        ),
        input_schema=_SearchArgs.model_json_schema(),
    )

    def __init__(self, store: ArtifactStore) -> None:
        self.store = store

    @property
    def spec(self) -> ToolSpec:
        return self.SPEC.model_copy(deep=True)

    def execute(self, call: ToolCall) -> ToolResult:
        try:
            args = _SearchArgs.model_validate(call.arguments)
            result = self.store.search(args.query, max_matches=args.max_matches)
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=True,
                output={
                    "query": result.query,
                    "hits": [hit.model_dump(mode="json") for hit in result.hits],
                    "truncated": result.truncated,
                    "queries_remaining": result.queries_remaining,
                    "characters_remaining": result.characters_remaining,
                    "evidence_role": "historical_tool_output_only",
                },
            )
        except (ValidationError, ArtifactError) as exc:
            return ToolResult(
                call_id=call.id,
                tool_name=self.spec.name,
                success=False,
                error=(
                    str(exc)
                    if isinstance(exc, ArtifactError)
                    else "invalid artifact search request"
                ),
            )


class ArtifactRecordingTool(BaseTool):
    """Persist the complete structured result returned by an existing read tool."""

    _RECORDED_NAMES = {
        ReservedToolName.SEARCH_CODE.value,
        ReservedToolName.READ_FILE.value,
        ReservedToolName.GET_GIT_DIFF.value,
    }

    def __init__(self, inner: BaseTool, store: ArtifactStore) -> None:
        if inner.spec.name not in self._RECORDED_NAMES:
            raise ValueError("only search, read and diff results can use this adapter")
        self.inner = inner
        self.store = store

    @property
    def spec(self) -> ToolSpec:
        return self.inner.spec

    @property
    def skill_catalog(self):
        return self.inner.skill_catalog

    def prepare(self, call: ToolCall) -> Any:
        prepare = getattr(self.inner, "prepare", None)
        return prepare(call) if prepare is not None else None

    def execute(self, call: ToolCall) -> ToolResult:
        result = self.inner.execute(call)
        if call.id == "checkpoint-diff":
            return result
        serialized = result.model_dump_json().encode("utf-8")
        result_digest = hashlib.sha256(serialized).hexdigest()
        try:
            ref = self.store.publish_process(
                call_id=call.id,
                tool_name=call.name,
                source_identity={"returned_result_sha256": result_digest},
                stdout=serialized,
                stderr=b"",
                complete=True,
            )
        except (ArtifactError, OSError) as exc:
            metadata = dict(result.metadata)
            metadata["artifact_recording_error"] = (
                "artifact_quota_exhausted"
                if "quota" in str(exc).casefold() or "limit" in str(exc).casefold()
                else f"artifact_publish_failed:{type(exc).__name__}"
            )
            return result.model_copy(update={"metadata": metadata}, deep=True)
        if isinstance(result.output, dict):
            output = dict(result.output)
        else:
            output = {"value": result.output}
        output["artifact_refs"] = [ref.model_dump(mode="json")]
        output["artifact_archive_scope"] = "structured_tool_result_returned_by_tool"
        return result.model_copy(update={"output": output}, deep=True)
