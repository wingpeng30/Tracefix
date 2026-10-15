"""Keep unsafe progress separate from the last committed, resumable checkpoint."""

from __future__ import annotations

from pathlib import Path

from tracefix.checkpoint import CheckpointError, CheckpointStore
from tracefix.jobs.service import JobService
from tracefix.jobs.test_budget import TestExecutionLedger


class JobCheckpointCoordinator:
    def __init__(self, run_dir: Path, *, run_id: str, service: JobService,
                 ledger: TestExecutionLedger) -> None:
        self.service, self.ledger = service, ledger
        self.progress = CheckpointStore(run_dir / "job-progress", {
            "kind": "unsafe-job-progress-v1", "run_id": run_id,
        })
        self._sequence = (self.progress.load(current_identity=self.progress.identity).sequence
                          if self.progress.path.exists() else 0)

    def defer_if_unsafe(self, *, safe_sequence: int) -> bool:
        """Never read source, cancel jobs or touch the safe checkpoint on this path."""
        if not (self.service.active or self.service.has_unknown or self.ledger.pending):
            return False
        self.progress.save({
            "resumable": False, "last_safe_sequence": safe_sequence,
            "reason_code": "job_outcome_unknown" if self.service.has_unknown else "job_active",
            "jobs": [record.model_dump(mode="json")
                     for record in self.service.progress_receipts()],
            "test_executions": self.ledger.checkpoint_identity(),
        }, sequence=self._sequence + 1)
        self._sequence += 1
        return True

    def safe_identity(self) -> dict[str, object]:
        if self.ledger.pending:
            raise CheckpointError("test execution is pending; no safe job checkpoint")
        return {"schema_version": 1, "jobs": self.service.checkpoint_identity(),
                "test_executions": self.ledger.checkpoint_identity()}

    def verify_saved(self, value: object) -> None:
        """A newer launch intent makes an old safe checkpoint ineligible for replay."""
        try:
            current = self.safe_identity()
        except Exception as exc:
            raise CheckpointError("job state is active or unknown; inspect without replay") from exc
        if value != current:
            raise CheckpointError(
                "job state changed after the last safe checkpoint; replay refused")

    @staticmethod
    def inspect_saved(run_dir: Path, *, run_id: str, checkout_id: str,
                      limit: int, value: object) -> None:
        current = {"schema_version": 1,
                   "jobs": JobService.inspect_checkpoint_identity(
                       run_dir / "jobs", run_id=run_id, checkout_id=checkout_id),
                   "test_executions": TestExecutionLedger.inspect_checkpoint_identity(
                       run_dir, run_id=run_id, limit=limit)}
        if current != value:
            raise CheckpointError("job state changed after safe checkpoint; replay refused")
