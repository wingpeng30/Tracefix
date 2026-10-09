"""Build a host-only catalog from sealed IDs and public regression rules; no model calls."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

from tracefix.comparison import read_json, write_json
from tracefix.comparison_holdout import FREEZE, HOLDOUT_TASK_IDS
from tracefix.exceptions import BenchmarkError
from tracefix.real_environment import resolve_managed_environment_python


def public_regressions(source: Path, task: str) -> list[str]:
    if task.startswith("pytest"):
        regressions = [
            "testing/test_compat.py::" + name
            for name in (
                "test_is_generator",
                "test_real_func_loop_limit",
                "test_get_real_func",
                "test_get_real_func_partial",
                "test_helper_failures",
                "test_safe_getattr",
                "test_safe_isclass",
                "test_cached_property",
            )
        ]
        # Version compatibility is determined from public base source, never pass/fail results.
        module = ast.parse((source / "testing/test_compat.py").read_text(encoding="utf-8"))
        names = {node.name for node in module.body if isinstance(node, ast.FunctionDef)}
        regressions = [node for node in regressions if node.split("::")[-1] in names]
        if not regressions:
            raise ValueError("no public compatibility regression nodes")
    elif task.startswith("sphinx"):
        regressions = ["tests/test_util_matching.py"]
    elif task.startswith("pylint"):
        regressions = ["tests/test_numversion.py", "tests/test_pragma_parser.py"]
    elif (source / "tests/test_structures.py").exists():
        regressions = ["tests/test_structures.py", "tests/test_hooks.py"]
    else:
        regressions = [
            "test_requests.py::RequestsTestCase::" + name
            for name in (
                "test_entry_points",
                "test_invalid_url",
                "test_basic_building",
                "test_path_is_not_double_encoded",
                "test_params_are_added_before_fragment",
            )
        ]
        module = ast.parse((source / "test_requests.py").read_text(encoding="utf-8"))
        methods = {
            node.name: cls.name
            for cls in module.body
            if isinstance(cls, ast.ClassDef)
            for node in cls.body
            if isinstance(node, ast.FunctionDef)
        }
        regressions = [
            "test_requests.py::" + methods[node.split("::")[-1]] + "::" + node.split("::")[-1]
            for node in regressions
            if node.split("::")[-1] in methods
        ]
        if not regressions:
            raise ValueError("no public Requests regression nodes")
    return regressions


def build_catalog(project: Path, service_identity: dict | None = None) -> list[dict]:
    freeze = read_json(FREEZE)
    if freeze["selected_task_ids"] != list(HOLDOUT_TASK_IDS):
        raise ValueError("sealed task identities changed")
    specifications = []
    for task in freeze["selected_task_ids"]:
        roots = [
            project / "runs/holdout-source-20260921" / task,
            project / "runs/holdout-source-replacements-20260922" / task,
        ]
        source = next((p for p in roots if p.is_dir()), None)
        if source is None:
            raise ValueError(f"missing frozen source: {task}")
        try:
            python = resolve_managed_environment_python(
                project / "runs/holdout-envs-compatible-20260922", task
            )
        except BenchmarkError:
            python = resolve_managed_environment_python(
                project / "runs/holdout-envs-20260921", task
            )
        recipe = read_json(project / "benchmarks/holdout_recipes" / f"{task}.json")
        regressions = public_regressions(source, task)
        spec = {
            "task_id": task,
            "kind": "real",
            "source": str(source.resolve()),
            "python": str(python),
            "repository": task.split("__")[0],
            "task_root": str(project / "benchmarks/holdout_candidates"),
            "recipe": recipe,
            "regressions": regressions,
        }
        if task.startswith("psf"):
            # Probe a real public product module. requests.packages aliases installed dependencies;
            # probing that alias namespace would misclassify legitimate urllib3 imports as product.
            recipe["source_import_probe"] = "requests.sessions"
            if service_identity is None:
                raise ValueError("Requests qualification requires controlled service identity")
            recipe.setdefault("environment_variables", {})["HTTPBIN_URL"] = (
                f"http://127.0.0.1:{service_identity['port']}/"
            )
            recipe["service_health_url"] = service_identity["health_url"]
            spec["service_identity"] = service_identity
        specifications.append(spec)
    return specifications


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--service-identity", type=Path, required=True)
    args = parser.parse_args()
    write_json(args.output, build_catalog(args.project.resolve(), read_json(args.service_identity)))
    print("Host-only twenty-task catalog written; supplier calls: 0")


if __name__ == "__main__":
    main()
