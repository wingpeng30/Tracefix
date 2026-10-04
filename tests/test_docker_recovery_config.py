"""Docker recovery is opt-in and does not change historical config identities."""

import hashlib
from pathlib import Path

import pytest

from tracefix.runtime import RunConfig, config_identity_sha256


def test_recovery_default_and_legacy_identity():
    config = RunConfig(repo=Path("."), task="inspect")
    assert config.docker_recovery_enabled is False
    legacy = config.model_dump(mode="json", exclude={"docker_recovery_enabled"})
    expected = hashlib.sha256(config.model_dump_json(
        exclude={"docker_recovery_enabled"}).encode("utf-8")).hexdigest()
    assert config_identity_sha256(config, legacy) == expected
    assert config_identity_sha256(config) != expected


@pytest.mark.parametrize("backend,profile", [("local", "frozen"), ("docker", "synthetic"),
                                            ("docker", "frozen")])
def test_recovery_rejects_unsupported_backends(backend, profile):
    values = {"execution_backend": backend, "docker_profile": profile}
    if backend == "docker":
        values.update(docker_task_id="fixture", docker_input_root=Path("inputs"))
    if profile == "synthetic":
        values["docker_image_id"] = "sha256:" + "a" * 64
    with pytest.raises(ValueError, match="ordinary Docker backend"):
        RunConfig(repo=Path("."), task="inspect", docker_recovery_enabled=True, **values)


def test_recovery_accepts_explicit_ordinary_profile():
    config = RunConfig(repo=Path("."), task="inspect", execution_backend="docker",
                       docker_profile="ordinary", docker_image_id="sha256:" + "a" * 64,
                       test_target="tests/test_widget.py", source_import="widget",
                       docker_recovery_enabled=True)
    assert config.docker_recovery_enabled is True
