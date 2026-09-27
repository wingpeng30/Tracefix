"""Repeat strict offline validation from a staged Git bundle inside Linux."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

from tracefix.real_benchmark import load_real_issue_tasks
from tracefix.real_experiment import validate_agent_patch_strict, validate_real_task_behavior
from tracefix.real_recipes import load_environment_recipes


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_input(root: Path, task_id: str) -> dict:
    manifest = json.loads((root / "input-manifest.json").read_text(encoding="utf-8"))
    if manifest["task_id"] != task_id:
        raise ValueError("staged task identity mismatch")
    for name, expected in manifest["files"].items():
        path = (root / name).resolve(strict=True)
        if (
            not path.is_relative_to(root.resolve())
            or not path.is_file()
            or digest(path) != expected
        ):
            raise ValueError(f"staged input changed: {name}")
    return manifest


def clone_source(root: Path, work: Path, commit: str) -> Path:
    source = work / "source"
    for command in (
        ["git", "clone", "--quiet", str(root / "source.bundle"), str(source)],
        ["git", "-C", str(source), "checkout", "--quiet", "--detach", commit],
    ):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise ValueError(f"bundle restore failed: {result.stderr.strip()}")
    return source


def start_local_service(
    url: str,
    test_python: Path,
    *,
    tls: bool = False,
    log_path: Path,
) -> tuple[subprocess.Popen[str], object, dict[str, object]]:
    environment = os.environ.copy()
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment.pop(name, None)
    environment["NO_PROXY"] = "127.0.0.1,localhost,::1"
    if tls:
        command = [
            str(test_python),
            "/opt/tracefix/request-test/service.py",
            "--certificate",
            "/opt/tracefix/request-test/test-ca.pem",
            "--private-key",
            "/opt/tracefix/request-test/test-key.pem",
        ]
    else:
        command = [
            str(test_python),
            "-m",
            "flask",
            "--app",
            "httpbin:app",
            "run",
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--no-reload",
        ]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_stream = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
        env=environment,
        text=True,
    )
    https_healthy = None
    for _ in range(50):
        if process.poll() is not None:
            log_stream.close()
            raise RuntimeError("local httpbin exited before health check")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    if tls:
                        context = ssl.create_default_context(
                            cafile="/opt/tracefix/request-test/test-ca.pem"
                        )
                        secure_url = "https://" + url.removeprefix("http://")
                        with urlopen(secure_url, context=context, timeout=1) as secure_response:
                            https_healthy = secure_response.status == 200
                    if https_healthy is not False:
                        return process, log_stream, {
                            "health_url": url,
                            "http_healthy": True,
                            "https_healthy": https_healthy,
                        }
        except OSError:
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=10)
    log_stream.close()
    raise RuntimeError("local httpbin service health check failed")


def mirror(destination: Path, output: Path) -> dict[str, str]:
    copied: dict[str, str] = {}
    if not destination.is_dir():
        return copied
    for checkout in destination.iterdir():
        evidence = checkout / ".tracefix-validation"
        if evidence.is_dir():
            target = output / "evidence" / destination.name / checkout.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(evidence, target)
            for path in target.rglob("*"):
                if path.is_file():
                    copied[path.relative_to(output).as_posix()] = digest(path)
    return copied


def portable(value: object, output: Path) -> object:
    if isinstance(value, dict):
        return {key: portable(item, output) for key, item in value.items()}
    if isinstance(value, list):
        return [portable(item, output) for item in value]
    if isinstance(value, str) and value.startswith("/work/"):
        parts = Path(value).parts
        if ".tracefix-validation" in parts:
            # /work/stage/checkout/.tracefix-validation/file -> copied evidence.
            index = parts.index(".tracefix-validation")
            return (Path("evidence") / parts[2] / parts[3] / Path(*parts[index + 1 :])).as_posix()
        return f"container:{value}"
    return value


def run(
    root: Path,
    output: Path,
    task_id: str,
    sequence: int,
    test_python: Path,
    agent_patch: Path | None = None,
    stages: tuple[str, ...] = ("qualification", "patch"),
    repeats: tuple[int, ...] = (1, 2),
) -> Path:
    if output.exists():
        raise ValueError("output directory must be new")
    manifest = verify_input(root, task_id)
    task = load_real_issue_tasks(root / "tasks", task_ids=(task_id,))[0]
    recipe = load_environment_recipes(root / "recipes")[task_id]
    if recipe.fingerprint != manifest["recipe_fingerprint"]:
        raise ValueError("staged recipe identity changed")
    protocol = json.loads((root / "protocol.json").read_text(encoding="utf-8"))
    selected_patch = agent_patch or (root / "patch.diff")
    if not selected_patch.is_file():
        raise ValueError("independent acceptance patch is missing")
    expected = tuple(protocol["p1_qualifications"][task_id]["expected_node_ids"])
    work = Path("/work")
    work.mkdir(exist_ok=True)
    source = clone_source(root, work, task.base_commit)
    task.validate_checkout(source)
    output.mkdir(parents=True)
    report = {
        "kind": "container_frozen_patch_reverification",
        "task_id": task_id,
        "sequence": sequence,
        "source_commit": task.base_commit,
        "input_manifest_sha256": digest(root / "input-manifest.json"),
        "recipe_fingerprint": recipe.fingerprint,
        "agent_patch_sha256": digest(selected_patch),
        "agent_patch_source": "external_agent_export" if agent_patch else "staged_saved_patch",
        "python_version": sys.version,
        "test_python": str(test_python),
        "results": [],
    }

    def save() -> None:
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    service = None
    service_log = None
    service_info = None
    try:
        if recipe.service_health_url:
            service, service_log, service_info = start_local_service(
                recipe.service_health_url,
                test_python,
                tls=bool(recipe.test_pythonpath_entries),
                log_path=output / "httpbin-service.log",
            )
            report["service"] = service_info
        save()
        for stage in stages:
            for repeat in repeats:
                destination = work / f"{stage}-{repeat}"
                try:
                    if stage == "qualification":
                        result = validate_real_task_behavior(
                            task,
                            source=source,
                            test_python=test_python,
                            output_dir=destination,
                            recipe=recipe,
                        )
                        qualified = result.eligible_for_llm_prescreen
                    else:
                        result = validate_agent_patch_strict(
                            task,
                            source=source,
                            agent_patch=selected_patch,
                            test_python=test_python,
                            output_dir=destination,
                            recipe=recipe,
                            expected_node_ids=expected,
                            environment_variables=recipe.environment_variables,
                        )
                        qualified = result.eligible
                    entry = {
                        "stage": stage,
                        "repeat": repeat,
                        "qualified": qualified,
                        "result": portable(result.model_dump(mode="json"), output),
                    }
                except Exception as exc:
                    entry = {
                        "stage": stage,
                        "repeat": repeat,
                        "qualified": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                entry["evidence_sha256"] = mirror(destination, output)
                report["results"].append(entry)
                save()
                if not entry["qualified"]:
                    return output / "report.json"
        return output / "report.json"
    finally:
        if service is not None:
            service.terminate()
            service.wait(timeout=10)
            if service_log is not None:
                service_log.close()
            if service_info is not None:
                log_path = output / "httpbin-service.log"
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                service_info["log_path"] = "httpbin-service.log"
                service_info["log_sha256"] = digest(log_path)
                service_info["request_lines"] = [
                    line for line in lines if '"GET ' in line or '"POST ' in line
                ]
                report["service"] = service_info
                save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--test-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--agent-patch", type=Path)
    parser.add_argument("--stage", choices=("qualification", "patch"), action="append")
    parser.add_argument("--repeat", type=int, choices=(1, 2), action="append")
    args = parser.parse_args()
    path = run(
        args.input,
        args.output,
        args.task_id,
        args.sequence,
        args.test_python,
        args.agent_patch,
        tuple(args.stage or ("qualification", "patch")),
        tuple(args.repeat or (1, 2)),
    )
    print(path)
    result = json.loads(path.read_text(encoding="utf-8"))
    expected_count = len(args.stage or ("qualification", "patch")) * len(args.repeat or (1, 2))
    raise SystemExit(
        0
        if len(result["results"]) == expected_count
        and all(item["qualified"] for item in result["results"])
        else 1
    )
