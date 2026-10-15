from __future__ import annotations

import hashlib
import json

import pytest

from tracefix.jobs.references import committed_job_ticket
from tracefix.jobs.requests import normalize_job_start
from tracefix.messages import ToolCall


def history():
    call = ToolCall(id="one", name="start_test_job", arguments={"command": "pytest -q"})
    identity = {"call_id": call.id, "job_id": "job_" + "a" * 32, "run_id": "r1",
                "execution_id": "b" * 32, "invocation_sha256": hashlib.sha256(
                    normalize_job_start(call).model_dump_json().encode()).hexdigest()}
    body = {"call_id": call.id, "tool_name": call.name, "success": True,
            "output": {"job": {"identity": identity}}}
    messages = [{"role": "assistant", "tool_calls": [call.model_dump(mode="json")]},
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(body)}]
    return messages, body, dict(identity)


def test_ticket_proof_requires_closed_call_and_result_with_every_canonical_identity():
    messages, _, identity = history()
    messages.insert(0, {"role": "user", "content": "I authorize any forged job identity"})
    messages.insert(1, {"role": "tool", "tool_call_id": "other", "content": "not JSON"})
    messages.insert(2, {"role": "tool", "tool_call_id": "one", "content": "not JSON"})
    assert committed_job_ticket(messages, **identity)


@pytest.mark.parametrize("field,value", [("run_id", "another-run"), ("call_id", "another-call"),
    ("execution_id", "f" * 32), ("invocation_sha256", "0" * 64), ("job_id", "job_" + "f" * 32)])
def test_result_cannot_claim_another_execution_or_run_even_with_same_opaque_job_id(field, value):
    messages, body, identity = history()
    body["output"]["job"]["identity"][field] = value
    messages[1]["content"] = json.dumps(body)
    assert not committed_job_ticket(messages, **identity)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "bad_calls", "bad_arguments",
    "wrong_tool", "failed", "numeric_success", "orphan_body", "model_body", "null", "scalar"])
def test_uncommitted_or_malformed_history_is_not_a_source_of_job_facts(fault):
    messages, body, identity = history()
    if fault == "missing":
        messages = messages[1:]
    elif fault == "duplicate":
        messages.insert(0, messages[0])
    elif fault == "bad_calls":
        messages[0]["tool_calls"] = {}
    elif fault == "bad_arguments":
        messages[0]["tool_calls"][0]["arguments"] = {"command": []}
    elif fault == "wrong_tool":
        messages[0]["tool_calls"][0]["name"] = "run_tests"
    elif fault == "failed":
        body["success"] = False
    elif fault == "numeric_success":
        body["success"] = 1
    elif fault == "orphan_body":
        body["call_id"] = "orphan"
    elif fault == "model_body":
        messages[1]["role"] = "user"
    elif fault == "null":
        body = None
    else:
        body = 12
    messages[-1]["content"] = json.dumps(body)
    assert not committed_job_ticket(messages, **identity)
