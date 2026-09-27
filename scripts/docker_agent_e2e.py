"""Zero-cost scripted Agent run and fresh-container strict acceptance replay.

All host interactions use Docker CLI; no host checkout is bind-mounted into a
container. Agent containers see only a Git bundle, a public task statement and
the already-produced product patch. Acceptance containers receive the frozen
test/gold inputs and are always created from the same pinned image ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
DEFAULT_INPUT_ROOT = REPO / "runs" / "docker-foundation-20260926-v1" / "inputs-v2"
IMAGES = {
    "pytest-dev__pytest-10081": (
        "tracefix/reverify-pytest10081:pilot",
        "sha256:2cc93cb74dd96de3179e8320b5b99049fdef0545ddfcad97d388ccaf34fb646a",
        "/usr/local/bin/python",
    ),
    "psf__requests-1766": (
        "tracefix/reverify-requests1766:dual-python",
        "sha256:14b0a27aa9f35af99f4c7b2047f9c24da91ab93dd2b86ef100cea7617ef0c757",
        "/opt/python39/bin/python3.9",
    ),
    "sphinx-doc__sphinx-10449": (
        "tracefix/reverify-sphinx10449:20260926",
        "sha256:f090d53486c3c4bbfc823adc9c52428e66a5f3e94e0010d4a685a0826310cb0e",
        "/usr/local/bin/python",
    ),
}
SMOKE_TESTS = {
    # Existing public tests only; the hidden SWE-bench test patch never enters
    # the Agent container. Acceptance remains responsible for that test patch.
    "pytest-dev__pytest-10081": "testing/test_unittest.py::test_simple_unittest",
    "psf__requests-1766": (
        "test_requests.py::RequestsTestCase::test_HTTP_200_OK_GET_ALTERNATIVE"
    ),
    "sphinx-doc__sphinx-10449": (
        "tests/test_ext_autodoc_configs.py::test_autoclass_content_class"
    ),
}
PRIOR_REPLAYS = {
    "pytest-dev__pytest-10081": (
        "runs/docker-foundation-20260926-v1/replays/pytest-dev__pytest-10081/run-2/report.json"
    ),
    "psf__requests-1766": (
        "runs/docker-foundation-20260926-v1/replays/psf__requests-1766/run-2/report.json"
    ),
    "sphinx-doc__sphinx-10449": (
        "runs/docker-foundation-20260926-v1/replays/sphinx-doc__sphinx-10449/run-1/report.json"
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_image_identity(actual: str, expected: str) -> None:
    """Fail before creating a container if its local image reference drifted."""
    if actual != expected:
        raise RuntimeError(f"image identity mismatch: {actual} != {expected}")


def verify_run_identity(record: dict, expected: dict) -> None:
    """Validate saved run identity before an operator attempts recovery/replay."""
    fields = (
        "task_id",
        "image_id",
        "source_commit",
        "input_manifest_sha256",
        "recipe_fingerprint",
    )
    differences = {
        key: {"recorded": record.get(key), "current": expected.get(key)}
        for key in fields
        if record.get(key) != expected.get(key)
    }
    if differences:
        raise RuntimeError(f"saved run identity mismatch: {differences}")


def acceptance_signature(entry: dict) -> tuple:
    result = entry.get("result", {})
    evidence = result.get("evidence", {})
    environment = result.get("environment_before", {})
    return (
        entry.get("qualified"),
        result.get("eligible"),
        evidence.get("status"),
        evidence.get("returncode"),
        evidence.get("test_count"),
        evidence.get("failure_count"),
        evidence.get("error_count"),
        tuple(evidence.get("expected_node_ids", [])),
        tuple(evidence.get("executed_node_ids", [])),
        environment.get("fingerprint_sha256"),
    )


def run(command: list[str], *, check: bool = True, input_text: str | None = None) -> str:
    result = subprocess.run(command, input=input_text, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", check=False)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command!r}\n{result.stderr}")
    return result.stdout.strip()


class ToolSession:
    def __init__(self, docker: str, container: str, python: str, config: dict) -> None:
        cmd = [
            docker,
            "exec",
            "-i",
            container,
            "env",
            "PYTHONPATH=/opt/tracefix/src",
            "python",
            "/opt/tracefix/agent-bridge.py",
            "--workspace",
            "/work/agent",
            "--evidence",
            "/work/evidence",
            "--python",
            python,
            "--timeout",
            "120",
        ]
        if config.get("pytest_config"):
            cmd.extend(["--pytest-config", config["pytest_config"]])
        for path in config.get("pythonpath", []):
            cmd.extend(["--pythonpath", path])
        if config.get("environment"):
            cmd.extend(["--environment-json", "/input/agent-environment.json"])
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    def call(self, name: str, arguments: dict) -> dict:
        call = {"id": uuid4().hex, "name": name, "arguments": arguments}
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps(call, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            detail = self.process.stderr.read() if self.process.stderr else ""
            raise RuntimeError(
                "container tool bridge exited before replying: "
                f"{detail[-2000:]}"
            )
        return json.loads(line)

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        if self.process.poll() is None:
            self.process.wait(timeout=20)
        if self.process.returncode:
            raise RuntimeError(f"container tool bridge exited {self.process.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", choices=tuple(IMAGES), required=True)
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--docker", default=os.environ.get("DOCKER", "docker"))
    parser.add_argument("--keep-failed-containers", action="store_true")
    args = parser.parse_args()
    output_root = args.output_root or Path(os.environ.get(
        "TRACEFIX_RUNS_ROOT", r"E:\TraceFixRunsActive" if os.name == "nt" else "runs"
    ))
    task_id = args.task_id
    stage = (args.input_root / task_id).resolve(strict=True)
    from scripts.docker_reverify import verify_input

    manifest = verify_input(stage, task_id)
    task = json.loads((stage / "tasks" / task_id / "task.json").read_text(encoding="utf-8"))
    recipe = json.loads((stage / "recipes" / f"{task_id}.json").read_text(encoding="utf-8"))
    image, expected_image_id, test_python = IMAGES[task_id]
    actual_image_id = run([args.docker, "image", "inspect", "--format", "{{.Id}}", image])
    verify_image_identity(actual_image_id, expected_image_id)
    output = output_root.expanduser().resolve() / f"docker-agent-e2e-{task_id}-{uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    run_id = output.name
    report: dict = {
        "kind": "zero_cost_scripted_agent_container_e2e",
        "task_id": task_id,
        "run_id": run_id,
        "source_commit": manifest["source_commit"],
        "input_manifest_sha256": sha256(stage / "input-manifest.json"),
        "recipe_fingerprint": manifest["recipe_fingerprint"],
        "image_ref": image,
        "image_id": actual_image_id,
        "model_requests": 0,
        "agent_container_id": None,
        "acceptance_container_id": None,
        "results": [],
    }

    def save_report() -> None:
        (output / "run.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    save_report()
    agent_id = f"tfx-agent-{uuid4().hex[:12]}"
    accept_id = f"tfx-accept-{uuid4().hex[:12]}"
    failed = True
    try:
        safe = ["--network", "none", "--security-opt", "no-new-privileges",
                "--cap-drop", "ALL", "--pids-limit", "512", "--memory", "6g",
                "--env", "HTTP_PROXY=", "--env", "HTTPS_PROXY=", "--env", "ALL_PROXY=",
                "--env", "http_proxy=", "--env", "https_proxy=", "--env", "all_proxy=",
                "--env", "NO_PROXY=127.0.0.1,localhost,::1"]
        report["agent_container_id"] = run([args.docker, "create", "--name", agent_id,
                                             *safe, actual_image_id, "sleep", "infinity"])
        save_report()
        run([args.docker, "start", agent_id])
        run([args.docker, "exec", agent_id, "mkdir", "-p", "/input", "/work", "/opt/tracefix",
             "/work/evidence"])
        run([args.docker, "cp", str(stage / "source.bundle"), f"{agent_id}:/input/source.bundle"])
        run([args.docker, "cp", str(REPO / "src"), f"{agent_id}:/opt/tracefix"])
        run([args.docker, "cp", str(REPO / "docker" / "agent-bridge.py"),
             f"{agent_id}:/opt/tracefix/agent-bridge.py"])
        environment = recipe.get("environment_variables", {})
        (output / "agent-environment.json").write_text(
            json.dumps(environment, indent=2), encoding="utf-8")
        run([args.docker, "cp", str(output / "agent-environment.json"),
             f"{agent_id}:/input/agent-environment.json"])
        run([args.docker, "exec", agent_id, "git", "clone", "--quiet", "/input/source.bundle",
             "/work/agent"])
        run([args.docker, "exec", agent_id, "git", "-C", "/work/agent", "checkout",
             "--quiet", "--detach", manifest["source_commit"]])
        report["agent_checkout"] = "/work/agent"
        report["agent_workspace_digest"] = run([args.docker, "exec", agent_id, "git", "-C",
                                                  "/work/agent", "rev-parse", "HEAD"])
        for template in recipe.get("build_commands", []):
            command = [test_python if value == "{python}" else value for value in template]
            run([args.docker, "exec", "-w", "/work/agent", agent_id, *command])
        module = recipe.get("source_import_probe")
        if module:
            probe = (
                "import importlib,json; m=importlib.import_module("
                + repr(module)
                + "); print(json.dumps(m.__file__))"
            )
            report["agent_import_probe"] = run(
                [
                    args.docker,
                    "exec",
                    "-w",
                    "/work/agent",
                    agent_id,
                    "env",
                    "PYTHONPATH=/opt/tracefix/src:/work/agent/src:/work/agent",
                    test_python,
                    "-c",
                    probe,
                ]
            )
        service = None
        if recipe.get("service_health_url"):
            service_python = "/opt/python39/bin/python3.9"
            service = run([args.docker, "exec", "-d", agent_id, service_python, "-m", "flask",
                           "--app", "httpbin:app", "run", "--host", "127.0.0.1", "--port",
                           "8765", "--no-reload"])
            healthy = False
            for _ in range(50):
                result = subprocess.run(
                    [
                        args.docker,
                        "exec",
                        agent_id,
                        service_python,
                        "-c",
                        "import urllib.request; "
                        "urllib.request.urlopen('http://127.0.0.1:8765/get', timeout=1)",
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    healthy = True
                    break
                time.sleep(0.1)
            if not healthy:
                raise RuntimeError("Requests local httpbin service did not become healthy")
            report["agent_service"] = {"container_process": service, "healthy": healthy}
        config = {"pytest_config": recipe.get("pytest_config"),
                  "environment": environment}
        session = ToolSession(args.docker, agent_id, test_python, config)
        calls = []
        source_file = (task.get("expected_source_files") or ["README.rst"])[0]
        calls.append(session.call("read_file", {"path": source_file}))
        report["agent_tool_calls"] = calls
        save_report()
        command = f"pytest -q {SMOKE_TESTS[task_id]}"
        baseline_test = session.call("run_tests", {"command": command, "timeout_seconds": 120})
        calls.append(baseline_test)
        report["agent_tool_calls"] = calls
        save_report()
        baseline_diff = session.call("get_git_diff", {})
        if baseline_diff["success"] and isinstance(baseline_diff.get("output"), dict):
            before_product_diff = baseline_diff["output"].get("diff", "")
        else:
            before_product_diff = None
        if before_product_diff != "":
            raise RuntimeError("test-only run produced a non-empty product diff")
        calls.append(baseline_diff)
        patch_text = (stage / "patch.diff").read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        apply_result = session.call("apply_patch", {"patch": patch_text})
        calls.append(apply_result)
        report["agent_tool_calls"] = calls
        save_report()
        if not apply_result["success"]:
            raise RuntimeError(
                f"saved product patch could not be applied: {apply_result.get('error')}"
            )
        after_test = session.call("run_tests", {"command": command, "timeout_seconds": 120})
        calls.append(after_test)
        diff_result = session.call("get_git_diff", {})
        calls.append(diff_result)
        report["agent_tool_calls"] = calls
        save_report()
        session.close()
        evidence_dir = output / "agent-evidence"
        evidence_dir.mkdir()
        run([args.docker, "cp", f"{agent_id}:/work/evidence/.", str(evidence_dir)])
        if not diff_result["success"] or not isinstance(diff_result.get("output"), dict):
            raise RuntimeError("Agent product diff export failed")
        diff = diff_result["output"].get("diff", "")
        if diff_result["output"].get("truncated"):
            raise RuntimeError("Agent product diff exceeded tool output limit")
        patch_path = output / "agent-product-patch.diff"
        patch_path.write_text(diff, encoding="utf-8", newline="\n")
        report["agent_results"] = {
            "read_file_success": calls[0]["success"],
            "test_only_product_diff_empty": before_product_diff == "",
            "test_only_test_status": (baseline_test.get("output") or {}).get("test_status"),
            "apply_patch_success": apply_result["success"],
            "post_patch_test_status": (after_test.get("output") or {}).get("test_status"),
            "post_patch_test_success": after_test["success"],
            "baseline_test_process_started": bool(
                baseline_test.get("metadata", {}).get("process_started")
            ),
            "post_patch_test_process_started": bool(
                after_test.get("metadata", {}).get("process_started")
            ),
            "changed_files": diff_result["output"].get("changed_files", []),
            "patch_sha256": sha256(patch_path),
            "patch_bytes": patch_path.stat().st_size,
            "tool_event_count": len(calls),
        }
        report["agent_patch_empty"] = patch_path.stat().st_size == 0
        report["agent_container_extra_file"] = "agent-only-sentinel.txt"
        run([args.docker, "exec", agent_id, "sh", "-c",
             "printf isolated > /work/agent/agent-only-sentinel.txt"])
        # The acceptance container is new, has the exact same immutable image ID,
        # and is the only side that receives the frozen hidden test/gold materials.
        report["acceptance_container_id"] = run([args.docker, "create", "--name", accept_id,
                                                  *safe, actual_image_id, "sleep", "infinity"])
        run([args.docker, "start", accept_id])
        run([args.docker, "exec", accept_id, "mkdir", "-p", "/input", "/output", "/opt/tracefix"])
        run([args.docker, "cp", str(stage), f"{accept_id}:/input"])
        run([args.docker, "cp", str(REPO / "src"), f"{accept_id}:/opt/tracefix"])
        run([args.docker, "cp", str(REPO / "scripts" / "docker_reverify.py"),
             f"{accept_id}:/opt/tracefix/docker_reverify.py"])
        run([args.docker, "cp", str(patch_path), f"{accept_id}:/output/agent-export.diff"])
        staged_path = f"/input/{stage.name}"
        # docker cp directory to /input creates the task input directory by basename.
        accept_root = staged_path
        output_name = f"/output/replay-{uuid4().hex[:8]}"
        acceptance_python = test_python
        cmd = [args.docker, "exec", accept_id, "env", "PYTHONPATH=/opt/tracefix/src", "python",
               "/opt/tracefix/docker_reverify.py", "--input", accept_root,
               "--output", output_name, "--task-id", task_id, "--sequence", "9001",
               "--test-python", acceptance_python, "--agent-patch", "/output/agent-export.diff"]
        accept_process = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        report["acceptance_exit_code"] = accept_process.returncode
        report["acceptance_stdout"] = accept_process.stdout[-4000:]
        report["acceptance_stderr"] = accept_process.stderr[-4000:]
        if accept_process.returncode not in (0, 1):
            raise RuntimeError(f"independent acceptance launcher failed: {accept_process.stderr}")
        replay_dir = output / "independent-acceptance"
        replay_dir.mkdir()
        run([args.docker, "cp", f"{accept_id}:{output_name}/.", str(replay_dir)], check=False)
        report_path = replay_dir / "report.json"
        if not report_path.is_file():
            # A path-copy fallback handles Docker Desktop CLI versions that reject '/.' syntax.
            run([args.docker, "cp", f"{accept_id}:{output_name}", str(replay_dir)])
            report_path = replay_dir / Path(output_name).name / "report.json"
        acceptance = json.loads(report_path.read_text(encoding="utf-8"))
        report["independent_acceptance"] = acceptance
        isolated = subprocess.run([args.docker, "exec", accept_id, "test", "!", "-e",
                                   "/work/agent/agent-only-sentinel.txt"], capture_output=True)
        report["agent_extra_file_absent_from_acceptance"] = isolated.returncode == 0
        if not report["agent_extra_file_absent_from_acceptance"]:
            raise RuntimeError("Agent-only file leaked into the independent acceptance container")
        old_container_runs = acceptance.get("results", [])
        repeated_patch = [item for item in old_container_runs
                          if item.get("stage") == "patch"]
        report["acceptance_repeats_match"] = (
            len(repeated_patch) == 2
            and all(item.get("qualified") for item in repeated_patch)
            and acceptance_signature(repeated_patch[0])
            == acceptance_signature(repeated_patch[1])
        )
        previous_path = REPO / PRIOR_REPLAYS[task_id]
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        previous_patch = [item for item in previous.get("results", [])
                          if item.get("stage") == "patch"]
        report["prior_replay_path"] = str(previous_path)
        report["acceptance_matches_prior_replay"] = (
            len(previous_patch) == 2
            and len(repeated_patch) == 2
            and all(
                acceptance_signature(current) == acceptance_signature(old)
                for current, old in zip(repeated_patch, previous_patch, strict=True)
            )
        )
        tests_started = (
            report["agent_results"]["baseline_test_process_started"]
            and report["agent_results"]["post_patch_test_process_started"]
        )
        report["status"] = (
            "completed"
            if report["acceptance_repeats_match"]
            and report["acceptance_matches_prior_replay"]
            and tests_started
            else "agent_test_not_started"
            if not tests_started
            else "acceptance_failed"
        )
        failed = False
    except Exception as exc:
        report["status"] = "interrupted_or_failed"
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)
        raise
    finally:
        report["finished_at_epoch"] = time.time()
        (output / "run.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        if not failed or not args.keep_failed_containers:
            for container in (agent_id, accept_id):
                subprocess.run([args.docker, "rm", "-f", container], capture_output=True,
                               text=True, check=False)
    return 0 if report.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
