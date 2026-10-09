"""Requalify twenty tasks and deterministically add ten; never invoke a model."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from prepare_holdout_catalog import public_regressions

from tracefix.comparison import file_sha, read_json, write_json
from tracefix.comparison_campaign import qualify_task, source_identity
from tracefix.comparison_holdout import HOLDOUT_TASK_IDS
from tracefix.comparison_selection import next_candidate, validate_selection
from tracefix.provenance import inspect_test_environment
from tracefix.real_environment import EnvironmentPreparationConfig, RealEnvironmentPreparer


def reuse_qualification(path: Path, expected: dict | None = None) -> dict | None:
    if not path.exists():
        return None
    record = read_json(path)
    if expected is not None and any(record.get(k) != v for k, v in expected.items()):
        raise ValueError("preparation qualification specification changed")
    evidence = record.get("qualification", {})
    if (
        record.get("kind") != "real"
        or not evidence.get("eligible_for_llm_prescreen")
        or evidence.get("qualification_type") != "assertion_failure"
        or source_identity(Path(record["source"])) != record["source_identity"]
        or inspect_test_environment(Path(record["python"])).fingerprint_sha256
        != record["environment_identity"]
    ):
        raise ValueError("preparation qualification identity changed")
    return record


def fixed_source(project: Path, output: Path, task: str, metadata: dict, template: Path) -> Path:
    source = output / "sources" / task
    freshly_cloned = not source.exists()

    def git(*args: str, cwd: Path | None = None, check: bool = True):
        return subprocess.run(
            ["git", "-c", "http.proxy=", "-c", "https.proxy=", *args],
            cwd=cwd,
            check=check,
            capture_output=True,
            text=True,
        )

    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        existing = project / "runs/holdout-source-20260921" / task
        origin = existing if (existing / ".git").exists() else template
        git(
            "clone",
            "-c",
            "core.autocrlf=false",
            "--no-hardlinks",
            "--no-checkout",
            str(origin),
            str(source),
        )
        git("remote", "set-url", "origin", metadata["repo_url"], cwd=source)
    if not freshly_cloned and git("status", "--porcelain", cwd=source).stdout.strip():
        raise ValueError("changed preparation source cannot be reset")
    present = git("cat-file", "-e", metadata["base_commit"] + "^{commit}", cwd=source, check=False)
    if present.returncode:
        git("fetch", "--no-tags", "origin", metadata["base_commit"], cwd=source)
    current = git("rev-parse", "HEAD", cwd=source, check=False).stdout.strip()
    if freshly_cloned or current != metadata["base_commit"]:
        git("checkout", "--detach", metadata["base_commit"], cwd=source)
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parent-protocol", type=Path, required=True)
    parser.add_argument("--service-identity", type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "task-freeze.json").exists():
        raise ValueError("selection is already frozen")
    pool_path = project / "benchmarks/holdout_candidates/candidate-pool.json"
    pool = read_json(pool_path)
    previous = read_json(args.parent_protocol)["tasks"]
    service = read_json(args.service_identity)
    specs = []
    for task in HOLDOUT_TASK_IDS:
        original = previous[task]
        metadata = read_json(project / "benchmarks/holdout_candidates" / task / "task.json")
        source = fixed_source(project, output, task, metadata, Path(original["source"]))
        spec = {k: original[k] for k in ("task_id", "kind", "python", "recipe", "regressions")}
        spec.update(
            source=str(source),
            repository=metadata["repo"],
            task_root=str(project / "benchmarks/holdout_candidates"),
        )
        if task.startswith("psf"):
            spec["recipe"] = {
                **spec["recipe"],
                "environment_variables": {"HTTPBIN_URL": f"http://127.0.0.1:{service['port']}/"},
                "service_health_url": service["health_url"],
            }
            spec["service_identity"] = service
        previous_qualification = (
            output
            / "original-qualification"
            / "qualification"
            / task
            / "comparison-qualification.json"
        )
        if reuse_qualification(previous_qualification, spec) is None:
            qualify_task(spec, output / "original-qualification")
        specs.append(spec)
        print(f"original-qualified {task}", flush=True)

    selected = list(HOLDOUT_TASK_IDS)
    attempts_path = output / "selection-attempts.json"
    attempts = read_json(attempts_path) if attempts_path.exists() else []
    for attempt in attempts:
        if attempt["accepted"]:
            selected.append(attempt["task_id"])
            specs.append(read_json(Path(attempt["evidence_path"])))
    attempted = {a["task_id"] for a in attempts}
    excluded = set(pool["excluded_task_ids"])
    templates = {s["repository"]: s for s in specs[:20]}
    while len(selected) < 30:
        task = next_candidate(pool, selected, attempted, excluded)
        metadata = read_json(project / "benchmarks/holdout_candidates" / task / "task.json")
        directory = output / "additional-qualification" / "qualification" / task
        directory.mkdir(parents=True, exist_ok=True)
        try:
            evidence = directory / "comparison-qualification.json"
            cached = reuse_qualification(evidence)
            template = templates[metadata["repo"]]
            source = fixed_source(project, output, task, metadata, Path(template["source"]))
            recipe_path = project / "benchmarks/holdout_recipes" / f"{task}.json"
            recipe = (
                read_json(recipe_path)
                if recipe_path.exists()
                else {**template["recipe"], "task_id": task}
            )
            recipe_root = output / "recipes"
            write_json(recipe_root / f"{task}.json", recipe)
            python = (
                Path("D:/anaconda3/envs/FinalProject/python.exe")
                if task.startswith("pylint")
                else (Path("D:/anaconda3/envs/irllrec/python.exe"))
            )
            prepared = (
                None
                if cached
                else RealEnvironmentPreparer().prepare(
                    EnvironmentPreparationConfig(
                        tasks_dir=project / "benchmarks/holdout_candidates",
                        task_ids=(task,),
                        source_root=output / "sources",
                        environment_root=output / "environments",
                        output_dir=directory / "environment",
                        recipes_dir=recipe_root,
                        python_executable=python,
                        allow_create_interpreter=False,
                        timeout_seconds=900,
                    )
                )
            )
            environment = prepared.results[0] if prepared else None
            if environment is not None and environment.status not in {"ready", "reused"}:
                raise RuntimeError(f"environment {environment.status}; see {prepared.summary_path}")
            spec = {
                "task_id": task,
                "kind": "real",
                "source": str(source),
                "python": environment.python_executable if environment else cached["python"],
                "recipe": recipe,
                "repository": metadata["repo"],
                "task_root": str(project / "benchmarks/holdout_candidates"),
                "regressions": public_regressions(source, task),
            }
            qualified = cached or qualify_task(spec, output / "additional-qualification")
            evidence = directory / "comparison-qualification.json"
        except Exception as exc:
            evidence = directory / "selection-rejection.json"
            write_json(
                evidence,
                {"task_id": task, "accepted": False, "reason": f"{type(exc).__name__}: {exc}"},
            )
            accepted = False
        else:
            accepted = True
            selected.append(task)
            specs.append(qualified)
        attempted.add(task)
        attempts.append(
            {
                "task_id": task,
                "accepted": accepted,
                "evidence_path": str(evidence),
                "evidence_sha256": file_sha(evidence),
            }
        )
        write_json(attempts_path, attempts)
        print(f"additional-qualified {task}: {accepted}; selected={len(selected)}", flush=True)
    freeze = {
        "kind": "tracefix_ac_thirty_selection",
        "pool_path": str(pool_path),
        "pool_sha256": file_sha(pool_path),
        "selected_task_ids": selected,
        "excluded_task_ids": sorted(excluded),
        "attempts": attempts,
        "supplier_calls": 0,
    }
    validate_selection(freeze, selected)
    write_json(output / "task-freeze.json", freeze)
    write_json(output / "catalog.json", specs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
