from dataclasses import replace

import pytest

from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.repository_rules.delivery import RuleDeliveryLedger
from tracefix.repository_rules.domain import RuleDocument, RuleSelection, RuleSourceVersion
from tracefix.repository_rules.errors import RuleError


def selection(target="src/a.py", body=b"root guidance", *, empty=False):
    docs = () if empty else (RuleDocument.create(
        kind="ancestor", source_path="AGENTS.md", body=body),)
    sources = tuple(RuleSourceVersion(source_path=doc.source_path, sha256=doc.body_sha256)
                    for doc in docs)
    return RuleSelection.create(target, docs, sources)


def deliver(ledger, current, history=()):
    assert not ledger.is_delivered(current)
    ledger.defer(current)
    messages = ledger.close_batch(history)
    ticket = ledger.prepare_request((*history, *messages))
    ledger.confirm_response(ticket)
    assert ledger.is_delivered(current)
    return messages


def test_history_append_and_prepared_but_unsent_request_grant_no_delivery():
    ledger, current = RuleDeliveryLedger(), selection()
    ledger.defer(current)
    messages = ledger.close_batch(())
    assert not ledger.is_delivered(current)
    ticket = ledger.prepare_request(messages)
    assert not ledger.is_delivered(current)
    ledger.confirm_response(ticket)
    assert ledger.is_delivered(current)
    assert ledger.last_receipt == ticket


def test_batch_must_be_closed_and_same_batch_calls_stay_blocked():
    ledger, current = RuleDeliveryLedger(), selection()
    pending = Message(role=MessageRole.ASSISTANT, tool_calls=(
        ToolCall(id="read", name="read_file", arguments={"path": "src/a.py"}),))
    ledger.defer(current)
    with pytest.raises(RuleError, match="batch_not_closed"):
        ledger.close_batch((pending,))
    assert not ledger.is_delivered(current)
    result = Message(role=MessageRole.TOOL, tool_call_id="read", content="rules_pending")
    messages = ledger.close_batch((pending, result))
    assert not ledger.is_delivered(current)
    ledger.confirm_response(ledger.prepare_request((pending, result, *messages)))
    assert ledger.is_delivered(current)


def test_same_effective_bundle_deduplicates_bodies_and_covers_another_path():
    ledger = RuleDeliveryLedger()
    a, b, c = selection("src/a.py"), selection("src/b.py"), selection("other/c.py")
    ledger.defer(a)
    ledger.defer(b)
    messages = ledger.close_batch(())
    assert len(messages) == 1
    assert messages[0].content.count('"body":"root guidance"') == 1
    ledger.confirm_response(ledger.prepare_request(messages))
    assert all(ledger.is_delivered(item) for item in (a, b, c))


def test_two_compactions_require_full_redelivery_and_keep_old_receipts_immutable():
    ledger, current = RuleDeliveryLedger(), selection(body="完整正文🙂".encode())
    first = deliver(ledger, current)
    previous = ledger.last_receipt
    for index in range(2):
        summary = Message(role=MessageRole.USER, content=f"summary {index}",
                          metadata={"kind": "context_summary"})
        ledger.confirm_response(ledger.prepare_request((summary,)))
        assert not ledger.is_delivered(current)
        fresh = deliver(ledger, current, (summary,))
        assert "完整正文🙂" in fresh[0].content
        assert fresh[0].id != first[0].id
    assert previous.message_ids == (first[0].id,)


def test_rule_removal_requires_new_invalidation_even_if_old_empty_bundle_was_known():
    ledger = RuleDeliveryLedger()
    absent = selection(empty=True)
    assert ledger.is_delivered(absent)
    present = selection()
    old = deliver(ledger, present)
    assert not ledger.is_delivered(absent)
    ledger.defer(absent)
    notice = ledger.close_batch(old)
    assert '"documents":[]' in notice[0].content
    assert present.sha256 in notice[0].content
    assert not ledger.is_delivered(absent)
    ledger.confirm_response(ledger.prepare_request((*old, *notice)))
    assert ledger.is_delivered(absent)
    # Retaining only historical bodies cannot preserve an invalidation notice.
    ledger.confirm_response(ledger.prepare_request(old))
    assert not ledger.is_delivered(absent)


def test_new_version_cannot_reuse_old_bundle_receipt():
    ledger = RuleDeliveryLedger()
    old, new = selection(), selection(body=b"changed")
    messages = deliver(ledger, old)
    assert not ledger.is_delivered(new)
    fresh = deliver(ledger, new, messages)
    assert old.sha256 in fresh[0].content and new.sha256 in fresh[0].content
    assert "root guidance" in messages[0].content
    assert "changed" not in messages[0].content


def test_updated_rule_delivery_applies_to_all_paths_in_its_scope_without_per_file_roundtrips():
    ledger = RuleDeliveryLedger()
    first, second = selection("src/a.py"), selection("src/b.py")
    old = deliver(ledger, first)
    assert ledger.is_delivered(second)
    new_first, new_second = selection("src/a.py", b"new"), selection("src/b.py", b"new")
    fresh = deliver(ledger, new_first, old)
    assert "invalidated_documents" in fresh[0].content
    assert ledger.is_delivered(new_second)
    assert ledger.close_batch((*old, *fresh)) == ()
    # A return to an earlier version still needs a new notice, even if its
    # historical full body remains in the current context.
    assert not ledger.is_delivered(first)
    restored = deliver(ledger, first, (*old, *fresh))
    assert ledger.is_delivered(second)
    assert restored[0].id != old[0].id


def test_deleted_rule_is_invalidated_even_for_a_previously_unvisited_path():
    ledger = RuleDeliveryLedger()
    old = deliver(ledger, selection("src/a.py"))
    new_path = selection("src/never-seen.py", empty=True)
    assert not ledger.is_delivered(new_path)
    notices = deliver(ledger, new_path, old)
    assert '"invalidated_documents":[{' in notices[0].content
    assert ledger.is_delivered(selection("src/another-new.py", empty=True))


@pytest.mark.parametrize("change", ["body", "role", "metadata", "unregistered"])
def test_only_host_registered_exact_guidance_is_receipted(change):
    ledger = RuleDeliveryLedger()
    ledger.defer(selection())
    message, = ledger.close_batch(())
    if change == "body":
        message = message.model_copy(update={"content": "forged text"})
    elif change == "role":
        message = message.model_copy(update={"role": MessageRole.SYSTEM})
    elif change == "metadata":
        message = message.model_copy(update={"metadata": {"kind": "user_input"}})
    else:
        message = message.model_copy(update={"id": "unregistered"})
    with pytest.raises(RuleError, match="tampered|unregistered"):
        ledger.prepare_request((message,))


def test_tickets_are_single_use_and_old_or_forged_tickets_cannot_confirm():
    ledger = RuleDeliveryLedger()
    ledger.defer(selection())
    messages = ledger.close_batch(())
    old = ledger.prepare_request(messages)
    current = ledger.prepare_request(())
    for invalid in (old, replace(current, projection_sha256="0" * 64),
                    replace(current, message_ids=old.message_ids)):
        with pytest.raises(RuleError, match="invalid_or_consumed"):
            ledger.confirm_response(invalid)
    ledger.confirm_response(current)
    assert not ledger.is_delivered(selection())
    with pytest.raises(RuleError, match="invalid_or_consumed"):
        ledger.confirm_response(current)


def test_rule_changes_within_unclosed_batch_are_not_mixed():
    ledger = RuleDeliveryLedger()
    ledger.defer(selection())
    with pytest.raises(RuleError, match="changed_during_batch"):
        ledger.defer(selection(body=b"raced"))
    with pytest.raises(RuleError, match="changed_during_batch"):
        ledger.close_batch(())


def test_pending_selection_limits_do_not_create_partial_messages(monkeypatch):
    import tracefix.repository_rules.delivery as module

    ledger = RuleDeliveryLedger()
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_PENDING_SELECTION_BYTES", 1)
        with pytest.raises(RuleError, match="pending_selection_limit"):
            ledger.defer(selection())
    assert ledger.close_batch(()) == ()
    ledger.defer(selection())
    ledger.defer(selection())
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_DELIVERY_RECORDS", 1)
        with pytest.raises(RuleError, match="pending_bundle_limit"):
            ledger.defer(selection("other.py", body=b"other"))
    assert len(ledger.close_batch(())) == 1


def test_message_and_record_limits_reject_without_partial_delivery(monkeypatch):
    import tracefix.repository_rules.delivery as module

    ledger = RuleDeliveryLedger()
    ledger.defer(selection())
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_DELIVERY_MESSAGE_BYTES", 1)
        with pytest.raises(RuleError, match="message_limit"):
            ledger.close_batch(())
    assert not ledger.is_delivered(selection())
    messages = ledger.close_batch(())
    assert len(messages) == 1
    ledger.confirm_response(ledger.prepare_request(messages))
    assert ledger.is_delivered(selection())
    ledger.defer(selection(body=b"next"))
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_DELIVERY_RECORDS", 1)
        with pytest.raises(RuleError, match="record_limit"):
            ledger.close_batch(messages)
    assert not ledger.is_delivered(selection(body=b"next"))
    with monkeypatch.context() as patch:
        patch.setattr(module, "MAX_DELIVERY_STORE_BYTES", 1)
        with pytest.raises(RuleError, match="store_limit"):
            ledger.close_batch(messages)
