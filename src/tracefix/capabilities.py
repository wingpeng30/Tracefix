"""Immutable execution capabilities and fail-closed tool authorization.

Descriptors deliberately live beside execution policy rather than in vendor tool
schemas.  The model can request a tool by name but cannot choose its origin,
resource domain, or permissions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class CapabilityError(ValueError):
    """A tool has no trusted capability description or the policy is invalid."""


class ExecutionProfile(StrEnum):
    INSPECT = "inspect"
    WORKSPACE_WRITE = "workspace_write"


class CallerOrigin(StrEnum):
    MODEL = "model"
    VALIDATION_GATE = "validation_gate"
    RECOVERY = "recovery"
    HOST_LIFECYCLE = "host_lifecycle"
    INDEPENDENT_VALIDATION = "independent_validation"


class CapabilityDescriptor(BaseModel):
    """Execution-side metadata for one callable tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    tool_name: str = Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    resource_domain: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    effect: str = Field(pattern=r"^(read|session_write|workspace_write|execute)$")
    idempotent: bool
    execution_location: str = Field(pattern=r"^(host|workspace|container|mcp)$")
    concurrency: str = Field(pattern=r"^(serial|concurrent)$")
    model_callable: bool


class CapabilityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    tool_name: str
    capability_id: str | None = None
    origin: CallerOrigin
    profile: ExecutionProfile
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


_DESCRIPTORS: dict[str, CapabilityDescriptor] = {}


def _register(
    name: str,
    capability: str,
    domain: str,
    effect: str,
    *,
    idempotent: bool,
    location: str = "workspace",
    concurrency: str = "serial",
    model_callable: bool = True,
) -> None:
    _DESCRIPTORS[name] = CapabilityDescriptor(
        tool_name=name,
        capability_id=capability,
        resource_domain=domain,
        effect=effect,
        idempotent=idempotent,
        execution_location=location,
        concurrency=concurrency,
        model_callable=model_callable,
    )


_register("search_code", "workspace.search", "workspace", "read", idempotent=True)
_register("read_file", "workspace.read_file", "workspace", "read", idempotent=True)
_register("apply_patch", "workspace.apply_patch", "workspace", "workspace_write", idempotent=False)
_register("run_tests", "workspace.run_tests", "workspace", "execute", idempotent=False)
_register("get_git_diff", "workspace.get_diff", "workspace", "read", idempotent=True)
_register("start_test_job", "job.start_test", "workspace", "execute",
          idempotent=False, location="host")
_register("get_job_status", "job.status", "checkpoint", "read",
          idempotent=True, location="host")
_register("read_job_output", "job.output", "artifact", "read",
          idempotent=True, location="host")
_register("cancel_job", "job.cancel", "checkpoint", "session_write",
          idempotent=True, location="host")
_register("read_tool_artifact", "artifact.read", "artifact", "read", idempotent=True)
_register("search_tool_artifact", "artifact.search", "artifact", "read", idempotent=True)
_register(
    "get_task_state", "task_state.read", "checkpoint", "read", idempotent=True, location="host"
)
_register(
    "update_task_state",
    "task_state.update",
    "checkpoint",
    "session_write",
    idempotent=False,
    location="host",
)
_register("load_skill", "skill_snapshot.read", "skill_snapshot", "read", idempotent=True)
_register(
    "mcp_serena_symbols",
    "source_snapshot.symbols",
    "source_snapshot",
    "read",
    idempotent=True,
    location="mcp",
)
_register(
    "mcp_serena_find_symbol",
    "source_snapshot.find_symbol",
    "source_snapshot",
    "read",
    idempotent=True,
    location="mcp",
)
_register(
    "mcp_serena_references",
    "source_snapshot.references",
    "source_snapshot",
    "read",
    idempotent=True,
    location="mcp",
)
_register(
    "compact_context",
    "context.compact",
    "checkpoint",
    "session_write",
    idempotent=True,
    location="host",
)
_register(
    "validation_baseline",
    "validation.baseline",
    "workspace",
    "execute",
    idempotent=False,
    location="host",
    model_callable=False,
)
_register(
    "source_snapshot_export",
    "source_snapshot.export",
    "source_snapshot",
    "read",
    idempotent=True,
    location="host",
    model_callable=False,
)
_register(
    "checkpoint_restore",
    "checkpoint.restore",
    "checkpoint",
    "workspace_write",
    idempotent=False,
    location="container",
    model_callable=False,
)
_register(
    "checkpoint_commit",
    "checkpoint.commit",
    "checkpoint",
    "session_write",
    idempotent=False,
    location="host",
    model_callable=False,
)

_HOST_CONTROL_CAPABILITY_NAMES = (
    "checkpoint_commit",
    "checkpoint_restore",
)


class CapabilityCatalog:
    """A frozen catalog; unknown tool names are rejected during runtime assembly."""

    def __init__(self, descriptors: Iterable[CapabilityDescriptor]) -> None:
        items = tuple(sorted(descriptors, key=lambda item: item.tool_name))
        names = [item.tool_name for item in items]
        ids = [item.capability_id for item in items]
        if len(names) != len(set(names)) or len(ids) != len(set(ids)):
            raise CapabilityError("capability catalog contains duplicate identities")
        self._items = items
        self._by_name = {item.tool_name: item for item in items}
        canonical = json.dumps(
            [item.model_dump(mode="json") for item in items],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.sha256 = hashlib.sha256(canonical).hexdigest()

    @classmethod
    def for_tool_names(cls, names: Iterable[str]) -> CapabilityCatalog:
        unique = tuple(dict.fromkeys(names))
        unknown = sorted(name for name in unique if name not in _DESCRIPTORS)
        if unknown:
            raise CapabilityError("tool lacks a capability descriptor: " + ", ".join(unknown))
        return cls(_DESCRIPTORS[name] for name in unique)

    @property
    def descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        return self._items

    def get(self, name: str) -> CapabilityDescriptor | None:
        return self._by_name.get(name)


class CapabilityPolicy:
    """Pure, stable policy decisions for model and host-owned invocations."""

    VERSION = "tracefix-capability-policy-v3"

    def __init__(self, catalog: CapabilityCatalog, profile: ExecutionProfile) -> None:
        self.catalog = catalog
        self.profile = ExecutionProfile(profile)
        policy_identity = json.dumps(
            {
                "version": self.VERSION,
                "profile": self.profile.value,
                "catalog_sha256": catalog.sha256,
                "inspect_effects": ["read", "session_write"],
                "model_origins": ["model"],
                "validation_gate_tools": ["run_tests", "validation_baseline"],
                "recovery_tools": ["checkpoint_restore"],
                "host_lifecycle_tools": ["checkpoint_commit", "source_snapshot_export"],
                "independent_validation_tools": ["apply_patch", "get_git_diff", "run_tests"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.sha256 = hashlib.sha256(policy_identity).hexdigest()

    def authorize(self, tool_name: str, origin: CallerOrigin) -> CapabilityDecision:
        trusted_origin = CallerOrigin(origin)
        descriptor = self.catalog.get(tool_name)
        reason = "allowed"
        allowed = True
        if tool_name.startswith("__"):
            allowed, reason = False, "internal_control"
        elif descriptor is None:
            allowed, reason = False, "unknown_capability"
        elif trusted_origin is CallerOrigin.MODEL and not descriptor.model_callable:
            allowed, reason = False, "origin_not_allowed"
        elif trusted_origin is CallerOrigin.VALIDATION_GATE and tool_name not in {
            "run_tests", "validation_baseline"
        }:
            allowed, reason = False, "origin_not_allowed"
        elif trusted_origin is CallerOrigin.RECOVERY and tool_name != "checkpoint_restore":
            allowed, reason = False, "origin_not_allowed"
        elif trusted_origin is CallerOrigin.HOST_LIFECYCLE and tool_name not in {
            "checkpoint_commit", "source_snapshot_export"
        }:
            allowed, reason = False, "origin_not_allowed"
        elif trusted_origin is CallerOrigin.INDEPENDENT_VALIDATION and tool_name not in {
            "apply_patch", "get_git_diff", "run_tests",
        }:
            allowed, reason = False, "origin_not_allowed"
        elif (
            trusted_origin in {
                CallerOrigin.MODEL, CallerOrigin.VALIDATION_GATE,
                CallerOrigin.INDEPENDENT_VALIDATION,
            }
            and self.profile is ExecutionProfile.INSPECT
            and descriptor.effect in {
            "workspace_write",
            "execute",
            }
        ):
            allowed, reason = False, "profile_denied"
        return CapabilityDecision(
            allowed=allowed,
            reason_code=reason,
            tool_name=tool_name,
            capability_id=descriptor.capability_id if descriptor else None,
            origin=trusted_origin,
            profile=self.profile,
            policy_sha256=self.sha256,
            catalog_sha256=self.catalog.sha256,
        )


class RuntimeInvocationGuard(Protocol):
    def check(self, descriptor: CapabilityDescriptor, origin: CallerOrigin) -> str | None: ...


class ToolDispatcher:
    """Policy gate shared by all model-originated tool execution paths."""

    def __init__(self, policy: CapabilityPolicy, *,
                 execution_guard: RuntimeInvocationGuard | None = None) -> None:
        self.policy = policy
        self._execution_guard = execution_guard

    def authorize(self, tool_name: str, origin: CallerOrigin) -> CapabilityDecision:
        decision = self.policy.authorize(tool_name, origin)
        descriptor = self.policy.catalog.get(tool_name)
        if decision.allowed and self._execution_guard is not None and descriptor is not None:
            reason = self._execution_guard.check(descriptor, CallerOrigin(origin))
            if reason is not None:
                return CapabilityDecision.model_validate({
                    **decision.model_dump(), "allowed": False, "reason_code": reason,
                })
        return decision

    def prepare_invocation(self, tool, call, origin: CallerOrigin):
        """No validation, callback binding or process work precedes authorization."""
        from tracefix.tools.invocation import PreparedInvocation

        decision = self.authorize(call.name, origin)
        if not decision.allowed:
            raise CapabilityError(f"capability policy denied: {decision.reason_code}")
        return PreparedInvocation(tool, call)

    def invoke(self, tool, call, origin: CallerOrigin):
        return self.prepare_invocation(tool, call, origin).execute()


def independent_validation_dispatcher(profile: ExecutionProfile | str) -> ToolDispatcher:
    catalog = CapabilityCatalog.for_tool_names(("apply_patch", "get_git_diff", "run_tests"))
    dispatcher = ToolDispatcher(CapabilityPolicy(catalog, ExecutionProfile(profile)))
    for name in ("apply_patch", "run_tests"):
        decision = dispatcher.authorize(name, CallerOrigin.INDEPENDENT_VALIDATION)
        if not decision.allowed:
            raise CapabilityError(f"independent validation denied: {decision.reason_code}")
    return dispatcher


def host_control_policy(profile: ExecutionProfile | str) -> CapabilityPolicy:
    """Create the fixed policy used for host-owned checkpoint operations."""
    catalog = CapabilityCatalog.for_tool_names(_HOST_CONTROL_CAPABILITY_NAMES)
    return CapabilityPolicy(catalog, ExecutionProfile(profile))


def executor_policy_document(
    profile: ExecutionProfile | str,
    *,
    artifacts_enabled: bool,
    skills_enabled: bool,
) -> dict[str, object]:
    """Build the exact ordinary-container policy file from frozen feature flags."""
    names = ["search_code", "read_file", "apply_patch", "run_tests", "get_git_diff"]
    if artifacts_enabled:
        names.extend(("read_tool_artifact", "search_tool_artifact"))
    if skills_enabled:
        names.append("load_skill")
    catalog = CapabilityCatalog.for_tool_names(names)
    policy = CapabilityPolicy(catalog, ExecutionProfile(profile))
    return {
        "schema_version": 1,
        "policy_version": policy.VERSION,
        "execution_profile": policy.profile.value,
        "capability_catalog_sha256": catalog.sha256,
        "capability_policy_sha256": policy.sha256,
        "capabilities": [item.model_dump(mode="json") for item in catalog.descriptors],
    }
