"""Orchestrate fresh-container Sphinx single-node/full-file comparisons."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.docker_reverify import verify_input
from tracefix.docker_backend import _IMAGES

TASK_ID = "sphinx-doc__sphinx-10449"
FILE = "tests/test_ext_autodoc_configs.py"
TARGETS = (
    "tests/test_ext_autodoc_configs.py::test_autodoc_typehints_description_with_documented_init",
    "tests/test_ext_autodoc_configs.py::test_autodoc_default_options",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def docker(args: list[str], *, timeout: int = 1800) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(
            f"docker command failed ({result.returncode}): {args[:5]!r}: "
            f"{result.stderr.decode('utf-8', 'replace')[-2000:]}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--product-patch", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--minimum-free-gib", type=int, default=10)
    args = parser.parse_args()

    root = args.input_root.expanduser().resolve(strict=True)
    patch = args.product_patch.expanduser().resolve(strict=True)
    output = args.output_root.expanduser().resolve()
    if output.exists():
        raise ValueError(f"output directory must be new: {output}")
    output.mkdir(parents=True)
    usage = shutil.disk_usage(output)
    if usage.free < args.minimum_free_gib * 1024**3:
        raise RuntimeError("output/Docker storage drive is below the free-space threshold")
    input_manifest = verify_input(root, TASK_ID)
    image_id, test_python = _IMAGES[TASK_ID]
    docker_root = docker([args.docker, "info", "--format", "{{.DockerRootDir}}"])
    jobs = [(node, repeat, "product") for node in TARGETS for repeat in (1, 2)]
    jobs += [(FILE, repeat, "product") for repeat in (1, 2)]
    jobs += [(TARGETS[1], repeat, variant) for variant in ("base", "gold") for repeat in (1, 2)]
    reports: list[dict[str, object]] = []

    for index, (selector, repeat, variant) in enumerate(jobs, 1):
        label = f"{index:02d}-{variant}-{'file' if selector == FILE else 'node'}-{repeat}"
        target = output / label
        target.mkdir()
        name = f"tracefix-sphinx-probe-{uuid4().hex[:12]}"
        created = False
        copied = False
        try:
            docker(
                [
                    args.docker,
                    "create",
                    "--name",
                    name,
                    "--label",
                    f"tracefix.sphinx-order={output.name}",
                    "--network",
                    "none",
                    "--security-opt",
                    "no-new-privileges",
                    "--cap-drop",
                    "ALL",
                    "--pids-limit",
                    "512",
                    "--memory",
                    "6g",
                    image_id,
                    "sleep",
                    "infinity",
                ]
            )
            created = True
            docker([args.docker, "start", name])
            docker(
                [
                    args.docker,
                    "exec",
                    name,
                    "mkdir",
                    "-p",
                    "/input/task",
                    "/output",
                    "/opt/tracefix",
                ]
            )
            docker([args.docker, "cp", str(root) + "/.", f"{name}:/input/task"])
            docker([args.docker, "cp", str(patch), f"{name}:/input/agent.patch"])
            docker(
                [
                    args.docker,
                    "cp",
                    str(Path(__file__).resolve().parent / "sphinx_order_probe_container.py"),
                    f"{name}:/opt/tracefix/sphinx_order_probe_container.py",
                ]
            )
            docker(
                [
                    args.docker,
                    "cp",
                    str(Path(__file__).resolve().parents[1] / "src"),
                    f"{name}:/opt/tracefix",
                ]
            )
            docker(
                [
                    args.docker,
                    "exec",
                    name,
                    "env",
                    "PYTHONPATH=/opt/tracefix/src",
                    "python",
                    "/opt/tracefix/sphinx_order_probe_container.py",
                    "--input-root",
                    "/input/task",
                    "--product-patch",
                    "/input/agent.patch",
                    "--selector",
                    selector,
                    "--run-label",
                    label,
                    "--variant",
                    variant,
                    "--output-root",
                    f"/output/{label}",
                ],
                timeout=900,
            )
            docker([args.docker, "cp", f"{name}:/output/{label}/.", str(target)])
            identity = docker(
                [args.docker, "inspect", "--format", "{{.Id}} {{.Image}} {{json .Mounts}}", name]
            ).stdout.decode().strip().split(" ", 2)
            report_path = target / "report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report.update(
                {
                    "container_id": identity[0],
                    "actual_image_id": identity[1],
                    "mounts": json.loads(identity[2]),
                    "host_input_manifest_sha256": digest(root / "input-manifest.json"),
                    "host_product_patch_sha256": digest(patch),
                    "docker_root_dir": docker_root.stdout.decode().strip(),
                }
            )
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            copied = True
            reports.append(report)
        finally:
            if created and copied:
                docker([args.docker, "rm", "-f", name])

    summary = {
        "kind": "sphinx_single_node_vs_full_file_order_diagnostic",
        "task_id": TASK_ID,
        "image_id": image_id,
        "test_python": test_python,
        "input_manifest_sha256": digest(root / "input-manifest.json"),
        "product_patch_sha256": digest(patch),
        "original_input_manifest": input_manifest,
        "results": reports,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"runs": len(reports), "output": str(output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
