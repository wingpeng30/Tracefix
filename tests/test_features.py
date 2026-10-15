"""One feature resolver serves API admission, preflight and recovery."""

from pathlib import Path

import pytest

from tracefix.features import FeatureResolutionError, resolve_features
from tracefix.runtime import RunConfig, TraceFixRunner


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"background_jobs_enabled": True}, "jobs_foundations_required"),
    ({"job_cgroup_root": Path("scope")}, "jobs_delegation_backend_invalid"),
    ({"background_jobs_enabled": True, "harness_foundations_enabled": True,
      "test_environment_variables": {"EXAMPLE_MODE": "fixture"}}, "jobs_local_recovery_required"),
    ({"background_jobs_enabled": True, "harness_foundations_enabled": True,
      "execution_backend": "docker"}, "jobs_docker_integration_pending"),
    ({"regression_targets": ("tests/a.py",)}, "validation_target_missing"),
    ({"regression_targets": ("tests/a.py",), "test_target": "tests/b.py"},
     "source_import_missing"),
    ({"execution_backend": "docker", "harness_foundations_enabled": True},
     "foundations_recovery_required"),
    ({"execution_backend": "docker", "memory_enabled": True}, "memory_recovery_required"),
    ({"execution_backend": "docker", "conversation_enabled": True},
     "dialogue_recovery_required"),
    ({"docker_recovery_enabled": True}, "recovery_backend_unsupported"),
    ({"skills_root": Path("approved")}, "skills_disabled"),
    ({"docker_image_id": "sha256:" + "a" * 64}, "docker_inputs_on_local"),
    ({"execution_backend": "docker", "docker_profile": "frozen"}, "frozen_inputs_missing"),
    ({"execution_backend": "docker", "docker_task_id": "frozen-task"},
     "ordinary_frozen_task"),
    ({"execution_backend": "docker", "docker_input_root": Path("inputs")},
     "ordinary_frozen_inputs"),
    ({"execution_backend": "docker", "regression_targets": ("tests/a.py",),
      "test_target": "tests/b.py", "source_import": "widget"},
     "validation_backend_unsupported"),
    ({"execution_backend": "docker", "docker_profile": "synthetic",
      "docker_task_id": "task", "docker_input_root": Path("inputs")},
     "synthetic_image_missing"),
    ({"execution_backend": "docker", "docker_profile": "frozen",
      "docker_task_id": "task", "docker_input_root": Path("inputs"),
      "docker_image_id": "sha256:" + "a" * 64}, "frozen_image_override"),
    ({"execution_backend": "docker", "docker_profile": "frozen",
      "docker_task_id": "task", "docker_input_root": Path("inputs"),
      "mcp_serena_image_id": "sha256:" + "b" * 64}, "serena_backend_unsupported"),
])
def test_shape_validation_does_not_decide_backend_admission(overrides, reason):
    backend = overrides.get("execution_backend", "local")
    values = dict(repo=Path("."), task="fixture", execution_backend=backend,
                  docker_profile="ordinary" if backend == "docker" else "frozen")
    values.update(overrides)
    config = RunConfig(**values)
    with pytest.raises(FeatureResolutionError) as caught:
        resolve_features(config)
    assert caught.value.code == "feature_not_supported"
    assert caught.value.context["reason_code"] == reason


def test_ordinary_full_combination_and_local_minimal_combination_are_supported():
    local = RunConfig(repo=Path("."), task="fixture", execution_backend="local")
    assert resolve_features(local).enabled == ()
    full = RunConfig(
        repo=Path("."), task="fixture", docker_recovery_enabled=True,
        docker_image_id="sha256:" + "a" * 64, harness_foundations_enabled=True,
        memory_enabled=True, conversation_enabled=True, agent_config={"skills_enabled": True},
        skills_root=Path("approved"), mcp_serena_image_id="sha256:" + "b" * 64,
        test_target="tests/a.py", source_import="widget", regression_targets=("tests/b.py",),
    )
    assert set(resolve_features(full).enabled) == {
        "memory", "dialogue", "foundations", "skills", "serena", "regression", "recovery",
    }


def test_denial_precedes_filesystem_preparation_and_model_creation(tmp_path):
    calls = []
    config = RunConfig(repo=tmp_path / "missing", task="fixture", memory_enabled=True,
                       output_dir=tmp_path / "runs", env_file=None)
    with pytest.raises(FeatureResolutionError):
        TraceFixRunner(lambda _: calls.append("model")).run(config)
    assert calls == []
    assert not config.output_dir.exists()
