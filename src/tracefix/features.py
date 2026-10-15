"""Pure feature admission, independent of Pydantic and executor implementations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tracefix.exceptions import RunConfigurationError


class SkillFlags(Protocol):
    skills_enabled: bool


class FeatureConfiguration(Protocol):
    execution_backend: str
    docker_profile: str
    docker_task_id: str | None
    docker_input_root: Path | None
    docker_image_id: str | None
    docker_recovery_enabled: bool
    harness_foundations_enabled: bool
    memory_enabled: bool
    conversation_enabled: bool
    mcp_serena_image_id: str | None
    skills_root: Path | None
    test_target: str | None
    source_import: str | None
    regression_targets: tuple[str, ...]
    agent_config: SkillFlags


class FeatureResolutionError(RunConfigurationError, ValueError):
    code = "feature_not_supported"


@dataclass(frozen=True)
class ResolvedFeatures:
    backend: str
    profile: str
    enabled: tuple[str, ...]


def resolve_features(config: FeatureConfiguration) -> ResolvedFeatures:
    """Reject unsupported combinations without touching files, Docker or a model.

    Availability probes remain in runtime preflight; this resolver never installs,
    pulls, switches backend, or expands permissions.
    """
    docker = config.execution_backend == "docker"
    ordinary = docker and config.docker_profile == "ordinary"

    def require(condition: bool, reason: str, message: str) -> None:
        if not condition:
            raise FeatureResolutionError(message, context={
                "reason_code": reason, "backend": config.execution_backend,
                "profile": config.docker_profile,
            })

    if config.regression_targets:
        require(bool(config.test_target), "validation_target_missing",
                "regression_targets require a test target")
        require(bool(config.source_import), "source_import_missing",
                "regression_targets require a source import")
        require(not docker or ordinary and config.docker_recovery_enabled,
                "validation_backend_unsupported",
                "regression_targets require ordinary Docker recovery")
    if docker and not ordinary:
        require(bool(config.docker_task_id and config.docker_input_root),
                "frozen_inputs_missing", "Docker execution requires a task ID and input root")
    if ordinary:
        require(config.docker_task_id in (None, "tracefix-ordinary"),
                "ordinary_frozen_task", "ordinary Docker does not accept a frozen task ID")
        require(config.docker_input_root is None, "ordinary_frozen_inputs",
                "ordinary Docker does not accept frozen task inputs")
    require(config.skills_root is None or config.agent_config.skills_enabled,
            "skills_disabled", "custom Skills directories require Skills to be enabled")
    require(not config.docker_recovery_enabled or ordinary, "recovery_backend_unsupported",
            "Docker recovery requires the ordinary Docker backend")
    require(not (config.harness_foundations_enabled and docker)
            or config.docker_recovery_enabled, "foundations_recovery_required",
            "Docker harness foundations require ordinary Docker recovery")
    require(not config.memory_enabled or not docker or config.docker_recovery_enabled,
            "memory_recovery_required", "experience memory currently requires the local backend")
    require(not config.conversation_enabled or not docker or config.docker_recovery_enabled,
            "dialogue_recovery_required",
            "continuous dialogue currently requires the local backend")
    require(config.mcp_serena_image_id is None or not docker or ordinary,
            "serena_backend_unsupported", "Serena MCP requires local or ordinary Docker execution")
    require(docker or config.docker_profile == "frozen" and config.docker_image_id is None,
            "docker_inputs_on_local",
            "Docker profile and image identity require the Docker backend")
    require(config.docker_profile != "synthetic" or bool(config.docker_image_id),
            "synthetic_image_missing", "synthetic Docker execution requires a frozen image ID")
    require(config.docker_profile != "frozen" or config.docker_image_id is None,
            "frozen_image_override", "frozen pilot image identities cannot be overridden")
    enabled = tuple(name for name, value in (
        ("memory", config.memory_enabled), ("dialogue", config.conversation_enabled),
        ("foundations", config.harness_foundations_enabled),
        ("skills", config.agent_config.skills_enabled),
        ("serena", config.mcp_serena_image_id is not None),
        ("regression", bool(config.regression_targets)),
        ("recovery", config.docker_recovery_enabled),
    ) if value)
    return ResolvedFeatures(config.execution_backend, config.docker_profile, enabled)
