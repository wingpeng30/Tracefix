"""Pure checks linking a job receipt to a closed model call/result ticket."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from tracefix.jobs.requests import normalize_job_start
from tracefix.messages import ToolCall


def committed_job_ticket(history: Sequence[Mapping], *, call_id: str, job_id: str,
                         invocation_sha256: str) -> bool:
    source = None
    for message in history:
        if message.get("role") == "assistant":
            calls = message.get("tool_calls", ())
            if not isinstance(calls, list | tuple):
                return False
            for call in calls:
                if isinstance(call, dict) and call.get("id") == call_id:
                    if source is not None:
                        return False
                    source = call
    if source is None:
        return False
    try:
        target = normalize_job_start(ToolCall.model_validate(source))
    except ValueError:
        return False
    if hashlib.sha256(target.model_dump_json().encode()).hexdigest() != invocation_sha256:
        return False
    for message in history:
        if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
            continue
        try:
            payload = json.loads(message.get("content") or "null")
        except (ValueError, TypeError):
            continue
        output = payload.get("output") if isinstance(payload, dict) else None
        job = output.get("job") if isinstance(output, dict) else None
        identity = job.get("identity") if isinstance(job, dict) else None
        if (isinstance(identity, dict) and identity.get("job_id") == job_id
                and payload.get("tool_name") == "start_test_job"
                and payload.get("success") is True):
            return True
    return False
