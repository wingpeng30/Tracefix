"""Exercise the real pytester setup path behind the frozen pytest recipe."""

import json
import sys
from pathlib import Path

from tracefix.real_experiment import _pytest_evidence, _run_qualified_pytest
from tracefix.real_recipes import EnvironmentRecipe


def test_explicit_pyproject_restores_pytester_setup_and_call(tmp_path):
    from test_real_experiment import _fixture

    task, checkout = _fixture(tmp_path)
    task = task.model_copy(update={"fail_to_pass": ("tests/test_fixture.py",)})
    (checkout / "tox.ini").write_text("[pytest]\n", encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = "-p pytester"\n', encoding="utf-8"
    )
    (checkout / "conftest.py").write_text(
        "import pytest\n\n@pytest.fixture\ndef pytester(pytester):\n    return pytester\n",
        encoding="utf-8",
    )
    (checkout / "tests/test_fixture.py").write_text(
        'def test_fixture(pytester):\n    assert pytester.makepyfile("VALUE = 1").is_file()\n',
        encoding="utf-8",
    )
    default_recipe = EnvironmentRecipe(task_id=task.id)
    failed = _run_qualified_pytest(
        task, checkout, Path(sys.executable), "default-tox", default_recipe
    )
    failed_evidence = _pytest_evidence(failed, checkout)
    failed_audit = json.loads(
        (checkout / ".tracefix-validation/execution.audit.json").read_text(encoding="utf-8")
    )
    assert failed_evidence.returncode != 0
    assert not failed_evidence.execution_audit_available
    assert "recursive dependency" in failed.output["stdout"]
    assert {r["when"] for r in failed_audit["reports"]} == {"setup", "teardown"}
    assert any(r["when"] == "setup" and r["outcome"] == "failed" for r in failed_audit["reports"])

    fixed_recipe = default_recipe.model_copy(update={"pytest_config": "pyproject.toml"})
    passed = _run_qualified_pytest(
        task, checkout, Path(sys.executable), "explicit-pyproject", fixed_recipe
    )
    passed_evidence = _pytest_evidence(passed, checkout)
    passed_audit = json.loads(
        (checkout / ".tracefix-validation/execution.audit.json").read_text(encoding="utf-8")
    )
    assert passed_evidence.status == "passed" and passed_evidence.returncode == 0
    assert passed_evidence.execution_audit_available
    assert passed_evidence.collection_audit_available
    assert {r["when"] for r in passed_audit["reports"]} == {"setup", "call", "teardown"}
    assert all(r["outcome"] == "passed" for r in passed_audit["reports"])
