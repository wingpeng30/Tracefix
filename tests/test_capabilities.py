from __future__ import annotations

import pytest
from pydantic import ValidationError

from tracefix.capabilities import (
    CallerOrigin,
    CapabilityCatalog,
    CapabilityError,
    CapabilityPolicy,
    ExecutionProfile,
    ToolDispatcher,
    executor_policy_document,
    host_control_policy,
)


def test_catalog_is_deterministic_and_rejects_undescribed_tools() -> None:
    first = CapabilityCatalog.for_tool_names(("read_file", "apply_patch", "read_file"))
    second = CapabilityCatalog.for_tool_names(("apply_patch", "read_file"))

    assert first.sha256 == second.sha256
    assert [item.tool_name for item in first.descriptors] == ["apply_patch", "read_file"]
    with pytest.raises(CapabilityError, match="lacks a capability descriptor"):
        CapabilityCatalog.for_tool_names(("read_file", "mystery_tool"))


def test_descriptor_is_execution_metadata_and_immutable() -> None:
    descriptor = CapabilityCatalog.for_tool_names(("apply_patch",)).descriptors[0]

    assert descriptor.model_dump() == {
        "capability_id": "workspace.apply_patch",
        "tool_name": "apply_patch",
        "resource_domain": "workspace",
        "effect": "workspace_write",
        "idempotent": False,
        "execution_location": "workspace",
        "concurrency": "serial",
        "model_callable": True,
    }
    with pytest.raises(ValidationError):
        descriptor.tool_name = "read_file"


def test_inspect_profile_denies_writes_and_processes_but_allows_reads() -> None:
    policy = CapabilityPolicy(
        CapabilityCatalog.for_tool_names(("read_file", "apply_patch", "run_tests")),
        ExecutionProfile.INSPECT,
    )
    dispatcher = ToolDispatcher(policy)

    assert dispatcher.authorize("read_file", CallerOrigin.MODEL).reason_code == "allowed"
    for name in ("apply_patch", "run_tests"):
        decision = dispatcher.authorize(name, CallerOrigin.MODEL)
        assert not decision.allowed
        assert decision.reason_code == "profile_denied"
        assert decision.policy_sha256 == policy.sha256


def test_workspace_write_profile_allows_declared_model_capabilities() -> None:
    policy = CapabilityPolicy(
        CapabilityCatalog.for_tool_names(("read_file", "apply_patch", "run_tests")),
        ExecutionProfile.WORKSPACE_WRITE,
    )

    assert all(
        policy.authorize(name, CallerOrigin.MODEL).allowed
        for name in ("read_file", "apply_patch", "run_tests")
    )


def test_model_cannot_forge_host_origin_or_call_internal_controls() -> None:
    catalog = CapabilityCatalog.for_tool_names(("run_tests", "get_git_diff"))
    policy = CapabilityPolicy(catalog, ExecutionProfile.WORKSPACE_WRITE)

    assert policy.authorize("run_tests", CallerOrigin.MODEL).allowed
    assert policy.authorize("run_tests", CallerOrigin.VALIDATION_GATE).allowed
    assert policy.authorize("get_git_diff", CallerOrigin.VALIDATION_GATE).reason_code == (
        "origin_not_allowed"
    )
    assert policy.authorize("__prepare__", CallerOrigin.MODEL).reason_code == "internal_control"
    assert policy.authorize("__prepare__", CallerOrigin.VALIDATION_GATE).reason_code == (
        "internal_control"
    )


def test_unknown_and_non_model_origins_fail_closed_with_stable_reasons() -> None:
    policy = CapabilityPolicy(
        CapabilityCatalog.for_tool_names(("read_file",)), ExecutionProfile.WORKSPACE_WRITE
    )

    unknown = policy.authorize("unregistered", CallerOrigin.MODEL)
    recovery = policy.authorize("read_file", CallerOrigin.RECOVERY)
    assert (unknown.allowed, unknown.reason_code) == (False, "unknown_capability")
    assert (recovery.allowed, recovery.reason_code) == (False, "origin_not_allowed")
    assert unknown.policy_sha256 == recovery.policy_sha256
    assert unknown.catalog_sha256 == recovery.catalog_sha256


def test_policy_identity_changes_with_profile() -> None:
    catalog = CapabilityCatalog.for_tool_names(("read_file", "apply_patch"))
    inspect = CapabilityPolicy(catalog, ExecutionProfile.INSPECT)
    write = CapabilityPolicy(catalog, ExecutionProfile.WORKSPACE_WRITE)

    assert inspect.sha256 != write.sha256
    assert inspect.catalog.sha256 == write.catalog.sha256


def test_host_control_policy_limits_checkpoint_actions_to_trusted_origins() -> None:
    policy = host_control_policy(ExecutionProfile.INSPECT)

    restore = policy.authorize("checkpoint_restore", CallerOrigin.RECOVERY)
    commit = policy.authorize("checkpoint_commit", CallerOrigin.HOST_LIFECYCLE)
    forged = policy.authorize("checkpoint_restore", CallerOrigin.MODEL)

    assert restore.allowed and commit.allowed
    assert not forged.allowed and forged.reason_code == "origin_not_allowed"
    assert restore.catalog_sha256 == commit.catalog_sha256 == policy.catalog.sha256


def test_executor_policy_document_freezes_enabled_container_capabilities() -> None:
    document = executor_policy_document(
        ExecutionProfile.WORKSPACE_WRITE, artifacts_enabled=True, skills_enabled=True
    )
    names = [item["tool_name"] for item in document["capabilities"]]

    assert document["policy_version"] == CapabilityPolicy.VERSION
    assert names == [
        "apply_patch",
        "get_git_diff",
        "load_skill",
        "read_file",
        "read_tool_artifact",
        "run_tests",
        "search_code",
        "search_tool_artifact",
    ]
    policy = CapabilityPolicy(
        CapabilityCatalog.for_tool_names(names), ExecutionProfile.WORKSPACE_WRITE
    )
    assert document["capability_policy_sha256"] == policy.sha256
    assert document["capability_catalog_sha256"] == policy.catalog.sha256
