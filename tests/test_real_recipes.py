from __future__ import annotations

import json

import pytest

from tracefix.exceptions import BenchmarkError
from tracefix.real_recipes import EnvironmentRecipe, load_environment_recipes


@pytest.mark.parametrize("entry", ["relative/path", "/work/../outside"])
def test_recipe_rejects_unsafe_container_pythonpath(entry: str) -> None:
    with pytest.raises(ValueError, match="safe absolute container paths"):
        EnvironmentRecipe(task_id="fixture-task", test_pythonpath_entries=(entry,))


@pytest.mark.parametrize(
    "variables",
    [
        {"not valid": "value"},
        {"TRACEFIX_TOKEN": "secret"},
        {"PYTEST_ADDOPTS": "-q"},
    ],
)
def test_recipe_rejects_invalid_or_sensitive_environment_variables(variables) -> None:
    with pytest.raises(ValueError, match="recipe (environment variable|cannot inject)"):
        EnvironmentRecipe(task_id="fixture-task", environment_variables=variables)


@pytest.mark.parametrize(
    ("target", "health"),
    [
        ("http://example.invalid/", "http://example.invalid/get"),
        ("http://localhost:90/", "http://localhost:91/get"),
        ("https://localhost/", "https://localhost/get"),
        ("https://localhost/", "http://127.0.0.1/get"),
    ],
)
def test_recipe_rejects_nonlocal_or_mismatched_service_health_urls(target, health) -> None:
    with pytest.raises(ValueError, match="one local service"):
        EnvironmentRecipe(
            task_id="psf__requests-1766",
            environment_variables={"HTTPBIN_URL": target},
            service_health_url=health,
        )


def test_recipe_loader_handles_absent_invalid_and_duplicate_inputs(tmp_path) -> None:
    assert load_environment_recipes(tmp_path / "missing") == {}
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="invalid environment recipe"):
        load_environment_recipes(tmp_path)

    (tmp_path / "broken.json").unlink()
    data = {"task_id": "fixture-task"}
    (tmp_path / "one.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "two.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BenchmarkError, match="duplicate environment recipe"):
        load_environment_recipes(tmp_path)


def test_recipe_build_commands_pytest_config_and_selector_reason_are_validated():
    with pytest.raises(ValueError, match="build commands cannot be empty"):
        EnvironmentRecipe(task_id="fixture-task", build_commands=((),))
    with pytest.raises(ValueError, match=r"must start with \{python\}"):
        EnvironmentRecipe(task_id="fixture-task", build_commands=(("pip", "install"),))
    with pytest.raises(ValueError, match="supported repository config"):
        EnvironmentRecipe(task_id="fixture-task", pytest_config="external.ini")
    with pytest.raises(ValueError, match="documented reason"):
        EnvironmentRecipe(task_id="fixture-task", selector_overrides=("tests/test_x.py",))


def test_recipe_platform_python_and_fingerprint_compatibility():
    recipe = EnvironmentRecipe(
        task_id="fixture-task", python_versions=("3.11",), supported_platforms=("windows",)
    )
    assert recipe.supports_python("3.11.9")
    assert not recipe.supports_python("3.10.14")
    assert recipe.supports_current_platform()
    assert len(recipe.fingerprint) == 64
    unconstrained = EnvironmentRecipe(task_id="fixture-task")
    assert unconstrained.supports_python("3.12.1")
