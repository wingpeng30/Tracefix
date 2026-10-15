"""Immutable test recipes and the shared synchronous/background evidence verdict."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PreparedTest:
    call_json: str
    command: tuple[str, ...]
    timeout_seconds: float
    environment: tuple[tuple[str, str], ...] = field(repr=False)
    workspace: Path
    evidence_dir: Path
    audit_id: str
    audit_path: Path
    junit_path: Path
    source_sha256_before: str
    started_monotonic: float


@dataclass(frozen=True, slots=True)
class TestRunOutcome:
    test_status: str
    source_sha256_before: str
    source_sha256_after: str
    diagnostic: str | None

    @property
    def passed(self) -> bool:
        return self.test_status == "passed"


def judge_test_outcome(
    *, returncode: int | None, stdout: str, stderr: str,
    audit: Mapping[str, object], counts: Mapping[str, int],
    audit_issue: str | None, junit_issue: str | None, phase_issue: str | None,
    source_before: str, source_after: str,
) -> TestRunOutcome:
    """Reuse the existing qualification rules, independently of process scheduling."""
    if returncode is None:
        status = "timed_out"
    elif not audit and any(token in f"{stdout}\n{stderr}".casefold() for token in (
        "modulenotfounderror", "no module named", "importerror", "cannot import name",
    )):
        status = "environment_error"
    elif audit_issue or junit_issue or phase_issue or not audit:
        status = "invalid_test_run"
    elif (counts["tests"] > 0 and counts["passed"] == 0
          and counts["failures"] == 0 and counts["errors"] == 0):
        status = "invalid_test_run"
    elif returncode != 0:
        status = "test_failure"
    elif (counts["tests"] == 0 or counts["passed"] == 0
          or counts["failures"] or counts["errors"]):
        status = "invalid_test_run"
    else:
        status = "passed"
    if status == "passed" and source_before != source_after:
        status = "invalid_test_run"
    return TestRunOutcome(status, source_before, source_after,
                          audit_issue or junit_issue or phase_issue)
