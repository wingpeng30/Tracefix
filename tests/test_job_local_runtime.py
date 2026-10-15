from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tracefix.artifacts import ArtifactStore
from tracefix.capabilities import CallerOrigin
from tracefix.jobs.domain import JobError
from tracefix.jobs.local_runtime import LocalJobRuntime
from tracefix.jobs.ports import JobSourceState
from tracefix.messages import ToolCall
from tracefix.tools.base import ToolRegistry
from tracefix.tools.builtin import RunTestsTool
from tracefix.tools.jobs import JOB_TOOL_NAMES, ManagedRunTestsTool, register_job_tools


class UnusedScope:
    closed = False

    def close(self):
        self.closed = True
        return True


def assemble(tmp_path, *, streaming=True, profile="workspace_write", scope_factory=UnusedScope,
             collision=False, custom_executor=False, extra_capabilities=()):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    artifacts = ArtifactStore(tmp_path / "artifacts", run_id="r1", streaming_enabled=streaming)
    original = RunTestsTool(workspace, artifact_store=artifacts)
    registry = ToolRegistry([ManagedRunTestsTool(original, None) if custom_executor else original])
    if collision:
        register_job_tools(registry, None)
    runtime = LocalJobRuntime(
        tools=registry, artifacts=artifacts, run_dir=tmp_path, run_id="r1", checkout_id="a" * 64,
        limit=1, deadline=datetime.now(UTC) + timedelta(seconds=10), profile=profile,
        scope_factory=scope_factory, extra_capabilities=extra_capabilities,
        source_identity=lambda: JobSourceState(
            product_diff_sha256="b" * 64, nonempty_diff=False))
    return runtime, registry, original


def test_local_assembly_freezes_schema_and_inspect_cannot_reserve_or_prepare(tmp_path):
    runtime, old, original = assemble(tmp_path, profile="inspect")
    try:
        assert old.names == ("run_tests",)
        assert runtime.tools.names == ("run_tests", *JOB_TOOL_NAMES)
        assert runtime.tools.get("run_tests").spec == original.spec
        assert runtime.dispatcher.authorize("get_job_status", CallerOrigin.MODEL).allowed
        assert not runtime.dispatcher.authorize("start_test_job", CallerOrigin.MODEL).allowed
        with pytest.raises(JobError):
            runtime.service.start(ToolCall(id="denied", name="run_tests",
                                           arguments={"command": "not pytest"}))
        assert runtime.ledger.charged == runtime.ledger.held == 0
        assert not runtime.session.active and not runtime.session.has_unknown
        assert runtime.session.source_state().product_diff_sha256 == "b" * 64
        identity = runtime.checkpoints.safe_identity()
        runtime.checkpoints.verify_saved(identity)
    finally:
        assert runtime.close()
    reopened, _, _ = assemble(tmp_path, profile="inspect")
    try:
        reopened.checkpoints.verify_saved(identity)
    finally:
        assert reopened.close()


@pytest.mark.parametrize("arguments,reason", [
    ({"streaming": False}, "require_streaming_artifacts"),
    ({"collision": True}, "tool_collision"),
    ({"custom_executor": True}, "require_prepared_test_executor"),
])
def test_incompatible_assembly_fails_before_creating_execution_ledger(tmp_path, arguments, reason):
    with pytest.raises(JobError, match=reason):
        assemble(tmp_path, **arguments)
    assert not (tmp_path / "test-executions").exists()


def test_ownership_probe_failure_does_not_create_a_ledger(tmp_path):
    class FailedScope:
        def close(self):
            return False

    with pytest.raises(JobError, match="preflight_failed"):
        assemble(tmp_path, scope_factory=FailedScope)
    assert not (tmp_path / "test-executions").exists()


@pytest.mark.parametrize("primary", [False, True])
def test_validation_cleanup_failure_preserves_primary_error(tmp_path, monkeypatch, primary):
    from types import SimpleNamespace

    runtime, _, original = assemble(tmp_path, extra_capabilities=("validation_baseline",))
    monkeypatch.setattr("tracefix.jobs.local_runtime.JobService",
                        lambda *args, **kwargs: SimpleNamespace(shutdown=lambda: False))
    try:
        expected, reason = (ValueError, "primary validation failure") if primary else (
            JobError, "cleanup_or_outcome_unknown")
        with pytest.raises(expected, match=reason) as error:
            with runtime.validation_executor(original, record_dir=tmp_path / "baseline",
                                             checkout_id="c" * 64):
                if primary:
                    raise ValueError("primary validation failure")
        if primary:
            assert error.value.__notes__ == ["validation_job_cleanup_or_outcome_unknown"]
    finally:
        assert runtime.close()


def test_validation_scope_cannot_gain_patch_authority_or_ignore_inspect_profile(
    tmp_path, monkeypatch,
):
    runtime, _, original = assemble(tmp_path, profile="inspect",
                                     extra_capabilities=("validation_baseline",))
    try:
        with pytest.raises(JobError, match="profile_denied"):
            with runtime.validation_executor(original, record_dir=tmp_path / "baseline",
                                             checkout_id="c" * 64):
                raise AssertionError("inspect acquired an execution scope")
    finally:
        assert runtime.close()
    runtime, _, original = assemble(tmp_path, extra_capabilities=("validation_baseline",))

    def service(*args, **kwargs):
        kwargs["authorize"](ToolCall(id="forged", name="apply_patch", arguments={}))
        raise AssertionError("validation role granted an unrelated tool")

    monkeypatch.setattr("tracefix.jobs.local_runtime.JobService", service)
    try:
        with pytest.raises(JobError):
            with runtime.validation_executor(original, record_dir=tmp_path / "baseline",
                                             checkout_id="c" * 64):
                raise AssertionError("validation role gained patch authority")
        assert runtime.ledger.charged == runtime.ledger.held == 0
    finally:
        assert runtime.close()


def test_partial_local_assembly_releases_execution_ownership(tmp_path, monkeypatch):
    from tracefix.jobs.test_budget import TestExecutionLedger

    def failure(*args, **kwargs):
        raise JobError("corrupt_job_store")

    monkeypatch.setattr("tracefix.jobs.local_runtime.JobService", failure)
    with pytest.raises(JobError, match="corrupt_job_store"):
        assemble(tmp_path)
    with TestExecutionLedger(tmp_path, run_id="r1", limit=1) as ledger:
        assert ledger.charged == ledger.held == 0
