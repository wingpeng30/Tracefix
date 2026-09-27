"""Run one Sphinx selector/file-order probe inside a fresh task container."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from tracefix.provenance import inspect_test_environment
from tracefix.real_benchmark import load_real_issue_tasks
from tracefix.real_experiment import (
    _pytest_command,
    _validation_environment,
    _write_audit_plugin,
)
from tracefix.real_recipes import load_environment_recipes

TASK_ID = "sphinx-doc__sphinx-10449"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_input(root: Path, task_id: str) -> dict:
    manifest = json.loads((root / "input-manifest.json").read_text(encoding="utf-8"))
    if manifest["task_id"] != task_id:
        raise ValueError("input task identity mismatch")
    for name, expected in manifest["files"].items():
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root.resolve()) or not path.is_file() or sha(path) != expected:
            raise ValueError(f"frozen input hash mismatch: {name}")
    return manifest


def clone_source(root: Path, work: Path, commit: str) -> Path:
    source = work / "source"
    for command in (
        ["git", "clone", "--quiet", str(root / "source.bundle"), str(source)],
        ["git", "-C", str(source), "checkout", "--quiet", "--detach", commit],
    ):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise ValueError(f"frozen source restore failed: {result.stderr.strip()}")
    return source


def apply_patch(checkout: Path, patch: Path) -> None:
    result = subprocess.run(
        ["git", "-c", "core.longpaths=true", "apply", "-"],
        cwd=checkout,
        input=patch.read_bytes().replace(b"\r\n", b"\n"),
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--product-patch", type=Path, required=True)
    parser.add_argument("--selector", required=True)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--variant", choices=("base", "gold", "product"), required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()

    root = args.input_root.resolve(strict=True)
    output = args.output_root.resolve()
    output.mkdir(parents=True, exist_ok=False)
    verify_input(root, TASK_ID)
    task = load_real_issue_tasks(root / "tasks", task_ids=(TASK_ID,))[0]
    recipe = load_environment_recipes(root / "recipes")[TASK_ID]
    work = Path("/work")
    work.mkdir(exist_ok=True)
    restore = work / "restore"
    restore.mkdir()
    source = clone_source(root, restore, task.base_commit)
    checkout = work / "checkout"
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", str(source), str(checkout)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "--quiet", "--detach", task.base_commit],
        cwd=checkout,
        check=True,
        capture_output=True,
    )
    apply_patch(checkout, task.test_patch_path)
    product_patch = args.product_patch.resolve(strict=True)
    if args.variant == "gold":
        apply_patch(checkout, task.gold_patch_path)
    elif args.variant == "product":
        apply_patch(checkout, product_patch)

    evidence = checkout / ".tracefix-validation"
    evidence.mkdir()
    _write_audit_plugin(checkout)
    test_python = Path("/opt/python310/bin/python3.10")
    before = inspect_test_environment(test_python)
    env = _validation_environment(checkout, ())
    env["TRACEFIX_SOURCE_IMPORT_PROBE"] = "sphinx"
    env["TRACEFIX_AUDIT_PATH"] = str(evidence / "execution.audit.json")
    env["TRACEFIX_AUDIT_RUN_ID"] = f"sphinx-order:{args.run_label}"
    env["TRACEFIX_AUDIT_STAGE"] = "execution"
    command = list(
        _pytest_command(test_python, checkout, (args.selector,), evidence, recipe)
    )
    command.extend(
        [f"--junitxml={evidence / 'junit.xml'}", "-p", "tracefix_pytest_audit"]
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
        returncode, timed_out = completed.returncode, False
        stdout, stderr = completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        returncode, timed_out = None, True
        stdout = (
            exc.stdout.decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else exc.stdout or ""
        )
        stderr = (
            exc.stderr.decode("utf-8", "replace")
            if isinstance(exc.stderr, bytes)
            else exc.stderr or ""
        )
    (output / "pytest.stdout.txt").write_text(stdout, encoding="utf-8")
    (output / "pytest.stderr.txt").write_text(stderr, encoding="utf-8")

    audit_path = evidence / "execution.audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else {}
    junit = evidence / "junit.xml"
    try:
        cases = ET.parse(junit).findall(".//testcase")
        failures = [
            {
                "node": f"{case.get('classname', '')}::{case.get('name', '')}",
                "detail": (case.findtext("failure") or case.findtext("error") or "")[:3000],
            }
            for case in cases
            if case.find("failure") is not None or case.find("error") is not None
        ]
    except (OSError, ET.ParseError):
        cases, failures = [], []
    after = inspect_test_environment(test_python)
    report = {
        "task_id": TASK_ID,
        "run_label": args.run_label,
        "variant": args.variant,
        "selector": args.selector,
        "source_commit": task.base_commit,
        "input_manifest_sha256": sha(root / "input-manifest.json"),
        "test_patch_sha256": sha(task.test_patch_path),
        "product_patch_sha256": sha(product_patch),
        "applied_product_patch_sha256": (
            sha(product_patch) if args.variant == "product" else None
        ),
        "gold_patch_sha256": sha(task.gold_patch_path),
        "recipe_fingerprint": recipe.fingerprint,
        "python_version": before.python_version,
        "dependency_fingerprint_before": before.fingerprint_sha256,
        "dependency_fingerprint_after": after.fingerprint_sha256,
        "dependency_stable": before.fingerprint_sha256 == after.fingerprint_sha256,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "junit_sha256": sha(junit) if junit.is_file() else None,
        "audit_sha256": sha(audit_path) if audit_path.is_file() else None,
        "collected_node_ids": audit.get("collected_node_ids", []),
        "executed_node_ids": sorted(
            {
                item["nodeid"]
                for item in audit.get("reports", [])
                if item.get("when") == "call" and isinstance(item.get("nodeid"), str)
            }
        ),
        "imported_sphinx_paths": {
            key: value
            for key, value in audit.get("imported_source_paths", {}).items()
            if key == "sphinx" or key.startswith("sphinx.")
        },
        "testcase_count": len(cases),
        "failure_count": len(failures),
        "failures": failures,
        "stdout_tail": stdout[-5000:],
        "stderr_tail": stderr[-2000:],
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
