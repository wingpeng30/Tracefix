"""Map a historical job to its immutable artifact through the current run's quota owner."""

from __future__ import annotations

import hashlib
import json

from tracefix.artifacts import ArtifactRef
from tracefix.jobs.domain import TERMINAL_STATUSES, JobError, JobStatus


class RemoteJobArtifactReader:
    def __init__(self, channel, *, run_id: str, result_provider) -> None:
        self.channel, self.run_id, self.result_provider = channel, run_id, result_provider

    def read(self, record, *, stream: str, offset: int, limit: int) -> dict:
        record.require_owner(self.run_id)
        if record.status not in TERMINAL_STATUSES or record.status == JobStatus.OUTCOME_UNKNOWN:
            raise JobError("historical_job_output_requires_final_artifact")
        result = self.result_provider(record.identity.job_id)
        if (result is None or result.call_id != record.identity.call_id
                or result.tool_name != "run_tests"
                or hashlib.sha256(json.dumps(result.model_dump(mode="json"), ensure_ascii=False,
                    sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                != record.outcome_sha256):
            raise JobError("historical_job_result_receipt_mismatch")
        rows = result.output.get("artifact_refs", []) if (
            result is not None and isinstance(result.output, dict)) else []
        refs = tuple(ArtifactRef.model_validate(row) for row in rows)
        if (len(refs) != 1 or refs[0].run_id != self.run_id
                or refs[0].call_id != record.identity.call_id or refs[0].tool_name != "run_tests"):
            raise JobError("historical_job_artifact_reference_missing_or_invalid")
        ref = refs[0]
        response = self.channel.call("read_tool_artifact", {
            "artifact_id": ref.artifact_id, "stream": stream, "offset": offset, "limit": limit})
        if not response.success:
            raise JobError("historical_job_artifact_unavailable")
        page = response.output
        if (not isinstance(page, dict) or page.get("artifact_id") != ref.artifact_id
                or page.get("view_sha256") != ref.view_sha256
                or page.get("source_identity") != ref.source_identity
                or page.get("complete") is not ref.complete or page.get("stream") != stream
                or page.get("offset") != offset or not isinstance(page.get("content"), str)
                or len(page["content"]) > limit
                or page.get("next_offset") != offset + len(page["content"])):
            raise JobError("historical_job_artifact_receipt_mismatch")
        return {**page, "job_id": record.identity.job_id, "final": True}
