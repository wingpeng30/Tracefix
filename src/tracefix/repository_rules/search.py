"""Run-bound, literal search pages that gate each candidate before reading it."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from uuid import uuid4

from pydantic import ValidationError

from tracefix.messages import ToolCall
from tracefix.repository_rules.domain import RuleSelection, digest
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.gate import RuleDelivery, RuleSelector
from tracefix.repository_rules.paths import matches_glob, relative_path, validate_glob
from tracefix.source_view import SourceView
from tracefix.tool_arguments import RuleSearchArguments as RuleSearchArguments

MAX_SEARCH_CURSORS = 128
MAX_SEARCH_SOURCE_BYTES = 1_000_000


@dataclass(frozen=True)
class _Cursor:
    run_id: str
    query_sha256: str
    candidates_sha256: str
    position: int
    line_offset: int
    file_sha256: str | None
    prefix_count: int
    rules_sha256: str


class PagedRepositorySearch:
    """Only opaque cursor IDs leave this service; paths are never cursor input."""

    def __init__(self, run_id: str, *,
                 candidates: Callable[[RuleSearchArguments], Sequence[str]],
                 source: SourceView, selector: RuleSelector, delivery: RuleDelivery,
                 case_sensitive_paths: bool = True) -> None:
        if not isinstance(run_id, str) or not 1 <= len(run_id) <= 200:
            raise RuleError("rules_search_run_identity_invalid")
        self.run_id, self.candidates, self.source = run_id, candidates, source
        self.selector, self.delivery = selector, delivery
        if type(case_sensitive_paths) is not bool:
            raise RuleError("rules_case_semantics_invalid")
        self.case_sensitive_paths = case_sensitive_paths
        self._cursors: dict[str, _Cursor] = {}

    def validate_call(self, call: ToolCall) -> RuleSearchArguments:
        if call.name != "search_code":
            raise RuleError("rules_search_tool_name_invalid")
        try:
            args = RuleSearchArguments.model_validate(call.arguments)
        except ValidationError as exc:
            raise RuleError("rules_search_arguments_invalid") from exc
        relative_path(args.path, allow_root=True)
        validate_glob(args.glob)
        if args.cursor is not None:
            state = self._cursors.get(args.cursor)
            if (state is None or state.run_id != self.run_id
                    or state.query_sha256 != self._query_hash(args)):
                raise RuleError("rules_search_cursor_unknown_or_mismatched")
        return args

    def cursor_fingerprint(self, cursor: str) -> str:
        state = self._cursors.get(cursor)
        if state is None:
            raise RuleError("rules_search_cursor_unknown_or_mismatched")
        return digest(asdict(state))

    def checkpoint(self, history: Sequence) -> dict:
        from tracefix.repository_rules.search_state import validate_search

        payload = {"run_id": self.run_id, "limit": MAX_SEARCH_CURSORS,
                   "cursors": {key: asdict(state) for key, state in self._cursors.items()}}
        envelope = {"schema_version": 1, "payload": payload, "payload_sha256": digest(payload)}
        saved = validate_search(envelope, history, run_id=self.run_id,
                                cursor_limit=MAX_SEARCH_CURSORS)
        payload = saved.model_dump(mode="json")
        return {"schema_version": 1, "payload": payload, "payload_sha256": digest(payload)}

    def restore(self, value: object, history: Sequence) -> None:
        self.prepare_restore(value, history)()

    def prepare_restore(self, value: object, history: Sequence) -> Callable[[], None]:
        from tracefix.repository_rules.search_state import validate_search

        saved = validate_search(value, history, run_id=self.run_id,
                                cursor_limit=MAX_SEARCH_CURSORS)
        cursors = {key: _Cursor(**cursor.model_dump())
                   for key, cursor in saved.cursors.items()}

        def publish() -> None:
            self._cursors = cursors

        return publish

    @staticmethod
    def _query_hash(args: RuleSearchArguments) -> str:
        return digest(args.model_dump(exclude={"cursor"}))

    def _select(self, path: str) -> RuleSelection:
        try:
            selection = RuleSelection.model_validate(self.selector.select(path))
        except ValidationError as exc:
            raise RuleError("rules_selection_invalid") from exc
        if selection.target_path != path:
            raise RuleError("rules_selection_target_mismatch")
        return selection

    def _issue(self, args, candidates_sha, position, line_offset, file_sha, versions):
        if len(self._cursors) >= MAX_SEARCH_CURSORS:
            raise RuleError("rules_search_cursor_limit")
        key = "rules_cursor_" + uuid4().hex
        self._cursors[key] = _Cursor(self.run_id, self._query_hash(args), candidates_sha,
                                     position, line_offset, file_sha, len(versions),
                                     digest(versions))
        return key

    @staticmethod
    def _continuation(args: RuleSearchArguments, cursor: str | None):
        return ({"tool_name": "search_code", "arguments": {
            **args.model_dump(exclude={"cursor"}), "cursor": cursor,
        }} if cursor is not None else None)

    def search(self, call: ToolCall) -> dict:
        args = self.validate_call(call)
        candidates = []
        for index, path in enumerate(self.candidates(args)):
            if index >= 5000:
                raise RuleError("rules_access_path_limit")
            path = relative_path(path)
            target, root = path, args.path
            if not self.case_sensitive_paths:
                target, root = target.casefold(), root.casefold()
            if root != "." and target != root and not target.startswith(root + "/"):
                raise RuleError("rules_search_candidate_outside_query")
            if target != root:
                relative = path if root == "." else path[len(args.path) + 1:]
                if not matches_glob(args.glob, relative, case_sensitive=self.case_sensitive_paths):
                    raise RuleError("rules_search_candidate_outside_query")
            candidates.append(path)
        if len(set(candidates)) != len(candidates):
            raise RuleError("rules_search_duplicate_candidate")
        candidates_sha = digest(candidates)
        state = self._cursors.get(args.cursor) if args.cursor is not None else None
        position, line_offset, expected_file_sha = 0, 0, None
        versions = []
        if state is not None:
            if state.candidates_sha256 != candidates_sha:
                raise RuleError("rules_search_cursor_candidates_changed")
            position, line_offset, expected_file_sha = (
                state.position, state.line_offset, state.file_sha256)
            versions = [self._select(path).sha256 for path in candidates[:state.prefix_count]]
            if digest(versions) != state.rules_sha256:
                raise RuleError("rules_search_cursor_rules_changed")
        matches, searched, skipped, clipped = [], 0, 0, False
        needle = args.query if args.case_sensitive else args.query.casefold()
        while position < len(candidates):
            path = candidates[position]
            selection = self._select(path)
            if position < len(versions):
                if versions[position] != selection.sha256:
                    raise RuleError("rules_search_cursor_rules_changed")
            else:
                versions.append(selection.sha256)
            if not self.delivery.is_delivered(selection):
                self.delivery.defer(selection)
                cursor = self._issue(args, candidates_sha, position, line_offset,
                                     expected_file_sha, versions)
                return {"query": args.query, "matches": matches, "searched_files": searched,
                        "skipped_files": skipped, "truncated": True, "eof": False,
                        "rules_pending": True, "pending_path": path,
                        "pending_reason": "rules_pending", "cursor": cursor,
                        "continuation": self._continuation(args, cursor)}
            try:
                raw = self.source.read_bytes(path)
            except RuleError as exc:
                if exc.reason_code != "rules_source_limit_exceeded":
                    raise
                if expected_file_sha is not None:
                    raise RuleError("rules_search_cursor_source_changed") from exc
                skipped += 1
                position, line_offset, expected_file_sha = position + 1, 0, None
                continue
            if raw is None:
                raise RuleError("rules_search_source_changed")
            if not isinstance(raw, bytes) or len(raw) > MAX_SEARCH_SOURCE_BYTES:
                raise RuleError("rules_search_source_port_invalid")
            file_sha = hashlib.sha256(raw).hexdigest()
            if expected_file_sha is not None and file_sha != expected_file_sha:
                raise RuleError("rules_search_cursor_source_changed")
            if b"\x00" in raw[:8192]:
                position, line_offset, expected_file_sha = position + 1, 0, None
                continue
            searched += 1
            lines = raw.decode("utf-8", errors="replace").splitlines()
            for line_index in range(line_offset, len(lines)):
                line = lines[line_index]
                column = (line if args.case_sensitive else line.casefold()).find(needle)
                if column < 0:
                    continue
                clipped |= len(line) > 500
                matches.append({"path": path, "line": line_index + 1, "column": column + 1,
                                "text": line[:500]})
                if len(matches) == args.max_results:
                    next_line = line_index + 1
                    if next_line == len(lines):
                        position, next_line, file_sha = position + 1, 0, None
                    cursor = (self._issue(args, candidates_sha, position, next_line,
                                          file_sha, versions)
                              if position < len(candidates) else None)
                    return {"query": args.query, "matches": matches, "searched_files": searched,
                            "skipped_files": skipped, "truncated": bool(cursor) or clipped
                            or bool(skipped), "eof": cursor is None, "rules_pending": False,
                            "cursor": cursor, "continuation": self._continuation(args, cursor)}
            position, line_offset, expected_file_sha = position + 1, 0, None
        return {"query": args.query, "matches": matches, "searched_files": searched,
                "skipped_files": skipped, "truncated": clipped or bool(skipped), "eof": True,
                "rules_pending": False, "cursor": None, "continuation": None}
