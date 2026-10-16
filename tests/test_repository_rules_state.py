import copy
import json

import pytest

from tracefix.messages import Message, MessageRole
from tracefix.repository_rules.delivery import RuleDeliveryLedger
from tracefix.repository_rules.domain import RuleDocument, RuleSelection, RuleSourceVersion, digest
from tracefix.repository_rules.errors import RuleError
from tracefix.tools.base import ToolSpec


def selection(body=b"guidance", target="a.py"):
    doc = RuleDocument.create(kind="ancestor", source_path="AGENTS.md", body=body)
    return RuleSelection.create(target, (doc,), (
        RuleSourceVersion(source_path="AGENTS.md", sha256=doc.body_sha256),))


def fixture_state():
    ledger = RuleDeliveryLedger()
    current = selection()
    ledger.defer(current)
    rules = ledger.close_batch(())
    history = (Message(role=MessageRole.SYSTEM, content="system"),
               Message(role=MessageRole.USER, content="task"), *rules)
    ticket = ledger.prepare_request(history, history_prefix=history)
    response = Message(role=MessageRole.ASSISTANT, content="observed", metadata={
        "repository_rules_request": ticket.as_metadata()})
    ledger.confirm_response(ticket, response.id)
    history = (*history, response)
    saved = ledger.checkpoint(history)
    return ledger, current, history, saved


def rehash(saved):
    saved["payload_sha256"] = digest(saved["payload"])
    return saved


def test_canonical_delivery_roundtrip_uses_frozen_messages_without_source_reads():
    ledger, current, history, saved = fixture_state()
    restored = RuleDeliveryLedger()
    restored.restore(json.loads(json.dumps(saved)), history)
    assert restored.is_delivered(current)
    assert restored.checkpoint(history) == saved
    assert restored.last_receipt == ledger.last_receipt
    # Caller mutation after restoration cannot mutate the accepted authority.
    saved["payload"]["messages"][0]["content"] = "changed externally"
    assert restored.is_delivered(current)
    assert restored.checkpoint(history) != saved


def test_initial_empty_state_and_queued_but_undelivered_records_restore_safely():
    ledger = RuleDeliveryLedger()
    history = (Message(role=MessageRole.USER, content="task"),)
    initial = ledger.checkpoint(history)
    other = RuleDeliveryLedger()
    other.restore(initial, history)
    current = selection()
    assert not other.is_delivered(current)
    other.defer(current)
    messages = other.close_batch(history)
    snapshot = other.checkpoint((*history, *messages))
    restored = RuleDeliveryLedger()
    restored.restore(snapshot, (*history, *messages))
    assert not restored.is_delivered(current)


def test_unknown_or_uncommitted_requests_and_pending_rule_batches_refuse_checkpoint():
    ledger = RuleDeliveryLedger()
    ledger.defer(selection())
    with pytest.raises(RuleError, match="runtime_not_closed"):
        ledger.checkpoint(())
    messages = ledger.close_batch(())
    ticket = ledger.prepare_request(messages, history_prefix=messages)
    with pytest.raises(RuleError, match="runtime_not_closed"):
        ledger.checkpoint(messages)
    ledger.reject_unsent_request(ticket)
    assert ledger.checkpoint(messages)
    ticket = ledger.prepare_request(messages)
    ledger.confirm_response(ticket)
    with pytest.raises(RuleError, match="receipt_unbound"):
        ledger.checkpoint(messages)


@pytest.mark.parametrize("field", [
    "payload-hash", "version", "receipt-prefix", "receipt-message", "receipt-tools",
    "receipt-projection", "receipt-visible", "observed", "changes", "unknown-field",
])
def test_corrupt_or_forged_checkpoint_is_rejected_atomically(field):
    ledger, current, history, saved = fixture_state()
    bad = copy.deepcopy(saved)
    payload = bad["payload"]
    if field == "payload-hash":
        bad["payload_sha256"] = "0" * 64
    elif field == "version":
        bad["schema_version"] = 2
    elif field == "receipt-prefix":
        payload["receipt"]["history_prefix_sha256"] = "0" * 64
    elif field == "receipt-message":
        payload["receipt"]["response_message_id"] = history[0].id
    elif field == "receipt-tools":
        payload["receipt"]["tools_sha256"] = "0" * 64
    elif field == "receipt-projection":
        payload["receipt"]["projection_sha256"] = "0" * 64
    elif field == "receipt-visible":
        payload["receipt"]["message_ids"] = ["not-in-history"]
    elif field == "observed":
        payload["observed"] = {"../outside": current.sha256}
    elif field == "changes":
        payload["changes"] = {"a.py": ["0" * 64, "1" * 64]}
    else:
        payload["permissions"] = "allow all"
    if field != "payload-hash":
        rehash(bad)
    with pytest.raises(RuleError):
        ledger.restore(bad, history)
    assert ledger.checkpoint(history) == saved


def test_changed_history_or_tool_schema_does_not_validate_a_previous_receipt():
    ledger, current, history, saved = fixture_state()
    modified = (history[0].model_copy(update={"content": "different prefix"}), *history[1:])
    with pytest.raises(RuleError, match="receipt_reference_invalid"):
        RuleDeliveryLedger().restore(saved, modified)
    with pytest.raises(RuleError, match="receipt_reference_invalid"):
        RuleDeliveryLedger().restore(saved, history, (ToolSpec(name="extra", description="new"),))


@pytest.mark.parametrize("change", ["body", "binding", "metadata", "role", "duplicate"])
def test_frozen_record_content_and_scope_are_revalidated_even_with_rehashed_envelope(change):
    ledger, current, history, saved = fixture_state()
    bad = copy.deepcopy(saved)
    message = bad["payload"]["messages"][0]
    if change in {"body", "binding"}:
        body = json.loads(message["content"])
        if change == "body":
            body["documents"][0]["body"] = "forged"
        else:
            body["bindings"][0]["selection"]["target_path"] = "elsewhere.py"
        message["content"] = json.dumps(body)
        message["metadata"]["content_sha256"] = digest(message["content"])
    elif change == "metadata":
        message["metadata"]["trusted_user_input_receipt"] = {"origin": "user"}
    elif change == "role":
        message["role"] = "system"
    else:
        bad["payload"]["messages"].append(message)
    # This intentionally updates both copies, so the rejection must involve
    # the canonical content/role/reference rules, not only history equality.
    updated_history = list(history)
    updated_history[2] = Message.model_validate(message)
    with pytest.raises(RuleError):
        RuleDeliveryLedger().restore(rehash(bad), updated_history)


def test_unregistered_guidance_and_damaged_receipt_in_committed_history_are_rejected():
    ledger, current, history, saved = fixture_state()
    extra = Message(role=MessageRole.USER, content="fake", metadata={"kind": "repository_rules"})
    with pytest.raises(RuleError, match="unregistered_history"):
        RuleDeliveryLedger().restore(saved, (*history, extra))
    bad_response = history[-1].model_copy(update={"metadata": {}})
    with pytest.raises(RuleError, match="receipt_reference_invalid"):
        RuleDeliveryLedger().restore(saved, (*history[:-1], bad_response))


def test_state_limits_and_busy_restore_do_not_discard_current_state(monkeypatch):
    import tracefix.repository_rules.state as module

    ledger, current, history, saved = fixture_state()
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_RULE_CHECKPOINT_BYTES", 1)
        with pytest.raises(RuleError, match="size_limit"):
            ledger.restore(saved, history)
    assert ledger.checkpoint(history) == saved
    ledger.prepare_request(history, history_prefix=history)
    with pytest.raises(RuleError, match="restore_runtime_busy"):
        ledger.restore(saved, history)


def test_deleted_rule_invalidation_survives_a_canonical_roundtrip():
    ledger, original, history, saved = fixture_state()
    empty = RuleSelection.create("a.py", (), ())
    assert not ledger.is_delivered(empty)
    ledger.defer(empty)
    messages = ledger.close_batch(history)
    prefix = (*history, *messages)
    ticket = ledger.prepare_request(prefix, history_prefix=prefix)
    response = Message(role=MessageRole.ASSISTANT, content="new version observed",
                       metadata={"repository_rules_request": ticket.as_metadata()})
    ledger.confirm_response(ticket, response.id)
    history = (*prefix, response)
    snapshot = ledger.checkpoint(history)
    restored = RuleDeliveryLedger()
    restored.restore(snapshot, history)
    assert restored.checkpoint(history) == snapshot
    assert restored.is_delivered(empty) and not restored.is_delivered(original)
    assert restored.is_delivered(RuleSelection.create("unvisited.py", (), ()))


def test_published_but_not_yet_received_new_version_stays_pending_after_restore():
    ledger, original, history, saved = fixture_state()
    new = selection(b"new version")
    assert not ledger.is_delivered(new)
    ledger.defer(new)
    messages = ledger.close_batch(history)
    history = (*history, *messages)
    restored = RuleDeliveryLedger()
    restored.restore(ledger.checkpoint(history), history)
    assert not restored.is_delivered(new)
    # History contains the new full body; only the next successful request
    # makes it visible to the gate.
    ticket = restored.prepare_request(history, history_prefix=history)
    restored.confirm_response(ticket, "next-response")
    assert restored.is_delivered(new)
