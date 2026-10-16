import pytest

from tracefix.messages import ToolCall
from tracefix.repository_rules.delivery import RuleDeliveryLedger
from tracefix.repository_rules.domain import RuleDocument, RuleSelection, RuleSourceVersion
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.search import PagedRepositorySearch


def call(cursor=None, **args):
    return ToolCall(id="search-contract", name="search_code",
                    arguments={"query": "needle", **args, **({"cursor": cursor} if cursor else {})})


class Source:
    def __init__(self, files):
        self.files, self.reads = files, []

    def read_bytes(self, path):
        self.reads.append(path)
        value = self.files[path]
        if isinstance(value, Exception):
            raise value
        return value


class Selector:
    def __init__(self):
        self.bodies, self.selected = {}, []

    def select(self, path):
        self.selected.append(path)
        body = self.bodies.get(path)
        if isinstance(body, Exception):
            raise body
        docs = () if body is None else (RuleDocument.create(
            kind="ancestor", source_path="AGENTS.md", body=body),)
        return RuleSelection.create(path, docs, tuple(RuleSourceVersion(
            source_path=doc.source_path, sha256=doc.body_sha256) for doc in docs))


def assemble(files, paths=None, run="run-a"):
    source, selector, ledger = Source(files), Selector(), RuleDeliveryLedger()
    candidates = list(files) if paths is None else paths
    search = PagedRepositorySearch(run, candidates=lambda args: candidates, source=source,
                                   selector=selector, delivery=ledger)
    return search, source, selector, ledger


def confirm_pending(ledger):
    messages = ledger.close_batch(())
    ledger.confirm_response(ledger.prepare_request(messages))
    return messages


def test_stops_at_first_unseen_scope_and_keeps_prior_safe_results():
    search, source, selector, ledger = assemble({
        "a.py": b"needle safe", "b.py": b"needle second", "c.py": b"needle third"})
    selector.bodies.update({"b.py": b"rules b", "c.py": b"rules c"})
    page = search.search(call())
    assert [hit["path"] for hit in page["matches"]] == ["a.py"]
    assert source.reads == ["a.py"] and selector.selected == ["a.py", "b.py"]
    assert page["rules_pending"] and page["pending_path"] == "b.py" and not page["eof"]
    assert page["cursor"].startswith("rules_cursor_") and "b.py" not in page["cursor"]
    confirm_pending(ledger)
    second = search.search(call(page["cursor"]))
    assert [hit["path"] for hit in second["matches"]] == ["b.py"]
    assert second["pending_path"] == "c.py" and source.reads == ["a.py", "b.py"]
    confirm_pending(ledger)
    third = search.search(call(second["cursor"]))
    assert [hit["path"] for hit in third["matches"]] == ["c.py"]
    assert third["eof"] and third["cursor"] is None


def test_result_pages_resume_within_file_without_dropping_middle_matches():
    search, source, selector, ledger = assemble({"a.py": b"needle 1\nneedle 2\nneedle 3"})
    first = search.search(call(max_results=1))
    second = search.search(call(first["cursor"], max_results=1))
    third = search.search(call(second["cursor"], max_results=1))
    assert [page["matches"][0]["line"] for page in (first, second, third)] == [1, 2, 3]
    assert third["eof"] and third["cursor"] is None
    assert source.reads == ["a.py"] * 3


@pytest.mark.parametrize("change", ["added", "removed", "order", "rules", "source"])
def test_cursor_rejects_changed_candidates_rules_and_partial_file_content(change):
    paths = ["a.py", "b.py"]
    search, source, selector, ledger = assemble({"a.py": b"needle\nneedle", "b.py": b"needle"},
                                               paths=paths)
    first = search.search(call(max_results=1))
    if change == "added":
        paths.append("c.py")
    elif change == "removed":
        paths.pop()
    elif change == "order":
        paths.reverse()
    elif change == "rules":
        selector.bodies["a.py"] = b"changed rules"
    else:
        source.files["a.py"] = b"needle\nchanged"
    with pytest.raises(RuleError, match="candidates_changed|rules_changed|source_changed"):
        search.search(call(first["cursor"], max_results=1))


def test_partial_file_becoming_oversize_is_stale_and_not_silently_skipped():
    search, source, selector, ledger = assemble({"a.py": b"needle\nneedle"})
    first = search.search(call(max_results=1))
    source.files["a.py"] = RuleError("rules_source_limit_exceeded")
    with pytest.raises(RuleError, match="cursor_source_changed"):
        search.search(call(first["cursor"], max_results=1))


@pytest.mark.parametrize("args", [
    {"query": "other"}, {"path": "src"}, {"glob": "*.py"},
    {"case_sensitive": True}, {"max_results": 2},
])
def test_cursor_is_bound_to_the_complete_query(args):
    search, source, selector, ledger = assemble({"a.py": b"needle\nneedle"})
    first = search.search(call(max_results=1))
    with pytest.raises(RuleError, match="unknown_or_mismatched"):
        search.search(call(first["cursor"], **{**{"max_results": 1}, **args}))


def test_other_run_and_host_paths_cannot_be_used_as_cursors():
    search, source, selector, ledger = assemble({"a.py": b"needle\nneedle"})
    first = search.search(call(max_results=1))
    other, *_ = assemble({"a.py": b"needle\nneedle"}, run="run-b")
    with pytest.raises(RuleError, match="unknown_or_mismatched"):
        other.search(call(first["cursor"], max_results=1))
    with pytest.raises(RuleError, match="arguments_invalid"):
        search.search(call("D:/host/log"))
    with pytest.raises(RuleError, match="arguments_invalid"):
        search.search(call(position=0))


def test_compacted_away_rules_defer_again_without_reading_partial_source():
    search, source, selector, ledger = assemble({"a.py": b"needle\nneedle"})
    selector.bodies["a.py"] = b"required rule"
    pending = search.search(call(max_results=1))
    confirm_pending(ledger)
    first = search.search(call(pending["cursor"], max_results=1))
    ledger.confirm_response(ledger.prepare_request(()))
    deferred = search.search(call(first["cursor"], max_results=1))
    assert deferred["rules_pending"] and deferred["matches"] == []
    assert source.reads == ["a.py"]
    confirm_pending(ledger)
    last = search.search(call(deferred["cursor"], max_results=1))
    assert last["matches"][0]["line"] == 2 and last["eof"]


def test_unicode_bad_bytes_binary_and_literal_queries_are_bounded():
    search, source, selector, ledger = assemble({
        "binary.py": b"needle\0hidden",
        "a.py": "中文🙂 needle\n.+ literal\n".encode() + b"bad\xff"})
    page = search.search(call())
    assert page["matches"][0]["column"] == 5
    assert page["searched_files"] == 1 and page["eof"]
    literal = search.search(call(query=".+"))
    assert [hit["line"] for hit in literal["matches"]] == [2]
    bad = search.search(call(query="bad"))
    assert bad["matches"][0]["text"] == "bad�"


def test_clipped_lines_and_oversize_skips_do_not_claim_complete_untruncated_output():
    search, source, selector, ledger = assemble({
        "large.py": RuleError("rules_source_limit_exceeded"),
        "long.py": b"needle" + b"x" * 1000})
    page = search.search(call())
    assert page["skipped_files"] == 1
    assert page["eof"] and page["truncated"] and len(page["matches"][0]["text"]) == 500


@pytest.mark.parametrize("value", [None, "not bytes", b"x" * 1_000_001],
                         ids=["missing", "wrong-type", "too-big"])
def test_source_port_failures_are_explicit(value):
    search, *_ = assemble({"a.py": value})
    with pytest.raises(RuleError, match="source_changed|port_invalid"):
        search.search(call())


def test_corrupt_rule_scope_is_an_error_and_does_not_read_its_body():
    search, source, selector, ledger = assemble({"a.py": b"needle"})
    selector.bodies["a.py"] = RuleError("rules_configuration_invalid")
    with pytest.raises(RuleError, match="configuration_invalid"):
        search.search(call())
    assert not source.reads


def test_duplicate_unsafe_or_unbounded_candidates_fail_before_content_reads():
    for paths in (["a.py", "a.py"], ["../secret"], ["a.py"] * 5001):
        search, source, *_ = assemble({"a.py": b"needle"}, paths=paths)
        with pytest.raises(RuleError):
            search.search(call())
        assert not source.reads


def test_cursor_quota_is_not_refreshed_by_repeated_pages(monkeypatch):
    import tracefix.repository_rules.search as module

    search, *_ = assemble({"a.py": b"needle\nneedle\nneedle"})
    monkeypatch.setattr(module, "MAX_SEARCH_CURSORS", 1)
    first = search.search(call(max_results=1))
    with pytest.raises(RuleError, match="cursor_limit"):
        search.search(call(first["cursor"], max_results=1))


@pytest.mark.parametrize("args", [
    {"path": "../outside"}, {"glob": "[ab].py"}, {"path": "elsewhere"}, {"glob": "*.md"},
])
def test_invalid_query_and_out_of_scope_candidate_are_rejected_before_body_reads(args):
    search, source, *_ = assemble({"a.py": b"needle"})
    with pytest.raises(RuleError):
        search.search(call(**args))
    assert not source.reads


def test_corrupt_selection_payload_does_not_reach_a_source_read():
    search, source, selector, ledger = assemble({"a.py": b"needle"})
    original = selector.select
    selector.select = lambda path: original(path).model_copy(update={"sha256": "0" * 64})
    with pytest.raises(RuleError, match="selection_invalid"):
        search.search(call())
    assert not source.reads
