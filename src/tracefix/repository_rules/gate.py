"""Explicit rule gate over source selection, metadata and delivery ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from pydantic import ValidationError

from tracefix.exceptions import ToolValidationError
from tracefix.messages import Message, ToolCall
from tracefix.repository_rules.access import ToolAccessPlan, ToolAccessPlanner
from tracefix.repository_rules.delivery import RuleRequestTicket
from tracefix.repository_rules.domain import RuleSelection, digest
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.paths import relative_path
from tracefix.tools.base import ToolSpec

MAX_ACCESS_SELECTION_BYTES = 8 * 1024 * 1024


class AccessPathResolver(Protocol):
    """Enumerate file paths from metadata, without reading source bodies."""

    def resolve(self, plan: ToolAccessPlan) -> Sequence[str]: ...


class RuleSelector(Protocol):
    def select(self, target: str) -> RuleSelection: ...


class RuleDelivery(Protocol):
    @property
    def registered_message_ids(self) -> frozenset[str]: ...
    def is_delivered(self, selection: RuleSelection) -> bool: ...
    def defer(self, selection: RuleSelection) -> None: ...
    def close_batch(self, history: Sequence[Message]) -> tuple[Message, ...]: ...
    def prepare_request(self, messages: Sequence[Message],
                        tools: Sequence[ToolSpec] = (), *,
                        history_prefix: Sequence[Message] | None = None) -> RuleRequestTicket: ...
    def confirm_response(self, ticket: RuleRequestTicket,
                         response_message_id: str | None = None) -> None: ...
    def reject_unsent_request(self, ticket: RuleRequestTicket) -> None: ...
    def checkpoint(self, history: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> dict: ...
    def prepare_restore(self, value: object, history: Sequence[Message],
                        tools: Sequence[ToolSpec] = ()) -> Callable[[], None]: ...


@dataclass(frozen=True)
class RuleAccessDecision:
    allowed: bool
    reason_code: str
    pending: tuple[tuple[str, str], ...] = ()


class ToolRuleGuard(Protocol):
    @property
    def registered_message_ids(self) -> frozenset[str]: ...
    def check(self, call: ToolCall) -> RuleAccessDecision: ...
    def close_batch(self, history: Sequence[Message]) -> tuple[Message, ...]: ...
    def prepare_request(self, messages: Sequence[Message],
                        tools: Sequence[ToolSpec] = (), *,
                        history_prefix: Sequence[Message] | None = None) -> RuleRequestTicket: ...
    def confirm_response(self, ticket: RuleRequestTicket,
                         response_message_id: str | None = None) -> None: ...
    def reject_unsent_request(self, ticket: RuleRequestTicket) -> None: ...
    def checkpoint(self, history: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> dict: ...
    def restore(self, value: object, history: Sequence[Message],
                tools: Sequence[ToolSpec] = ()) -> None: ...


class RuleSearchValidation(Protocol):
    """Trusted paired search executor validates here and gates each file itself."""

    def validate_call(self, call: ToolCall) -> None: ...


class RuleSearchRuntime(RuleSearchValidation, Protocol):
    @property
    def run_id(self) -> str: ...
    def checkpoint(self, history: Sequence[Message]) -> dict: ...
    def prepare_restore(self, value: object,
                        history: Sequence[Message]) -> Callable[[], None]: ...


class RuleAccessGate:
    """No source operation or permission change is performed by this gate.

    The host injects the permitted non-source tool catalog explicitly. Unknown
    tools fail closed rather than inferring safety from names or descriptions.
    """

    def __init__(self, planner: ToolAccessPlanner, selector: RuleSelector,
                 delivery: RuleDelivery, *, resolver: AccessPathResolver | None = None,
                 non_source_tools: frozenset[str] = frozenset(),
                 search_validator: RuleSearchRuntime | None = None,
                 source_validators: Mapping[str, RuleSearchValidation] | None = None,
                 run_id: str | None = None) -> None:
        source_tools = {"read_file", "apply_patch", "run_tests", "start_test_job",
                        "search_code", "get_git_diff", "mcp_serena_symbols",
                        "mcp_serena_find_symbol", "mcp_serena_references"}
        if source_tools & non_source_tools:
            raise RuleError("rules_source_tool_cannot_bypass_gate")
        self.planner, self.selector, self.delivery = planner, selector, delivery
        self.resolver, self.non_source_tools = resolver, non_source_tools
        self.search_validator = search_validator
        source_validators = dict(source_validators or {})
        if set(source_validators) - {"mcp_serena_symbols", "mcp_serena_find_symbol",
                                     "mcp_serena_references"}:
            raise RuleError("rules_source_validator_not_supported")
        self.source_validators = MappingProxyType(source_validators)
        if run_id is not None and (not isinstance(run_id, str) or not 1 <= len(run_id) <= 200):
            raise RuleError("rules_run_identity_invalid")
        self.run_id = run_id

    def _checkpoint_identity(self) -> None:
        if self.run_id is None:
            raise RuleError("rules_checkpoint_run_identity_required")
        if self.search_validator is not None and self.search_validator.run_id != self.run_id:
            raise RuleError("rules_search_run_identity_mismatch")

    @property
    def registered_message_ids(self) -> frozenset[str]:
        return self.delivery.registered_message_ids

    def checkpoint(self, history: Sequence[Message], tools: Sequence[ToolSpec] = ()) -> dict:
        self._checkpoint_identity()
        payload = {"run_id": self.run_id, "delivery": self.delivery.checkpoint(history, tools),
                   "search": (self.search_validator.checkpoint(history)
                              if self.search_validator is not None else None)}
        return {"schema_version": 1, "payload": payload, "payload_sha256": digest(payload)}

    def restore(self, value: object, history: Sequence[Message],
                tools: Sequence[ToolSpec] = ()) -> None:
        from tracefix.repository_rules.runtime_state import decode_runtime_checkpoint

        self._checkpoint_identity()
        try:
            payload = decode_runtime_checkpoint(value, run_id=self.run_id,
                                                 search_enabled=self.search_validator is not None)
            publish_delivery = self.delivery.prepare_restore(payload["delivery"], history, tools)
            publish_search = (self.search_validator.prepare_restore(payload["search"], history)
                              if self.search_validator is not None else None)
            # Both components finish all checks before synchronous publication.
            publish_delivery()
            if publish_search is not None:
                publish_search()
        except (TypeError, ValueError, RecursionError) as exc:
            if isinstance(exc, RuleError):
                raise
            raise RuleError("rules_runtime_checkpoint_malformed") from exc

    def close_batch(self, history: Sequence[Message]) -> tuple[Message, ...]:
        return self.delivery.close_batch(history)

    def prepare_request(self, messages: Sequence[Message], tools: Sequence[ToolSpec] = (), *,
                        history_prefix: Sequence[Message] | None = None):
        return self.delivery.prepare_request(messages, tools, history_prefix=history_prefix)

    def confirm_response(self, ticket: RuleRequestTicket,
                         response_message_id: str | None = None) -> None:
        self.delivery.confirm_response(ticket, response_message_id)

    def reject_unsent_request(self, ticket: RuleRequestTicket) -> None:
        self.delivery.reject_unsent_request(ticket)

    def check(self, call: ToolCall) -> RuleAccessDecision:
        try:
            return self._check(call)
        except (ValidationError, ToolValidationError) as exc:
            raise RuleError("rules_tool_or_selection_invalid") from exc

    def _check(self, call: ToolCall) -> RuleAccessDecision:
        if call.name in self.non_source_tools:
            return RuleAccessDecision(True, "rules_no_source_access")
        if call.name == "search_code" and self.search_validator is not None:
            self.search_validator.validate_call(call)
            return RuleAccessDecision(True, "rules_per_candidate_search")
        if call.name in self.source_validators:
            self.source_validators[call.name].validate_call(call)
            return RuleAccessDecision(True, "rules_mcp_source_output_gated")
        plan = self.planner.plan(call)
        if self.resolver is not None:
            paths = tuple(self.resolver.resolve(plan))
            paths += tuple(access.path for access in plan.paths if access.purpose == "config")
        elif plan.enumeration == "none":
            paths = tuple(access.path for access in plan.paths)
        else:
            raise RuleError("rules_metadata_resolver_required")
        if len(paths) > 5000:
            raise RuleError("rules_access_path_limit")
        if plan.enumeration == "none" and not {item.path for item in plan.paths} <= set(paths):
            raise RuleError("rules_metadata_required_path_missing")
        # Validate every selection first. A corrupt later scope cannot publish
        # an earlier partial set as though the complete call had been checked.
        selections, selection_bytes = [], 0
        for path in dict.fromkeys(paths):
            target = relative_path(path)
            selection = RuleSelection.model_validate(self.selector.select(target))
            if selection.target_path != target:
                raise RuleError("rules_selection_target_mismatch")
            selection_bytes += len(selection.model_dump_json().encode("utf-8"))
            if selection_bytes > MAX_ACCESS_SELECTION_BYTES:
                raise RuleError("rules_access_selection_limit")
            selections.append(selection)
        pending = tuple(item for item in selections if not self.delivery.is_delivered(item))
        for selection in pending:
            self.delivery.defer(selection)
        return RuleAccessDecision(not pending, "rules_pending" if pending else "rules_delivered",
                                  tuple((item.target_path, item.sha256) for item in pending))
