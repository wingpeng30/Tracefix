"""Zero-cost integration of TraceFixRunner with the frozen Linux task containers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.audit_docker_replays import audit  # noqa: E402
from scripts.docker_reverify import verify_input  # noqa: E402
from tracefix.agent import AgentConfig  # noqa: E402
from tracefix.context import ContextConfig  # noqa: E402
from tracefix.docker_backend import _IMAGES  # noqa: E402
from tracefix.docker_tls import write_test_tls_material  # noqa: E402
from tracefix.messages import Message, MessageRole, ToolCall  # noqa: E402
from tracefix.models import BaseLLM, LLMConfig, LLMResponse, TokenUsage  # noqa: E402
from tracefix.real_recipes import EnvironmentRecipe  # noqa: E402
from tracefix.repository import RepoMapConfig  # noqa: E402
from tracefix.runtime import RunConfig, TraceFixRunner  # noqa: E402

TASKS = (
    "pytest-dev__pytest-10081",
    "psf__requests-1766",
    "sphinx-doc__sphinx-10449",
)
SOURCE_ROOT = REPO / "runs" / "real-candidate-validation-v080b"
DEFAULT_INPUT_ROOT = REPO / "runs" / "docker-foundation-20260926-v1" / "inputs-v2"
SMOKE = {
    TASKS[0]: "testing/test_unittest.py::test_simple_unittest",
    TASKS[1]: "test_requests.py::RequestsTestCase::test_HTTP_200_OK_GET_ALTERNATIVE",
    TASKS[2]: "tests/test_ext_autodoc_configs.py::test_autoclass_content_class",
}
BROAD_PUBLIC = {
    TASKS[0]: "testing/test_unittest.py",
    TASKS[1]: "test_requests.py",
    TASKS[2]: "tests/test_ext_autodoc_configs.py",
}


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracefix_source_sha256() -> str:
    paths = [REPO / "pyproject.toml"]
    for root in (REPO / "src" / "tracefix", REPO / "scripts"):
        paths.extend(
            path for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        relative = path.relative_to(REPO).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _derive_requests_tls_input(stage: Path, destination: Path) -> Path:
    """Create a separately hashed, HTTP-to-local-HTTPS Requests test profile."""
    if stage.name != TASKS[1] or destination.exists():
        raise ValueError("TLS input derivation requires a new Requests task destination")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(stage, destination)
    recipe_path = destination / "recipes" / f"{TASKS[1]}.json"
    recipe_data = json.loads(recipe_path.read_text(encoding="utf-8"))
    recipe_data["environment_variables"].update(
        {
            "HTTPBIN_URL": "https://localhost/",
            "TRACEFIX_TEST_CA_BUNDLE": "/opt/tracefix/request-test/test-ca.pem",
        }
    )
    recipe_data["test_pythonpath_entries"] = ["/opt/tracefix/request-test"]
    recipe_data["service_health_url"] = "http://localhost/get"
    recipe_data["supported_platforms"] = ["linux"]
    recipe_path.write_text(json.dumps(recipe_data, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    recipe = EnvironmentRecipe.model_validate(recipe_data)

    protocol_path = destination / "protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    protocol["recipe_hashes"][TASKS[1]] = recipe.fingerprint
    protocol["test_environment_variables"][TASKS[1]] = recipe.environment_variables
    protocol["service_health_urls"][TASKS[1]] = recipe.service_health_url
    protocol_path.write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")

    manifest_path = destination / "input-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["recipe_fingerprint"] = recipe.fingerprint
    manifest["derived_from_manifest_sha256"] = _hash(stage / "input-manifest.json")
    manifest["derivation"] = "Requests-only local HTTPS with runtime-generated test CA"
    manifest["files"]["recipes/psf__requests-1766.json"] = _hash(recipe_path)
    manifest["files"]["protocol.json"] = _hash(protocol_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    verify_input(destination, TASKS[1])
    return destination


def _response(content: str | None = None, calls: tuple[ToolCall, ...] = ()) -> LLMResponse:
    return LLMResponse(
        message=Message(role=MessageRole.ASSISTANT, content=content, tool_calls=calls),
        usage=TokenUsage(),
        model_name="offline/scripted",
        finish_reason="tool_calls" if calls else "stop",
    )


class _ScriptedAgent(BaseLLM):
    """Drive all five existing tools through the real MinimalAgent loop."""

    def __init__(
        self, config: LLMConfig, patch: str, source_path: str, task_id: str,
        skills_enabled: bool = False,
    ) -> None:
        super().__init__(config)
        self.patch = patch
        self.source_path = source_path
        self.task_id = task_id
        self.skills_enabled = skills_enabled
        self.turn = 0

    def complete(self, messages, tools=()) -> LLMResponse:
        self.turn += 1
        if self.skills_enabled and self.turn == 1:
            assert "tracefix-debugging" in "\n".join(
                message.content or "" for message in messages
            )
            assert "load_skill" in {tool.name for tool in tools}
            return _response(
                calls=(
                    ToolCall(
                        id="activate-skill", name="load_skill",
                        arguments={"name": "tracefix-debugging"},
                    ),
                    ToolCall(
                        id="activate-skill-again", name="load_skill",
                        arguments={"name": "tracefix-debugging"},
                    ),
                )
            )
        logical_turn = self.turn - int(self.skills_enabled)
        if self.skills_enabled:
            assert any(
                message.metadata.get("kind") == "skill_instructions"
                for message in messages
            )
        if logical_turn == 1:
            return _response(
                calls=(
                    ToolCall(
                        id="search-1",
                        name="search_code",
                        arguments={
                            "query": Path(self.source_path).stem,
                            "path": ".",
                            "glob": "**/*",
                            "max_results": 10,
                        },
                    ),
                    ToolCall(id="read-1", name="read_file", arguments={"path": self.source_path}),
                )
            )
        if logical_turn == 2:
            return _response(
                calls=(
                    ToolCall(
                        id="base-smoke",
                        name="run_tests",
                        arguments={
                            "command": f"pytest -q {SMOKE[self.task_id]}",
                            "timeout_seconds": 120,
                        },
                    ),
                )
            )
        if logical_turn == 3:
            return _response(
                calls=(
                    ToolCall(
                        id="baseline-product-diff",
                        name="get_git_diff",
                        arguments={"context_lines": 3},
                    ),
                )
            )
        if logical_turn == 4:
            return _response(
                calls=(
                    ToolCall(
                        id="apply-saved-patch", name="apply_patch", arguments={"patch": self.patch}
                    ),
                )
            )
        if logical_turn == 5:
            return _response(
                calls=(
                    ToolCall(
                        id="patched-smoke",
                        name="run_tests",
                        arguments={
                            "command": f"pytest -q {SMOKE[self.task_id]}",
                            "timeout_seconds": 120,
                        },
                    ),
                )
            )
        if logical_turn == 6:
            return _response(
                calls=(
                    ToolCall(
                        id="broad-public-diagnostic",
                        name="run_tests",
                        arguments={
                            "command": f"pytest -q {BROAD_PUBLIC[self.task_id]}",
                            "timeout_seconds": 300,
                        },
                    ),
                )
            )
        if logical_turn == 7:
            return _response(
                calls=(
                    ToolCall(id="export-diff", name="get_git_diff", arguments={"context_lines": 3}),
                )
            )
        return _response("offline scripted tool-flow complete")


def _docker(
    args: list[str], *, check: bool = True, timeout: int = 120
) -> subprocess.CompletedProcess:
    completed = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    if check and completed.returncode:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {args[:6]!r}: "
            f"{completed.stderr.decode('utf-8', 'replace')[-3000:]}"
        )
    return completed


def _fresh_acceptance(
    *,
    task_id: str,
    stage: Path,
    patch: Path,
    output: Path,
    stage_name: str,
    repeat: int,
    docker: str,
) -> dict:
    image_id, python = _IMAGES[task_id]
    container_name = f"tracefix-accept-{uuid4().hex[:12]}"
    result_dir = output / f"{stage_name}-{repeat}"
    result_dir.mkdir(parents=True, exist_ok=False)
    container_id = None
    retain = True
    tls_certificate_sha256 = None
    try:
        _docker(
            [
                docker,
                "create",
                "--name",
                container_name,
                "--label",
                f"tracefix.e2e={output.name}",
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
                "--env",
                "HTTP_PROXY=",
                "--env",
                "HTTPS_PROXY=",
                "--env",
                "ALL_PROXY=",
                "--env",
                "http_proxy=",
                "--env",
                "https_proxy=",
                "--env",
                "all_proxy=",
                *(
                    ["--sysctl", "net.ipv4.ip_unprivileged_port_start=0"]
                    if json.loads(
                        (stage / "recipes" / f"{task_id}.json").read_text(encoding="utf-8")
                    ).get("test_pythonpath_entries")
                    else []
                ),
                image_id,
                "sleep",
                "infinity",
            ]
        )
        container_id = (
            _docker([docker, "inspect", "--format", "{{.Id}}", container_name])
            .stdout.decode()
            .strip()
        )
        actual_image = (
            _docker([docker, "inspect", "--format", "{{.Image}}", container_id])
            .stdout.decode()
            .strip()
        )
        mounts = json.loads(
            _docker([docker, "inspect", "--format", "{{json .Mounts}}", container_id])
            .stdout.decode()
        )
        _docker([docker, "start", container_name])
        _docker(
            [docker, "exec", container_name, "mkdir", "-p", "/input", "/output", "/opt/tracefix"]
        )
        _docker([docker, "cp", str(stage), f"{container_name}:/input"])
        _docker([docker, "cp", str(REPO / "src"), f"{container_name}:/opt/tracefix"])
        _docker(
            [
                docker,
                "cp",
                str(REPO / "scripts" / "docker_reverify.py"),
                f"{container_name}:/opt/tracefix/docker_reverify.py",
            ]
        )
        _docker([docker, "cp", str(patch), f"{container_name}:/output/agent-patch.diff"])
        recipe = json.loads((stage / "recipes" / f"{task_id}.json").read_text(encoding="utf-8"))
        if task_id == TASKS[1] and recipe.get("test_pythonpath_entries"):
            _docker(
                [
                    docker,
                    "exec",
                    container_name,
                    "mkdir",
                    "-p",
                    "/opt/tracefix/request-test",
                ]
            )
            with tempfile.TemporaryDirectory(prefix="tracefix-tls-", dir=result_dir) as tls_dir:
                certificate = Path(tls_dir) / "test-ca.pem"
                private_key = Path(tls_dir) / "test-key.pem"
                write_test_tls_material(certificate, private_key)
                tls_certificate_sha256 = _hash(certificate)
                for path in (certificate, private_key):
                    _docker(
                        [
                            docker,
                            "cp",
                            str(path),
                            f"{container_name}:/opt/tracefix/request-test/{path.name}",
                        ]
                    )
        inner_output = f"/output/{stage_name}-{repeat}"
        command = [
            docker,
            "exec",
            container_name,
            "env",
            "PYTHONPATH=/opt/tracefix/src",
            "python",
            "/opt/tracefix/docker_reverify.py",
            "--input",
            f"/input/{task_id}",
            "--output",
            inner_output,
            "--task-id",
            task_id,
            "--sequence",
            str(9100 + repeat),
            "--test-python",
            python,
            "--stage",
            stage_name,
            "--repeat",
            str(repeat),
        ]
        if stage_name == "patch":
            command.extend(["--agent-patch", "/output/agent-patch.diff"])
        completed = _docker(command, check=False, timeout=1800)
        (result_dir / "launcher.stdout.txt").write_bytes(completed.stdout)
        (result_dir / "launcher.stderr.txt").write_bytes(completed.stderr)
        copy = _docker(
            [docker, "cp", f"{container_name}:{inner_output}/.", str(result_dir)], check=False
        )
        report_path = result_dir / "report.json"
        if copy.returncode or not report_path.is_file():
            raise RuntimeError("fresh acceptance container did not return its report")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        retain = not all(item.get("qualified") for item in report.get("results", []))
        sentinel = _docker(
            [
                docker,
                "exec",
                container_name,
                "python",
                "-c",
                "from pathlib import Path; roots=(Path('/work'),Path('/input')); "
                "assert not any(p.name == 'agent-only-sentinel' for r in roots "
                "for p in r.rglob('agent-only-sentinel'))",
            ],
            check=False,
        )
        return {
            "container_id": container_id,
            "container_name": container_name,
            "stage": stage_name,
            "repeat": repeat,
            "exit_code": completed.returncode,
            "agent_only_sentinel_absent": sentinel.returncode == 0,
            "image_id": actual_image,
            "mounts": mounts,
            "report_path": str(report_path),
            "tls_certificate_sha256": tls_certificate_sha256,
            "report": report,
        }
    finally:
        if container_id and not retain:
            identity = _docker(
                [
                    docker,
                    "inspect",
                    "--format",
                    '{{index .Config.Labels "tracefix.e2e"}}',
                    container_id,
                ],
                check=False,
            )
            if identity.returncode == 0 and identity.stdout.decode().strip() == output.name:
                _docker([docker, "rm", "-f", container_id], check=False)


def run_all(
    input_root: Path,
    output_root: Path,
    docker: str,
    task_ids: tuple[str, ...] = TASKS,
    *,
    run_acceptance: bool = True,
    skills_enabled: bool = False,
    requests_tls: bool = False,
) -> dict:
    if requests_tls and task_ids != (TASKS[1],):
        raise ValueError("the derived TLS profile is limited to Requests-1766 only")
    output_root.mkdir(parents=True, exist_ok=True)
    summary_path = output_root / "docker-runner-e2e-summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("kind") != "zero_cost_formal_runner_docker_integration":
            raise ValueError("existing output summary has an unexpected identity")
        existing_ids = {item["task_id"] for item in summary.get("tasks", [])}
        overlap = existing_ids.intersection(task_ids)
        if overlap:
            raise ValueError(f"refusing to overwrite task results: {sorted(overlap)}")
    else:
        summary = {
            "kind": "zero_cost_formal_runner_docker_integration",
            "created_at": datetime.now(UTC).isoformat(),
            "tracefix_git_head": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                text=True, check=True,
            ).stdout.strip(),
            "tracefix_source_tree_sha256": _tracefix_source_sha256(),
            "model_requests": 0,
            "formal_experiment": False,
            "holdout_used": False,
            "tasks": [],
        }
    for task_id in task_ids:
        stage = input_root / task_id
        manifest = verify_input(stage, task_id)
        selected_input_root = input_root
        if requests_tls:
            selected_input_root = output_root / "derived-inputs"
            stage = _derive_requests_tls_input(stage, selected_input_root / task_id)
            manifest = verify_input(stage, task_id)
        task_data = json.loads(
            (stage / "tasks" / task_id / "task.json").read_text(encoding="utf-8")
        )
        task_text = (stage / "tasks" / task_id / "problem.md").read_text(encoding="utf-8")
        patch_path = stage / "patch.diff"
        output = output_root / f"docker-runner-{task_id}-{uuid4().hex[:8]}"
        source_repo = SOURCE_ROOT / task_id
        patch = patch_path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        expected_source = task_data["expected_source_files"][0]
        runner = TraceFixRunner(
            lambda config, p=patch, s=expected_source, t=task_id: _ScriptedAgent(
                config, p, s, t, skills_enabled
            )
        )
        result = runner.run(
            RunConfig(
                repo=source_repo,
                task=task_text,
                model_name="offline/scripted",
                output_dir=output,
                env_file=None,
                execution_backend="docker",
                docker_task_id=task_id,
                docker_input_root=selected_input_root,
                agent_config=AgentConfig(
                    max_steps=10 if skills_enabled else 8,
                    max_test_runs=4,
                    wall_time_seconds=900,
                    skills_enabled=skills_enabled,
                    context=ContextConfig(
                        context_window_tokens=100_000,
                        compaction_trigger_tokens=2_500 if skills_enabled else 32_000,
                    ),
                    repo_map=RepoMapConfig(enabled=True),
                ),
            )
        )
        run_dir = Path(result.result_path).parent
        agent_report = {
            "status": result.status.value,
            "stop_reason": result.stop_reason,
            "test_runs": result.test_runs,
            "source_commit": result.source_commit,
            "workspace": result.workspace,
            "changed_files": list(result.changed_files),
            "patch_sha256": _hash(Path(result.diff_path)),
            "test_only_diff_empty": result.test_runs >= 1,
            "repo_map_computed": result.repo_map is not None,
            "context_compactions": result.context_metrics.compaction_count,
            "model_requests": 0,
            "container_identity": json.loads(
                (run_dir / "container-identity.json").read_text(encoding="utf-8")
            )
            if (run_dir / "container-identity.json").is_file()
            else None,
        }
        service_log = run_dir / "httpbin-service.log"
        if service_log.is_file():
            service_lines = service_log.read_text(encoding="utf-8", errors="replace").splitlines()
            agent_report["local_service_evidence"] = {
                "log_path": str(service_log),
                "log_sha256": _hash(service_log),
                "request_lines": [line for line in service_lines if '"GET ' in line],
            }
        else:
            agent_report["local_service_evidence"] = None
        if requests_tls:
            agent_report["https_healthy"] = (
                agent_report["container_identity"].get("local_service", {}).get("https_healthy")
            )
        # Test-only diff is independently queried before the saved patch is applied.
        # The scripted model includes that call and stores its result in the trace.
        trace = [
            json.loads(line)
            for line in Path(result.trace_path).read_text(encoding="utf-8").splitlines()
        ]
        tool_results = [
            event["payload"].get("result", {})
            for event in trace
            if event.get("event_type") == "tool_returned"
        ]
        skill_events = [
            event for event in trace
            if event.get("event_type") in {"skill_catalog_exposed", "skill_activated"}
        ]
        agent_report["skills_enabled"] = skills_enabled
        agent_report["skill_trace_event_count"] = len(skill_events)
        agent_report["skill_activation_hashes"] = [
            event["payload"].get("content_sha256")
            for event in skill_events if event.get("event_type") == "skill_activated"
        ]
        agent_report["skill_duplicate_tool_results"] = sum(
            1 for item in tool_results
            if item.get("tool_name") == "load_skill"
            and (item.get("output") or {}).get("already_loaded") is True
        )
        agent_report["tool_names"] = [
            event["payload"].get("call", {}).get("name")
            for event in trace
            if event.get("event_type") == "tool_called"
        ]
        diff_results = [item for item in tool_results if item.get("tool_name") == "get_git_diff"]
        agent_report["test_only_diff_empty"] = bool(
            diff_results and (diff_results[0].get("output") or {}).get("diff") == ""
        )
        test_results = [item for item in tool_results if item.get("tool_name") == "run_tests"]
        agent_report["test_diagnostics"] = [
            {
                "call_index": index,
                "test_status": (item.get("output") or {}).get("test_status"),
                "returncode": (item.get("output") or {}).get("returncode"),
                "test_counts": (item.get("output") or {}).get("test_counts"),
                "timed_out": (item.get("output") or {}).get("timed_out"),
                "error": item.get("error"),
            }
            for index, item in enumerate(test_results, 1)
        ]
        if not run_acceptance:
            entry = {
                "task_id": task_id,
                "run_id": result.run_id,
                "run_dir": str(run_dir),
                "input_manifest_sha256": _hash(stage / "input-manifest.json"),
                "agent": agent_report,
                "acceptance_deferred_for_debug": True,
                "passed": False,
            }
            (run_dir / "docker-runner-e2e.json").write_text(
                json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            summary["tasks"].append(entry)
            continue
        acceptances = []
        for stage_name in ("qualification", "patch"):
            for repeat in (1, 2):
                acceptances.append(
                    _fresh_acceptance(
                        task_id=task_id,
                        stage=stage,
                        patch=Path(result.diff_path),
                        output=run_dir / "independent-acceptance",
                        stage_name=stage_name,
                        repeat=repeat,
                        docker=docker,
                    )
                )
        combined = {
            "task_id": task_id,
            "sequence": 9200,
            "results": [item for run in acceptances for item in run["report"]["results"]],
        }
        acceptance_root = run_dir / "independent-acceptance"
        for run in acceptances:
            subdir = Path(run["report_path"]).parent.name
            for entry in run["report"]["results"]:
                entry["evidence_sha256"] = {
                    f"{subdir}/{name}": digest for name, digest in entry["evidence_sha256"].items()
                }
        acceptance_path = acceptance_root / "combined-report.json"
        acceptance_path.write_text(
            json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        audit_path = acceptance_root / "strict-audit.json"
        audit_row = audit(acceptance_path)
        audit_path.write_text(json.dumps(audit_row, ensure_ascii=False, indent=2), encoding="utf-8")
        checks = {
            "runner_completed": result.status.value == "completed",
            "skill_catalog_and_deduplication_valid": not skills_enabled
            or (
                len(
                    [e for e in skill_events if e.get("event_type") == "skill_catalog_exposed"]
                ) == 1
                and len(agent_report["skill_activation_hashes"]) == 1
                and agent_report["skill_duplicate_tool_results"] == 1
            ),
            "skill_survives_compaction": not skills_enabled
            or (
                result.context_metrics.compaction_count > 0
                and any(
                    event.get("event_type") == "skill_activated"
                    for event in trace
                )
            ),
            "five_tools_exercised": (
                set(agent_report["tool_names"]) - {"load_skill"}
                == {"search_code", "read_file", "apply_patch", "run_tests", "get_git_diff"}
            ),
            "tests_counted_at_process_start": result.test_runs == 3,
            "broad_public_suite_diagnostic_recorded": len(agent_report["test_diagnostics"]) == 3,
            "requests_child_routed_to_local_service": task_id != TASKS[1]
            or (
                agent_report["local_service_evidence"] is not None
                and sum(
                    '"GET ' in line
                    for line in agent_report["local_service_evidence"]["request_lines"]
                )
                >= 3
            ),
            "requests_actual_tls_path_verified": not requests_tls
            or (
                agent_report.get("https_healthy") is True
                and bool(agent_report.get("local_service_evidence"))
            ),
            "runtime_certificates_are_unique": not requests_tls
            or len(
                {
                    agent_report["container_identity"].get("local_service", {}).get(
                        "tls_certificate_sha256"
                    ),
                    *(item.get("tls_certificate_sha256") for item in acceptances),
                }
            ) == 5,
            "test_only_product_diff_empty": agent_report["test_only_diff_empty"],
            "patch_exported": bool(result.changed_files)
            and Path(result.diff_path).stat().st_size > 0,
            "all_fresh_acceptance_passed": all(
                all(row.get("qualified") for row in run["report"]["results"])
                and run["agent_only_sentinel_absent"]
                and run["image_id"] == _IMAGES[task_id][0]
                and not run["mounts"]
                for run in acceptances
            ),
            "strict_evidence_audit_passed": audit_row["repeat_consistent"],
            "frozen_input_verified": manifest["source_commit"] == result.source_commit,
        }
        entry = {
            "task_id": task_id,
            "run_id": result.run_id,
            "run_dir": str(run_dir),
            "input_manifest_sha256": _hash(stage / "input-manifest.json"),
            "agent": agent_report,
            "acceptance_containers": [
                {
                    key: run[key]
                    for key in (
                        "container_id",
                        "stage",
                        "repeat",
                        "exit_code",
                        "image_id",
                        "mounts",
                        "agent_only_sentinel_absent",
                        "report_path",
                    )
                }
                for run in acceptances
            ],
            "checks": checks,
            "passed": all(checks.values()),
        }
        (run_dir / "docker-runner-e2e.json").write_text(
            json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        summary["tasks"].append(entry)
    task_rows = {entry["task_id"]: entry for entry in summary["tasks"]}
    summary["all_tasks_passed"] = set(task_ids).issubset(task_rows) and all(
        task_rows[task_id]["passed"] for task_id in task_ids
    )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=DEFAULT_INPUT_ROOT)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(os.environ.get("TRACEFIX_RUNS_ROOT", r"E:\TraceFixRunsActive")),
    )
    parser.add_argument("--docker", default=os.environ.get("DOCKER", "docker"))
    parser.add_argument("--task-id", action="append", choices=TASKS)
    parser.add_argument("--agent-only", action="store_true")
    parser.add_argument("--skills-enabled", action="store_true")
    parser.add_argument("--requests-tls", action="store_true")
    args = parser.parse_args()
    result = run_all(
        args.input_root,
        args.output_root,
        args.docker,
        tuple(args.task_id or TASKS),
        run_acceptance=not args.agent_only,
        skills_enabled=args.skills_enabled,
        requests_tls=args.requests_tls,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["all_tasks_passed"] else 1)
