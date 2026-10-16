import copy
from types import SimpleNamespace

import pytest

from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.repository_rules.delivery import RuleDeliveryLedger
from tracefix.repository_rules.domain import RuleSelection, digest
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.search import PagedRepositorySearch
from tracefix.tools.repository_search import RepositorySearchTool


def assemble(run="run-a", *, forbidden=False):
    def nope(*args, **kwargs):
        pytest.fail("restoration consulted live source or rules")

    return PagedRepositorySearch(run, candidates=nope if forbidden else lambda args: ("a.py",),
        source=SimpleNamespace(read_bytes=nope if forbidden else lambda path: b"needle\nneedle"),
        selector=SimpleNamespace(select=nope if forbidden else lambda target:
            RuleSelection.create(target, (), ())), delivery=RuleDeliveryLedger())


def fixture_state():
    search = assemble()
    tool = RepositorySearchTool(search)
    request = ToolCall(id="search", name="search_code",
                       arguments={"query": "needle", "max_results": 1})
    result = tool.execute(request)
    history = (Message(role=MessageRole.ASSISTANT, tool_calls=(request,)),
               Message(role=MessageRole.TOOL, tool_call_id=request.id,
                       content=result.model_dump_json()))
    saved = search.checkpoint(history)
    return search, result.output["cursor"], history, saved


def rehash(saved):
    saved["payload_sha256"] = digest(saved["payload"])
    return saved


def test_cursor_restoration_does_not_enumerate_or_read_live_files():
    search, cursor, history, saved = fixture_state()
    restored = assemble(forbidden=True)
    restored.restore(saved, history)
    assert restored.checkpoint(history) == saved
    assert restored.cursor_fingerprint(cursor) == search.cursor_fingerprint(cursor)


def test_restored_cursor_continues_at_middle_of_file_and_old_quota_is_preserved(monkeypatch):
    import tracefix.repository_rules.search as module

    search, cursor, history, saved = fixture_state()
    restored = assemble()
    restored.restore(saved, history)
    page = restored.search(ToolCall(id="resumed", name="search_code", arguments={
        "query": "needle", "max_results": 1, "cursor": cursor}))
    assert page["matches"][0]["line"] == 2 and page["eof"]
    # The saved limit is itself part of the identity; it cannot be interpreted
    # under a different quota or silently replenished during restoration.
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_SEARCH_CURSORS", 1)
        with pytest.raises(RuleError, match="identity_mismatch"):
            assemble().restore(saved, history)


@pytest.mark.parametrize("field", [
    "version", "digest", "run", "limit", "cursor-run", "query", "candidate", "rules",
    "position", "line", "file", "count", "missing", "extra", "unknown-field",
])
def test_tampered_cursor_state_or_quota_is_rejected_atomically(field):
    search, cursor, history, saved = fixture_state()
    bad = copy.deepcopy(saved)
    state = bad["payload"]["cursors"][cursor]
    if field == "version":
        bad["schema_version"] = 2
    elif field == "digest":
        bad["payload_sha256"] = "0" * 64
    elif field == "run":
        bad["payload"]["run_id"] = "other"
    elif field == "limit":
        bad["payload"]["limit"] += 1
    elif field == "cursor-run":
        state["run_id"] = "other"
    elif field in {"query", "candidate", "rules"}:
        key = {"query": "query_sha256", "candidate": "candidates_sha256",
               "rules": "rules_sha256"}[field]
        state[key] = "0" * 64
    elif field == "position":
        state["position"] = 2
    elif field == "line":
        state["line_offset"] = 2
    elif field == "file":
        state["file_sha256"] = None
    elif field == "count":
        state["prefix_count"] = True
    elif field == "missing":
        bad["payload"]["cursors"] = {}
    elif field == "extra":
        bad["payload"]["cursors"]["rules_cursor_" + "0" * 32] = state
    else:
        state["host_path"] = "D:/outside"
    if field != "digest":
        rehash(bad)
    with pytest.raises(RuleError):
        search.restore(bad, history)
    assert search.checkpoint(history) == saved


@pytest.mark.parametrize("field", ["proof", "continuation", "query", "role", "missing"])
def test_cursor_must_reference_the_exact_committed_call_and_result(field):
    search, cursor, history, saved = fixture_state()
    import json

    result = json.loads(history[1].content)
    if field == "proof":
        result["metadata"]["repository_search_cursor_sha256"] = "0" * 64
    elif field == "continuation":
        result["output"]["continuation"]["arguments"]["query"] = "other"
    elif field == "query":
        call = history[0].tool_calls[0].model_copy(update={"arguments": {"query": "other"}})
        history = (history[0].model_copy(update={"tool_calls": (call,)}), history[1])
    elif field == "role":
        result["tool_name"] = "read_file"
    else:
        history = ()
    if history:
        history = (history[0], history[1].model_copy(update={"content": json.dumps(result)}))
    with pytest.raises(RuleError):
        assemble().restore(saved, history)


def test_different_run_or_checkpoint_size_and_prepared_restore_do_not_mutate(monkeypatch):
    import tracefix.repository_rules.search_state as module

    search, cursor, history, saved = fixture_state()
    with pytest.raises(RuleError, match="identity_mismatch"):
        assemble("run-b").restore(saved, history)
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_SEARCH_CHECKPOINT_BYTES", 1)
        with pytest.raises(RuleError, match="size_limit"):
            search.restore(saved, history)
    restored = assemble(forbidden=True)
    commit = restored.prepare_restore(saved, history)
    with pytest.raises(RuleError, match="unknown_or_mismatched"):
        restored.cursor_fingerprint(cursor)
    commit()
    assert restored.checkpoint(history) == saved
