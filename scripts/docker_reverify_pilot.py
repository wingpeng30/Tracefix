"""Offline Linux replay of one frozen pytest task; run inside the pilot container."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from tracefix.real_benchmark import load_real_issue_tasks
from tracefix.real_experiment import validate_agent_patch_strict, validate_real_task_behavior
from tracefix.real_recipes import load_environment_recipes

TASK_ID = "pytest-dev__pytest-10081"
SEQUENCE = 26
INPUT = Path("/input")
OUTPUT = Path("/output")
WORK = Path("/work")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if not INPUT.is_dir() or not OUTPUT.is_dir():
        raise RuntimeError("expected separate /input and /output mounts")
    task = load_real_issue_tasks(
        INPUT / "benchmarks/real_candidates", task_ids=(TASK_ID,)
    )[0]
    recipe = load_environment_recipes(INPUT / "benchmarks/real_recipes")[TASK_ID]
    source = INPUT / "runs/real-candidate-validation-v080b" / TASK_ID
    patch = (
        INPUT / "runs/evidence-rescue-20260926-v1/patches-other-elevated"
        / "derived-product-patches/026.diff"
    )
    protocol = json.loads(
        (INPUT / "runs/no-effect-recovery-paid-development-20260926-v1/experiment/protocol.json")
        .read_text(encoding="utf-8")
    )
    expected_nodes = tuple(protocol["p1_qualifications"][TASK_ID]["expected_node_ids"])
    task.validate_checkout(source)
    frozen = json.loads(
        (INPUT / "runs/evidence-rescue-20260926-v1/summary.json").read_text(encoding="utf-8")
    )
    row = next(item for item in frozen["rows"] if item["sequence"] == SEQUENCE)
    if digest(patch) != row["product_patch_sha256"]:
        raise RuntimeError("derived product patch hash changed")
    image = subprocess.run(
        ["cat", "/etc/os-release"], capture_output=True, text=True, check=True
    ).stdout
    record = {
        "kind": "docker_reverification_pilot",
        "task_id": TASK_ID,
        "sequence": SEQUENCE,
        "source_commit": task.base_commit,
        "test_patch_sha256": digest(task.test_patch_path),
        "gold_patch_sha256": digest(task.gold_patch_path),
        "product_patch_sha256": digest(patch),
        "recipe_sha256": recipe.fingerprint,
        "original_recipe_supported_platforms": recipe.supported_platforms,
        "linux_recipe_status": "experimental; old Windows recipe not modified",
        "checkout_filesystem": "container Linux overlay at /work",
        "python": platform.python_version(),
        "os_release": image,
        "pip_freeze": subprocess.run(
            [sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, check=True
        ).stdout.splitlines(),
        "results": [],
    }

    def save() -> None:
        (OUTPUT / "pilot-result.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save()

    WORK.mkdir(parents=True, exist_ok=False)

    def mirror_evidence(destination: Path) -> None:
        if not destination.is_dir():
            return
        for checkout in destination.iterdir():
            evidence = checkout / ".tracefix-validation"
            if evidence.is_dir():
                target = OUTPUT / "evidence" / destination.name / checkout.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(evidence, target)

    for repeat in (1, 2):
        destination = WORK / f"qualification-{repeat}"
        try:
            result = validate_real_task_behavior(
                task, source=source, test_python=Path(sys.executable),
                output_dir=destination, recipe=recipe,
            )
            entry = {"stage": "qualification", "repeat": repeat,
                     "result": result.model_dump(mode="json")}
        except Exception as exc:
            entry = {"stage": "qualification", "repeat": repeat,
                     "error_type": type(exc).__name__, "error": str(exc)}
        mirror_evidence(destination)
        record["results"].append(entry)
        save()
        if "error_type" in entry or not entry["result"]["eligible_for_llm_prescreen"]:
            return 1
    for repeat in (1, 2):
        destination = WORK / f"patch-{SEQUENCE:03}-{repeat}"
        try:
            result = validate_agent_patch_strict(
                task, source=source, agent_patch=patch,
                test_python=Path(sys.executable), output_dir=destination,
                recipe=recipe, expected_node_ids=expected_nodes,
                environment_variables=recipe.environment_variables,
            )
            entry = {"stage": "patch", "repeat": repeat,
                     "result": result.model_dump(mode="json")}
        except Exception as exc:
            entry = {"stage": "patch", "repeat": repeat,
                     "error_type": type(exc).__name__, "error": str(exc)}
        mirror_evidence(destination)
        record["results"].append(entry)
        save()
        if "error_type" in entry or not entry["result"]["eligible"]:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
