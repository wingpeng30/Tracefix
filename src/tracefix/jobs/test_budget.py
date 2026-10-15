"""Durable test-execution reservations; AgentState consumes the resulting projection."""

from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from tracefix.checkpoint import CheckpointError, CheckpointStore, ProcessLock
from tracefix.jobs.domain import JobError
from tracefix.jobs.executions import TestExecution


class _BudgetState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_runs: StrictInt = Field(ge=0)
    executions: tuple[TestExecution, ...] = Field(default=(), max_length=4096)

    @model_validator(mode="after")
    def unique(self) -> _BudgetState:
        if (len({row.execution_id for row in self.executions}) != len(self.executions)
                or len({row.call_id for row in self.executions}) != len(self.executions)):
            raise ValueError("duplicate test execution or call identity")
        return self

    @property
    def charged(self) -> int:
        return self.baseline_runs + sum(row.process_started is True for row in self.executions)

    @property
    def held(self) -> int:
        return sum(row.phase in {"reserved", "dispatched", "unknown"}
                   and row.process_started is not True for row in self.executions)


class TestExecutionLedger:
    """One host owner, atomic writes before dispatch, unknown work never replayed/refunded."""

    __test__ = False

    def __init__(self, run_dir: Path, *, run_id: str, limit: int, baseline_runs: int = 0) -> None:
        if type(limit) is not int or limit < 1:
            raise JobError("test_execution_limit_invalid")
        self.limit = limit
        self._epoch = uuid4().hex
        self._lock = threading.RLock()
        self._closed = False
        self._faulted = False
        self.store = CheckpointStore(run_dir / "test-executions", {
            "kind": "test-execution-ledger-v1", "run_id": run_id, "limit": limit,
        })
        self._owner = ProcessLock(self.store.run_dir).acquire()
        try:
            if self.store.path.exists():
                if self.store.path.stat().st_size > 2 * 1024 * 1024:
                    raise JobError("test_execution_ledger_exceeds_limit")
                saved = self.store.load(current_identity=self.store.identity)
                state = _BudgetState.model_validate(saved.payload)
                self._sequence = saved.sequence
                self._state = state
                recovered = tuple(
                    row.model_copy(update={"phase": "unknown"})
                    if row.phase in {"reserved", "dispatched"} else row
                    for row in state.executions
                )
                if recovered != state.executions:
                    self._commit(_BudgetState(baseline_runs=state.baseline_runs,
                                              executions=recovered))
            else:
                self._sequence = 0
                self._state = _BudgetState(baseline_runs=baseline_runs)
                self._commit(self._state)
            if self._state.charged + self._state.held > limit:
                raise JobError("test_execution_budget_inconsistent")
        except BaseException:
            self._owner.release()
            raise

    @property
    def charged(self) -> int:
        return self._state.charged

    @property
    def owner_epoch(self) -> str:
        return self._epoch

    @property
    def held(self) -> int:
        return self._state.held

    @property
    def has_unknown(self) -> bool:
        return self._faulted or any(row.phase == "unknown" for row in self._state.executions)

    @property
    def pending(self) -> bool:
        return any(row.phase in {"reserved", "dispatched"} for row in self._state.executions)

    def _require_open(self) -> None:
        if self._closed:
            raise JobError("test_execution_ledger_closed")
        if self._faulted:
            raise JobError("test_execution_persistence_failed")

    def _commit(self, state: _BudgetState) -> None:
        if state.charged + state.held > self.limit:
            raise JobError("test_execution_budget_exceeded")
        try:
            self.store.save(state.model_dump(mode="json"), sequence=self._sequence + 1)
        except BaseException:
            self._faulted = True
            raise
        self._state = state
        self._sequence += 1

    def reserve(self, *, call_id: str, invocation_sha256: str) -> TestExecution:
        with self._lock:
            self._require_open()
            if any(row.phase == "unknown" for row in self._state.executions):
                raise JobError("test_execution_outcome_unknown")
            if any(row.call_id == call_id for row in self._state.executions):
                raise JobError("test_execution_call_replay_refused")
            row = TestExecution(execution_id=uuid4().hex, call_id=call_id,
                                invocation_sha256=invocation_sha256, owner_epoch=self._epoch)
            state = _BudgetState(baseline_runs=self._state.baseline_runs,
                                 executions=(*self._state.executions, row))
            self._commit(state)
            return row

    def get(self, execution_id: str) -> TestExecution:
        with self._lock:
            self._require_open()
            for row in self._state.executions:
                if row.execution_id == execution_id:
                    return row
            raise JobError("test_execution_not_found")

    def _replace(self, row: TestExecution, **values: object) -> TestExecution:
        if row.owner_epoch != self._epoch:
            raise JobError("test_execution_stale_owner_epoch")
        updated = TestExecution.model_validate({**row.model_dump(), **values})
        if updated == row:
            return row
        self._commit(_BudgetState(baseline_runs=self._state.baseline_runs,
                                  executions=tuple(updated if item == row else item
                                                   for item in self._state.executions)))
        return updated

    def before_dispatch(self, execution_id: str) -> TestExecution:
        with self._lock:
            row = self.get(execution_id)
            if row.phase != "reserved":
                raise JobError("test_execution_dispatch_replay_refused")
            return self._replace(row, phase="dispatched", process_started=None)

    def acknowledge_started(self, execution_id: str) -> TestExecution:
        with self._lock:
            row = self.get(execution_id)
            if row.phase not in {"dispatched", "unknown", "settled"}:
                raise JobError("test_execution_start_observation_invalid")
            return self._replace(row, process_started=True)

    def settle(self, execution_id: str, *, process_started: bool | None,
               tree_exit_confirmed: bool, outcome_known: bool = True) -> TestExecution:
        with self._lock:
            row = self.get(execution_id)
            if row.phase == "reserved" and process_started is not False:
                raise JobError("test_execution_dispatch_not_recorded")
            if row.process_started is True and process_started is not True:
                raise JobError("test_execution_start_observation_cannot_be_retracted")
            phase = "unknown"
            if tree_exit_confirmed and process_started is not None and outcome_known:
                phase = "settled" if process_started else "not_started"
            if row.phase in {"settled", "not_started", "unknown"} and (
                phase != row.phase or process_started is not row.process_started
                or tree_exit_confirmed is not row.tree_exit_confirmed
            ):
                raise JobError("test_execution_terminal_observation_conflict")
            return self._replace(row, phase=phase, process_started=process_started,
                                 tree_exit_confirmed=tree_exit_confirmed)

    def checkpoint_identity(self) -> dict[str, object]:
        with self._lock:
            self._require_open()
            snapshot = self.store.load(current_identity=self.store.identity)
            if (snapshot.sequence != self._sequence
                    or snapshot.payload != self._state.model_dump(mode="json")):
                raise CheckpointError("test execution ledger changed outside owner")
            return {"sequence": snapshot.sequence, "payload_sha256": snapshot.payload_sha256}

    @staticmethod
    def inspect_checkpoint_identity(run_dir: Path, *, run_id: str, limit: int) -> dict[str, object]:
        """Read-only inspection must never recover, reserve, refund or acquire ownership."""
        store = CheckpointStore(run_dir / "test-executions", {
            "kind": "test-execution-ledger-v1", "run_id": run_id, "limit": limit,
        })
        if store.path.stat().st_size > 2 * 1024 * 1024:
            raise CheckpointError("test execution ledger exceeds its size limit")
        saved = store.load(current_identity=store.identity)
        state = _BudgetState.model_validate(saved.payload)
        if (state.charged + state.held > limit or any(
            row.phase in {"reserved", "dispatched", "unknown"} for row in state.executions
        )):
            raise CheckpointError("test execution is pending or unknown; replay refused")
        return {"sequence": saved.sequence, "payload_sha256": saved.payload_sha256}

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._owner.release()

    def __enter__(self) -> TestExecutionLedger:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
