"""A fixed archive reference for serial transfer between owned validation executors."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class JobArtifactSeed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root: Path
    snapshot_json: str = Field(max_length=4 * 1024 * 1024)

    @classmethod
    def from_snapshot(cls, root: Path, snapshot: dict):
        fields = {"batch_id", "artifacts_enabled", "artifact_archive", "artifact_archive_bytes",
                  "artifact_archive_sha256", "artifact_receipt", "streaming_artifacts",
                  "artifact_run_id"}
        return cls(root=root, snapshot_json=json.dumps({key: snapshot[key] for key in fields},
                                                       sort_keys=True, separators=(",", ":")))

    @property
    def snapshot(self) -> dict:
        value = json.loads(self.snapshot_json)
        if not isinstance(value, dict):
            raise ValueError("artifact seed must contain a snapshot reference")
        return value


class JobArtifactChain:
    """Serial executor handoff cannot reset quotas or rewrite already published objects."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.seed: JobArtifactSeed | None = None

    def advance(self, seed: JobArtifactSeed) -> None:
        from tracefix.docker_recovery import owned_file, validate_artifact_archive

        seed = JobArtifactSeed.model_validate_json(seed.model_dump_json())

        def inventory(value):
            snapshot = value.snapshot
            receipt = validate_artifact_archive(value.root, snapshot,
                expected_run_id=self.run_id, streaming_enabled=True)
            archive = owned_file(value.root, snapshot["artifact_archive"])
            with tarfile.open(archive, mode="r|") as stream:
                member = stream.next()
                if member is None or member.name != "manifest.json" or member.size > 2 * 1024**2:
                    raise ValueError("artifact handoff manifest is missing or oversized")
                contents = stream.extractfile(member)
                if contents is None:
                    raise ValueError("artifact handoff manifest is unreadable")
                raw = contents.read(2 * 1024**2 + 1)
                if hashlib.sha256(raw).hexdigest() != receipt["manifest_sha256"]:
                    raise ValueError("artifact handoff manifest changed")
                return json.loads(raw), receipt

        current, receipt = inventory(seed)
        if self.seed is not None:
            previous, old_receipt = inventory(self.seed)
            if (any(current["artifacts"].get(key) != value
                    for key, value in previous["artifacts"].items())
                    or any(receipt[field] < old_receipt[field]
                           for field in ("storage_bytes", "queries", "returned_chars"))):
                raise ValueError("artifact handoff rewrites history or reduces cumulative quotas")
        self.seed = seed
