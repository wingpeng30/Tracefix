import json
from types import SimpleNamespace

import pytest

from tracefix.repository_rules.delivery import RuleDeliveryLedger
from tracefix.repository_rules.domain import RuleDocument, RuleSelection, RuleSourceVersion
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.mcp_output import McpSourceGate, extract_serena_claims


def select(target, body=None):
    docs = () if body is None else (RuleDocument.create(
        kind="ancestor", source_path="AGENTS.md", body=body),)
    return RuleSelection.create(target, docs, tuple(RuleSourceVersion(
        source_path=doc.source_path, sha256=doc.body_sha256) for doc in docs))


def symbol(path="a.py", body="def f():\n    return 1"):
    return {"relative_path": path, "name_path": "f", "kind": "Function",
            "body_location": {"start_line": 0, "end_line": 1}, "body": body}


def filter_result(payload, *, current=None, source=None, method="find_symbol", args=None):
    ledger = RuleDeliveryLedger()
    gate = McpSourceGate(SimpleNamespace(select=current or select), ledger)
    text = json.dumps(payload)
    result = gate.filter(method, args or {}, text, source_files=("a.py", "b.py"),
                         source=source or SimpleNamespace(read_bytes=lambda path:
                             b"def f():\r\n    return 1\r\n"), source_sha256="a" * 64)
    return result, gate, ledger


@pytest.mark.parametrize("payload", [
    [symbol()], {"Function": [symbol()]},
    {"a.py": {"Function": [{key: value for key, value in symbol().items()
                            if key not in {"relative_path", "kind"}}]}},
], ids=["ungrouped", "kind-group", "file-kind-group"])
def test_pinned_serena_structured_shapes_have_verified_snapshot_paths(payload):
    result, *_ = filter_result(payload)
    assert not result["rules_pending"] and result["text"] == json.dumps(payload)
    assert result["source_refs"] == [{"path": "a.py", "snapshot_sha256": "a" * 64}]
    assert result["evidence_role"] == "untrusted_source_tool_result"


def test_new_scope_withholds_all_text_and_does_not_read_source_until_delivery():
    reads = []

    def source(path):
        reads.append(path)
        return b"def f():\n    return 1\n"

    payload = [symbol()]
    result, gate, ledger = filter_result(payload, current=lambda target: select(target, b"rules"),
                                         source=SimpleNamespace(read_bytes=source))
    assert result["rules_pending"] and result["text"] == "" and not reads
    assert "return 1" not in json.dumps(result)
    messages = ledger.close_batch(())
    ledger.confirm_response(ledger.prepare_request(messages))
    verified = gate.filter("find_symbol", {}, json.dumps(payload), source_files=("a.py",),
                            source=SimpleNamespace(read_bytes=source), source_sha256="b" * 64)
    assert not verified["rules_pending"] and reads == ["a.py"]


def test_overview_binds_its_explicit_source_file_and_empty_global_results_have_no_claims():
    claims = extract_serena_claims("get_symbols_overview", {"relative_path": "a.py"},
                                   '{"Function":["f"]}', ("a.py",))
    assert claims.paths == ("a.py",) and claims.snippets == ()
    assert extract_serena_claims("find_symbol", {}, "[]", ()).paths == ()


def test_children_inherit_parent_path_and_each_body_is_verified():
    child = {"name": "child", "body_location": {"start_line": 1, "end_line": 1},
             "body": "return 1"}
    payload = [{**symbol(), "children": {"Function": [child]}}]
    result, *_ = filter_result(payload)
    assert "child" in result["text"]
    child["body"] = "secret from another file"
    with pytest.raises(RuleError, match="body_source_mismatch"):
        filter_result(payload)


def test_reference_preview_lines_must_exist_in_the_claimed_file():
    payload = [{"relative_path": "a.py", "name_path": "f", "kind": "Function",
                "content_around_reference": "   0:def f():\n > 1:    return 1"}]
    result, *_ = filter_result(payload, method="find_referencing_symbols",
                               args={"relative_path": "b.py"})
    assert result["source_refs"][0]["path"] == "a.py"
    payload[0]["content_around_reference"] = "   2:secret from b.py"
    with pytest.raises(RuleError, match="reference_source_mismatch"):
        filter_result(payload, method="find_referencing_symbols")


def test_actual_pinned_reference_prefixes_zero_based_lines_and_virtual_eof_are_verified():
    payload = {"nested/use.py": {
        "File": [{"name_path": "use", "body_location": {"start_line": 0, "end_line": 5},
                  "content_around_reference": "  >   0:from widget import shared\n...   1:"}],
        "Function": [{"name_path": "caller", "body_location": {"start_line": 2, "end_line": 3},
                      "content_around_reference":
                          "...   2:def caller():\n  >   3:    return shared(2)\n...   4:"}],
    }}
    text = json.dumps(payload)
    gate = McpSourceGate(SimpleNamespace(select=select), RuleDeliveryLedger())
    source = SimpleNamespace(read_bytes=lambda path:
                             b"from widget import shared\n\ndef caller():\n    return shared(2)\n")
    result = gate.filter("find_referencing_symbols", {"relative_path": "widget.py"}, text,
                          source_files=("widget.py", "nested/use.py"), source=source,
                          source_sha256="a" * 64)
    assert result["text"] == text
    payload["nested/use.py"]["Function"][0]["content_around_reference"] = "...   1:def caller():"
    with pytest.raises(RuleError, match="reference_source_mismatch"):
        gate.filter("find_referencing_symbols", {}, json.dumps(payload),
                    source_files=("nested/use.py",), source=source, source_sha256="a" * 64)


@pytest.mark.parametrize("text", [
    "Shortened result:\n{}", "References without surrounding lines:\n{}",
    "not JSON", '"text"', "null", '{"Function":[{"name_path":"f"}]}',
    '[{"relative_path":"not-in-snapshot.py","name_path":"f"}]',
    '[{"relative_path":"../outside","name_path":"f"}]',
    '[{"relative_path":".env","name_path":"f"}]',
    '{"a.py":{"body":"UNVERIFIED SECRET","body":"safe"}}',
    '[{"relative_path":"a.py","name_path":NaN}]',
], ids=["shortened", "reference-shortened", "non-json", "string", "null", "missing-path",
        "outside-snapshot", "traversal", "secret-path", "duplicate-hidden-body", "nan"])
def test_unverifiable_output_never_becomes_deliverable_source(text):
    with pytest.raises(RuleError):
        extract_serena_claims("find_symbol", {}, text, ("a.py",))


@pytest.mark.parametrize("change", ["body", "range", "location-extra", "nested-name", "unknown"])
def test_wrong_source_claims_or_hidden_fields_are_rejected(change):
    payload = symbol()
    if change == "body":
        payload["body"] = "body that is absent"
    elif change == "range":
        payload["body_location"] = {"start_line": 0, "end_line": 50}
    elif change == "location-extra":
        payload["body_location"]["hidden"] = "unverified"
    elif change == "nested-name":
        payload["name_path"] = {"body": "hidden"}
    else:
        payload["info"] = "unsupported hover contents"
    with pytest.raises(RuleError):
        filter_result([payload])


@pytest.mark.parametrize("source", [None, b"\xff", "wrong type", b"x" * 1000001],
                         ids=["missing", "encoding", "type", "size"])
def test_source_validation_failures_are_explicit(source):
    with pytest.raises(RuleError):
        filter_result([symbol()], source=SimpleNamespace(read_bytes=lambda path: source))


def test_depth_node_and_path_limits_are_enforced():
    value = "name"
    for _ in range(40):
        value = [value]
    with pytest.raises(RuleError, match="structure_limit"):
        extract_serena_claims("get_symbols_overview", {"relative_path": "a.py"},
                              json.dumps(value), ("a.py",))
    with pytest.raises(RuleError, match="structure_limit"):
        extract_serena_claims("get_symbols_overview", {"relative_path": "a.py"},
                              json.dumps(["x"] * 2001), ("a.py",))
    paths = [f"p{index}.py" for index in range(129)]
    with pytest.raises(RuleError, match="path_limit"):
        extract_serena_claims("find_symbol", {}, json.dumps([
            {"relative_path": path, "name_path": "f"} for path in paths]), paths)


def test_invalid_method_oversize_and_source_identity_are_refused():
    for method, text in (("execute_shell", "[]"), ("find_symbol", " " * 16001)):
        with pytest.raises(RuleError):
            extract_serena_claims(method, {}, text, ())
    gate = McpSourceGate(SimpleNamespace(select=select), RuleDeliveryLedger())
    with pytest.raises(RuleError, match="identity_invalid"):
        gate.filter("find_symbol", {}, "[]", source_files=(), source=SimpleNamespace(),
                    source_sha256="untrusted")
