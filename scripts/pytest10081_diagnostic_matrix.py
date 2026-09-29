"""Run the pytest-10081 diagnostic matrix through TraceFix's formal validators.

Host mode creates a disposable, network-isolated frozen-image container and
exports its evidence. Container mode reuses the production qualification and
strict patch validation functions; it does not duplicate build or pytest setup.

Host usage requires the reviewed staging directory and saved product patch:
  python scripts/pytest10081_diagnostic_matrix.py --evidence-root NEW_DIR \
      --product-patch PATCH.diff
The host launches three selectors x three patch states x two repeats (18 cells).
Use --inside only from the owned diagnostic container; it requires an explicit
staging directory, output path, selector, repeat, and container ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import subprocess
import sys
import tarfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

TASK_ID = "pytest-dev__pytest-10081"
NODE_SELECTOR = "testing/test_unittest.py::test_plain_unittest_does_not_support_async"
ISSUE_SELECTOR = "testing/test_unittest.py::test_pdb_teardown_skipped_for_classes[@unittest.skip]"
FILE_SELECTOR = "testing/test_unittest.py"
IMAGE_ID = "sha256:1e2488a5c0e112771dec47fb1d07bd405c8c68e6973d1e91a8d85cf87c384c64"
TEST_PYTHON = "/opt/python310/bin/python3.10"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _docker(command: list[str], *, timeout: int = 1200) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace")[-4000:]
        raise RuntimeError(f"Docker command failed ({result.returncode}): {detail}")
    return result


def _evidence_ok(evidence: object) -> bool:
    return bool(
        evidence is not None
        and getattr(evidence, "execution", None) is not None
        and getattr(evidence, "junit_available", False)
        and getattr(evidence, "audit_available", False)
        and getattr(evidence, "collection_audit_available", False)
        and getattr(evidence, "source_import_audit_valid", False)
    )


def run_inside(
    stage: Path,
    product_patch: Path,
    output: Path,
    selector: str,
    repeat: int,
    container_id: str,
) -> dict:
    sys.path.insert(0, "/opt/tracefix/src")
    sys.path.insert(0, "/opt/tracefix/scripts")
    from docker_reverify import clone_source, verify_input

    from tracefix.real_benchmark import load_real_issue_tasks
    from tracefix.real_experiment import (
        validate_agent_patch_strict,
        validate_real_task_behavior,
    )
    from tracefix.real_recipes import load_environment_recipes

    if output.exists():
        raise ValueError(f"matrix output already exists: {output}")
    manifest = verify_input(stage, TASK_ID)
    recipe = load_environment_recipes(stage / "recipes")[TASK_ID]
    if recipe.fingerprint != manifest["recipe_fingerprint"]:
        raise ValueError("staged recipe identity changed")
    if _sha256(product_patch) == hashlib.sha256(b"").hexdigest():
        raise ValueError("saved product patch is empty")

    task = load_real_issue_tasks(stage / "tasks", task_ids=(TASK_ID,))[0]
    selected = task.model_copy(
        update={
            "fail_to_pass": (selector,),
            "test_command": f"pytest -q {selector}",
        }
    )
    output.mkdir(parents=True, exist_ok=False)
    source = clone_source(stage, output / "source", task.base_commit)
    selected.validate_checkout(source)
    qualification_dir = output / "base-gold"
    behavior = validate_real_task_behavior(
        selected,
        source=source,
        test_python=Path(TEST_PYTHON),
        output_dir=qualification_dir,
        recipe=recipe,
    )
    expected_nodes = behavior.gold_evidence.executed_node_ids
    if not expected_nodes:
        raise RuntimeError("gold qualification produced no executed node IDs")

    patch_dir = output / "saved-patch"
    patch_result = validate_agent_patch_strict(
        selected,
        source=source,
        agent_patch=product_patch,
        test_python=Path(TEST_PYTHON),
        output_dir=patch_dir,
        recipe=recipe,
        expected_node_ids=expected_nodes,
        environment_variables=recipe.environment_variables,
    )

    cells = []
    for variant, evidence, outcome, folder, environment_before, environment_after in (
        (
            "base", behavior.initial_evidence, behavior.qualification_type, "base-gold",
            behavior.environment_before, behavior.environment_after_initial,
        ),
        (
            "gold", behavior.gold_evidence, behavior.qualification_type, "base-gold",
            behavior.environment_before, behavior.environment_after_gold,
        ),
        (
            "saved_patch", patch_result.evidence, patch_result.reason, "saved-patch",
            patch_result.environment_before, patch_result.environment_after,
        ),
    ):
        evidence_data = evidence.model_dump(mode="json") if evidence is not None else None
        cells.append(
            {
                "task_id": TASK_ID,
                "selector": selector,
                "repeat": repeat,
                "variant": variant,
                "workspace_group": folder,
                "container_id": container_id,
                "test_patch_injected": True,
                "qualification_or_reason": outcome,
                "formal_harness_evidence_valid": _evidence_ok(evidence),
                "test_status": getattr(evidence, "status", None),
                "returncode": getattr(evidence, "returncode", None),
                "collected_count": len(getattr(evidence, "collected_node_ids", ())),
                "executed_count": len(getattr(evidence, "executed_node_ids", ())),
                "test_count": getattr(evidence, "test_count", None),
                "failure_count": getattr(evidence, "failure_count", None),
                "error_count": getattr(evidence, "error_count", None),
                "skipped_count": getattr(evidence, "skipped_count", None),
                "dependency_fingerprint_before": environment_before.fingerprint_sha256,
                "dependency_fingerprint_after": environment_after.fingerprint_sha256,
                "evidence": evidence_data,
            }
        )

    report = {
        "kind": "pytest10081_formal_harness_diagnostic_cell",
        "created_at": datetime.now(UTC).isoformat(),
        "task_id": TASK_ID,
        "selector": selector,
        "repeat": repeat,
        "source_commit": task.base_commit,
        "input_manifest_sha256": _sha256(stage / "input-manifest.json"),
        "recipe_fingerprint": recipe.fingerprint,
        "product_patch_sha256": _sha256(product_patch),
        "controller_python": sys.version,
        "test_python": TEST_PYTHON,
        "hidden_test_patch_injected_for_all_variants": True,
        "build_step": "setup.py --version via shared formal validators",
        "cells": cells,
        "base_gold_qualification": behavior.model_dump(mode="json"),
        "saved_patch_strict_validation": patch_result.model_dump(mode="json"),
    }
    report_path = output / "matrix-cell.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def _host(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve(strict=True)
    evidence_root = Path(args.evidence_root).resolve()
    evidence_root.mkdir(parents=True, exist_ok=False)
    product_patch = Path(args.product_patch).resolve(strict=True)
    task_id = TASK_ID

    snapshot_files: set[Path] = set()
    ignored_parts = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    for top in ("src", "scripts", "tests", "docker"):
        base = repo / top
        for path in base.rglob("*"):
            relative = path.relative_to(repo)
            if any(
                part in ignored_parts or part.startswith(".tmp-workspace-")
                for part in relative.parts
            ):
                continue
            if path.is_file() and path.suffix.lower() not in {".pem", ".key"}:
                snapshot_files.add(relative)
    for name in (
        "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini", "AGENTS.md",
        "docs/roadmap.md", "docs/development-history.md",
        "docs/experiments/2026-09-27-environment-qualification.md",
        "docs/handoffs/2026-09-17-v084.md",
        "docs/goals/2026-09-27-container-qualification.md",
        "docs/goals/2026-09-27-container-qualification.json",
    ):
        path = repo / name
        if path.is_file():
            snapshot_files.add(path.relative_to(repo))
    snapshot_manifest = []
    snapshot_path = evidence_root / "execution-source-snapshot.zip"
    with zipfile.ZipFile(snapshot_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative in sorted(snapshot_files, key=lambda item: item.as_posix().casefold()):
            data = (repo / relative).read_bytes()
            archive.writestr(f"D_Tracefix/{relative.as_posix()}", data)
            snapshot_manifest.append(
                {
                    "path": relative.as_posix(),
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=repo, capture_output=True, check=True
    ).stdout
    git_status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    identity = {
        "head": head,
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "git_status_sha256": hashlib.sha256(git_status).hexdigest(),
        "git_status_line_count": len(git_status.splitlines()),
        "snapshot_path": snapshot_path.name,
        "snapshot_sha256": _sha256(snapshot_path),
        "snapshot_file_count": len(snapshot_manifest),
        "snapshot_files": snapshot_manifest,
        "python_version": sys.version,
        "supplier_calls": 0,
        "formal_experiment": False,
        "holdout_used": False,
        "excluded_extensions": [".pem", ".key"],
    }
    (evidence_root / "execution-identity.json").write_text(
        json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    stage_output = evidence_root / "input-stage"
    stage_output.mkdir(parents=True, exist_ok=False)

    sys.path.insert(0, str(repo / "src"))
    sys.path.insert(0, str(repo / "scripts"))
    from prepare_docker_reverify import stage as prepare_stage

    task_dir = repo / "benchmarks" / "real_candidates" / task_id
    source_recipe_path = repo / "benchmarks" / "real_recipes" / f"{task_id}.json"
    source_recipe = json.loads(source_recipe_path.read_text(encoding="utf-8"))
    derived_recipe = dict(source_recipe)
    derived_recipe["python_versions"] = ["3.10"]
    derived_recipe["supported_platforms"] = ["linux"]
    recipe_path = evidence_root / f"{task_id}-docker310-recipe.json"
    recipe_path.write_text(
        json.dumps(derived_recipe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    recipe_override = {
        "source_recipe_path": str(source_recipe_path),
        "source_recipe_sha256": _sha256(source_recipe_path),
        "source_recipe": source_recipe,
        "derived_recipe_path": str(recipe_path),
        "derived_recipe_sha256": _sha256(recipe_path),
        "derived_recipe": derived_recipe,
        "difference_reason": (
            "Use the locked image's Python 3.10 interpreter and Linux platform; the repository's "
            "Windows/local recipe metadata is not the Docker task runtime identity."
        ),
    }
    (evidence_root / "recipe-override.json").write_text(
        json.dumps(recipe_override, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    source = repo / "runs" / "real-candidate-validation-v080b" / task_id
    protocol = (
        repo / "benchmarks" / "experiments" / "v0.8.21-no-effect-recovery-execution"
        / "protocol.json"
    )
    manifest_path = prepare_stage(
        task_dir=task_dir,
        recipe_path=recipe_path,
        source=source,
        patch=product_patch,
        protocol=protocol,
        output=stage_output / "stage",
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    image = IMAGE_ID
    inspect = _docker(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    actual_image = inspect.stdout.decode().strip()
    if actual_image != image:
        raise RuntimeError(f"frozen pytest image mismatch: {actual_image}")

    container = f"tracefix-g1-pytest-{uuid4().hex[:12]}"
    _docker(
        [
            "docker", "create", "--name", container,
            "--label", "tracefix.goal=container-qualification-20260927",
            "--network", "none", "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL", "--pids-limit", "512", "--memory", "6g",
            "--env", "HTTP_PROXY=", "--env", "HTTPS_PROXY=", "--env", "ALL_PROXY=",
            "--env", "http_proxy=", "--env", "https_proxy=", "--env", "all_proxy=",
            image, "sleep", "infinity",
        ]
    )
    container_id = (
        _docker(["docker", "inspect", "--format", "{{.Id}}", container])
        .stdout.decode()
        .strip()
    )
    _docker(["docker", "start", container])
    _docker(
        [
            "docker", "exec", container, "mkdir", "-p", "/input",
            "/opt/tracefix/scripts", "/work/g1-matrix",
        ]
    )
    _docker(["docker", "cp", str(stage_output / "stage"), f"{container}:/input"])
    _docker(["docker", "cp", str(repo / "src"), f"{container}:/opt/tracefix"])
    _docker(
        [
            "docker", "cp", str(repo / "scripts" / "docker_reverify.py"),
            f"{container}:/opt/tracefix/scripts/docker_reverify.py",
        ]
    )
    _docker(
        [
            "docker", "cp", str(Path(__file__).resolve()),
            f"{container}:/opt/tracefix/scripts/pytest10081_diagnostic_matrix.py",
        ]
    )
    _docker(["docker", "cp", str(product_patch), f"{container}:/input/saved-product.patch"])

    selectors = (
        ("issue-target", ISSUE_SELECTOR),
        ("warning-node", NODE_SELECTOR),
        ("public-file", FILE_SELECTOR),
    )
    results = []
    host_log = evidence_root / "host-matrix.log"
    export_complete = False
    try:
        for key, selector in selectors:
            for repeat in (1, 2):
                target = f"/work/g1-matrix/{key}-repeat-{repeat}"
                command = [
                    "docker", "exec", container,
                    "python", "/opt/tracefix/scripts/pytest10081_diagnostic_matrix.py",
                    "--inside", "--stage", "/input/stage",
                    "--product-patch", "/input/saved-product.patch",
                    "--output", target, "--selector", selector, "--repeat", str(repeat),
                    "--container-id", container_id,
                ]
                started = time.monotonic()
                run = subprocess.run(command, capture_output=True, timeout=1800, check=False)
                record = {
                    "selector_key": key,
                    "selector": selector,
                    "repeat": repeat,
                    "command": command,
                    "returncode": run.returncode,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "stdout": run.stdout.decode("utf-8", "replace"),
                    "stderr": run.stderr.decode("utf-8", "replace"),
                }
                results.append(record)
                with host_log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                print(
                    json.dumps(
                        {
                            key: record[key]
                            for key in (
                                "selector_key", "repeat", "returncode", "duration_seconds"
                            )
                        }
                    ),
                    flush=True,
                )
                if run.returncode:
                    break
            if results[-1]["returncode"]:
                break

        export = evidence_root / "container-evidence"
        export.mkdir(exist_ok=False)
        archive_in_container = "/work/g1-matrix-evidence.tar"
        packed = _docker(
            ["docker", "exec", container, "tar", "-cf", archive_in_container,
             "-C", "/work/g1-matrix", "."]
        )
        del packed
        archive_on_host = evidence_root / "container-evidence.tar"
        _docker(["docker", "cp", f"{container}:{archive_in_container}", str(archive_on_host)])
        symlinks = []
        extracted_files = []
        with tarfile.open(archive_on_host, "r:") as archive:
            for member in archive.getmembers():
                relative = posixpath.normpath(member.name)
                if relative in (".", "") or relative.startswith("../") or relative.startswith("/"):
                    if relative not in (".", ""):
                        raise RuntimeError(f"unsafe path in container evidence tar: {member.name}")
                    continue
                if member.issym() or member.islnk():
                    symlinks.append({"path": relative, "linkname": member.linkname,
                                     "kind": "symlink" if member.issym() else "hardlink"})
                    continue
                if member.isdir():
                    (export / Path(*relative.split("/"))).mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise RuntimeError(
                        f"unsupported entry in container evidence tar: {member.name}"
                    )
                target = export / Path(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                source_file = archive.extractfile(member)
                if source_file is None:
                    raise RuntimeError(f"cannot read tar member: {member.name}")
                target.write_bytes(source_file.read())
                extracted_files.append({"path": relative, "size": target.stat().st_size,
                                        "sha256": _sha256(target)})
        (evidence_root / "container-evidence-symlinks.json").write_text(
            json.dumps(symlinks, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (evidence_root / "container-evidence-manifest.json").write_text(
            json.dumps({"archive_sha256": _sha256(archive_on_host),
                        "regular_files": extracted_files,
                        "symlinks": symlinks}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        export_complete = True
        matrix_cells = []
        for cell_report in sorted(export.glob("*/matrix-cell.json")):
            cell_data = json.loads(cell_report.read_text(encoding="utf-8"))
            matrix_cells.extend(cell_data["cells"])
        outcome = {
            "kind": "pytest10081_diagnostic_matrix_host_summary",
            "status": (
                "complete"
                if len(matrix_cells) == 18
                and all(cell["formal_harness_evidence_valid"] for cell in matrix_cells)
                else "preparation_failed" if not matrix_cells else "incomplete_or_invalid_evidence"
            ),
            "created_at": datetime.now(UTC).isoformat(),
            "task_id": task_id,
            "container_id": container_id,
            "image_id": actual_image,
            "input_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "recipe_fingerprint": manifest["recipe_fingerprint"],
            "recipe_override_sha256": _sha256(evidence_root / "recipe-override.json"),
            "product_patch_sha256": _sha256(product_patch),
            "workspace_head": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout.strip(),
            "workspace_source_snapshot_sha256": identity["snapshot_sha256"],
            "tracked_diff_sha256": identity["tracked_diff_sha256"],
            "matrix_commands": results,
            "all_commands_completed": len(results) == 6
            and all(item["returncode"] == 0 for item in results),
            "matrix_cells": [
                {
                    "selector": cell["selector"],
                    "repeat": cell["repeat"],
                    "variant": cell["variant"],
                    "test_patch_injected": cell["test_patch_injected"],
                    "test_status": cell["test_status"],
                    "returncode": cell["returncode"],
                    "formal_harness_evidence_valid": cell["formal_harness_evidence_valid"],
                }
                for cell in matrix_cells
            ],
            "matrix_cells_valid": len(matrix_cells) == 18
            and all(cell["formal_harness_evidence_valid"] for cell in matrix_cells)
            and all(cell["test_patch_injected"] for cell in matrix_cells),
            "supplier_calls": 0,
            "formal_experiment": False,
            "holdout_used": False,
            "input_manifest_path": str(manifest_path),
            "raw_evidence_path": str(export),
            "host_log_path": str(host_log),
        }
        (evidence_root / "matrix-summary.json").write_text(
            json.dumps(outcome, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0 if outcome["all_commands_completed"] and outcome["matrix_cells_valid"] else 1
    except Exception as exc:
        (evidence_root / "evidence-export-or-run-failure.json").write_text(
            json.dumps({"status": "incomplete_unaccepted", "error": repr(exc),
                        "container_name": container, "container_id": container_id,
                        "image_id": actual_image, "command_results": results,
                        "host_log_path": str(host_log),
                        "recovery_action": "container retained when export is incomplete"},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise
    finally:
        # Keep the owned container if evidence export failed so it can be recovered.
        if export_complete:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--product-patch", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--selector", choices=(NODE_SELECTOR, ISSUE_SELECTOR, FILE_SELECTOR)
    )
    parser.add_argument("--repeat", type=int)
    parser.add_argument("--container-id")
    parser.add_argument("--repo", default=r"D:\Tracefix")
    parser.add_argument("--evidence-root")
    args = parser.parse_args()
    if args.inside:
        if (
            not all((args.stage, args.product_patch, args.output, args.selector, args.container_id))
            or args.repeat not in (1, 2)
            or args.evidence_root is not None
        ):
            parser.error(
                "container mode requires stage, patch, output, selector, container ID and repeat"
            )
        run_inside(
            args.stage, args.product_patch, args.output,
            args.selector, args.repeat, args.container_id,
        )
        return 0
    if (
        not args.evidence_root
        or not args.product_patch
        or args.stage is not None
        or args.output is not None
        or args.selector is not None
        or args.container_id is not None
        or args.repeat is not None
    ):
        parser.error(
            "host mode requires --evidence-root and --product-patch; "
            "container-only options require --inside"
        )
    if not args.product_patch.is_file() or args.product_patch.stat().st_size == 0:
        parser.error("--product-patch must name a non-empty file")
    if args.evidence_root.exists():
        parser.error("--evidence-root must be a new path; existing evidence is never overwritten")
    return _host(args)


if __name__ == "__main__":
    raise SystemExit(main())
