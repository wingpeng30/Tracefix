"""Pure validation of the single host-owned rules checkpoint envelope."""

from __future__ import annotations

import json
from collections.abc import Sequence

from tracefix.messages import Message
from tracefix.repository_rules.domain import digest
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.search import MAX_SEARCH_CURSORS
from tracefix.repository_rules.search_state import validate_search
from tracefix.repository_rules.state import validate_delivery
from tracefix.tools.base import ToolSpec


def decode_runtime_checkpoint(value: object, *, run_id: str, search_enabled: bool) -> dict:
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > 18 * 1024 * 1024:
            raise RuleError("rules_runtime_checkpoint_size_limit")
        frozen = json.loads(raw)
        if (not isinstance(frozen, dict)
                or set(frozen) != {"schema_version", "payload", "payload_sha256"}
                or type(frozen["schema_version"]) is not int or frozen["schema_version"] != 1
                or not isinstance(frozen["payload"], dict)
                or set(frozen["payload"]) != {"run_id", "delivery", "search"}
                or frozen["payload_sha256"] != digest(frozen["payload"])
                or frozen["payload"]["run_id"] != run_id):
            raise RuleError("rules_runtime_checkpoint_identity_invalid")
        payload = frozen["payload"]
        if (payload["search"] is not None) != search_enabled:
            raise RuleError("rules_runtime_checkpoint_search_mode_invalid")
        return payload
    except (TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, RuleError):
            raise
        raise RuleError("rules_runtime_checkpoint_malformed") from exc


def validate_runtime_checkpoint(value: object, history: Sequence[Message],
                                tools: Sequence[ToolSpec], *, run_id: str) -> frozenset[str]:
    """Inspection needs no source checkout, worker, MCP service or model."""
    payload = decode_runtime_checkpoint(value, run_id=run_id, search_enabled=True)
    delivery = validate_delivery(payload["delivery"], history, tools)
    validate_search(payload["search"], history, run_id=run_id, cursor_limit=MAX_SEARCH_CURSORS)
    return frozenset(record.message.id for record in delivery.records)
