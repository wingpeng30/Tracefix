"""Exercise real local source cloning and safe qualification reuse for preparation."""

import importlib
import subprocess
from pathlib import Path

import pytest
from test_comparison import fixture_catalog

from tracefix.comparison import read_json, write_json
from tracefix.comparison_campaign import source_identity
from tracefix.provenance import inspect_test_environment


def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("prepare_ac_catalog")


def test_fixed_source_materializes_base_and_refuses_changed_checkout(tmp_path, monkeypatch):
    preparation = module(monkeypatch)
    catalog, _ = fixture_catalog(tmp_path)
    spec = read_json(catalog)[0]
    source = Path(spec["source"])
    identity = source_identity(source)
    metadata = {"base_commit": identity["commit"], "repo_url": str(source)}
    fixed = preparation.fixed_source(tmp_path, tmp_path / "owned", "sample", metadata, source)
    assert source_identity(fixed)["commit"] == identity["commit"]
    assert set(source_identity(fixed)["files"]) == set(identity["files"])
    assert (fixed / "sample.py").read_bytes() == subprocess.check_output(
        ["git", "show", "HEAD:sample.py"], cwd=source
    )
    assert (
        preparation.fixed_source(tmp_path, tmp_path / "owned", "sample", metadata, source) == fixed
    )
    (fixed / "sample.py").write_text("changed")
    with pytest.raises(ValueError, match="changed preparation"):
        preparation.fixed_source(
            tmp_path, tmp_path / "owned", "sample", {**metadata, "base_commit": "HEAD~1"}, source
        )


def test_cached_qualification_requires_exact_spec_source_and_environment(tmp_path, monkeypatch):
    preparation = module(monkeypatch)
    catalog, _ = fixture_catalog(tmp_path)
    spec = read_json(catalog)[0]
    record = {
        **spec,
        "kind": "real",
        "qualification": {
            "eligible_for_llm_prescreen": True,
            "qualification_type": "assertion_failure",
        },
        "source_identity": source_identity(Path(spec["source"])),
        "environment_identity": inspect_test_environment(Path(spec["python"])).fingerprint_sha256,
    }
    path = tmp_path / "cached.json"
    assert preparation.reuse_qualification(path) is None
    write_json(path, record)
    assert preparation.reuse_qualification(path, {"task_id": record["task_id"]}) == record
    with pytest.raises(ValueError, match="specification"):
        preparation.reuse_qualification(path, {"task_id": "changed"})
    write_json(path, {**record, "environment_identity": "changed"})
    with pytest.raises(ValueError, match="identity"):
        preparation.reuse_qualification(path)
    write_json(path, record)
    (Path(spec["source"]) / "sample.py").write_text("changed")
    with pytest.raises(ValueError, match="clean"):
        preparation.reuse_qualification(path)


def test_public_regression_rules_use_public_base_version(tmp_path, monkeypatch):
    preparation = module(monkeypatch)
    source = tmp_path
    (source / "testing").mkdir()
    (source / "testing/test_compat.py").write_text("def test_is_generator(): pass\n")
    assert preparation.public_regressions(source, "pytest-dev__pytest-new") == [
        "testing/test_compat.py::test_is_generator"
    ]
    assert preparation.public_regressions(source, "sphinx-doc__sphinx-new") == [
        "tests/test_util_matching.py"
    ]
    assert preparation.public_regressions(source, "pylint-dev__pylint-new") == [
        "tests/test_numversion.py",
        "tests/test_pragma_parser.py",
    ]
