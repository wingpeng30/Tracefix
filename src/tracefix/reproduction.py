"""Run TraceFixRunner end to end on a synthetic task without a provider client."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch as mock_patch

from tracefix import (
    AgentConfig,
    AgentStatus,
    BaseLLM,
    LLMConfig,
    LLMResponse,
    Message,
    MessageRole,
    RunConfig,
    TokenUsage,
    ToolCall,
    TraceFixRunner,
)
from tracefix.report import render_report

PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""

PAGINATION_PATCH = """diff --git a/catalog/pagination.py b/catalog/pagination.py
--- a/catalog/pagination.py
+++ b/catalog/pagination.py
@@ -1,3 +1,3 @@
 def page_items(items: list[str], page: int, page_size: int) -> list[str]:
-    start = page * page_size
+    start = (page - 1) * page_size
     return items[start : start + page_size]
"""


def _source_sha256() -> str:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(
        item for item in root.rglob("*") if item.is_file() and "__pycache__" not in item.parts
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _response(content: str | None = None, *calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        message=Message(role=MessageRole.ASSISTANT, content=content, tool_calls=calls),
        usage=TokenUsage(),
        model_name="offline/scripted",
        finish_reason="tool_calls" if calls else "stop",
    )


class _ScriptedLLM(BaseLLM):
    """A fixed fixture that cannot create a provider client or access a network."""

    def __init__(self, config: LLMConfig, skills_enabled: bool, scenario: str = "smoke") -> None:
        super().__init__(config)
        self.skills_enabled = skills_enabled
        self.requests = 0
        self.turn = 0
        self.scenario = scenario

    def complete(self, messages, tools=()) -> LLMResponse:
        self.requests += 1
        self.turn += 1
        tool_names = {tool.name for tool in tools}
        if self.skills_enabled and self.turn == 1:
            if "load_skill" not in tool_names or "tracefix-debugging" not in (
                messages[1].content or ""
            ):
                raise AssertionError("enabled skill catalog was not provided to the fixture")
            return _response(
                None,
                ToolCall(id="skill-1", name="load_skill", arguments={"name": "tracefix-debugging"}),
            )
        if self.skills_enabled and not any(
            item.metadata.get("kind") == "skill_instructions" for item in messages
        ):
            raise AssertionError("loaded skill instructions did not enter model context")
        offset = int(self.skills_enabled)
        calls = {
            1 + offset: ToolCall(
                id="search-1",
                name="search_code",
                arguments={"query": "VALUE", "path": ".", "max_results": 10},
            ),
            2 + offset: ToolCall(id="read-1", name="read_file", arguments={"path": "app.py"}),
            3 + offset: ToolCall(id="patch-1", name="apply_patch", arguments={"patch": PATCH}),
            4 + offset: ToolCall(id="test-1", name="run_tests", arguments={"command": "pytest -q"}),
            5 + offset: ToolCall(id="diff-1", name="get_git_diff", arguments={"context_lines": 3}),
        }
        if self.scenario == "pagination":
            calls = {
                1 + offset: ToolCall(
                    id="before-1", name="run_tests", arguments={"command": "pytest -q"}
                ),
                2 + offset: ToolCall(
                    id="search-1",
                    name="search_code",
                    arguments={"query": "page_items", "path": ".", "max_results": 10},
                ),
                3 + offset: ToolCall(
                    id="api-1", name="read_file", arguments={"path": "catalog/api.py"}
                ),
                4 + offset: ToolCall(
                    id="impl-1", name="read_file", arguments={"path": "catalog/pagination.py"}
                ),
                5 + offset: ToolCall(
                    id="patch-1", name="apply_patch", arguments={"patch": PAGINATION_PATCH}
                ),
                6 + offset: ToolCall(
                    id="after-1", name="run_tests", arguments={"command": "pytest -q"}
                ),
                7 + offset: ToolCall(
                    id="diff-1", name="get_git_diff", arguments={"context_lines": 3}
                ),
            }
        call = calls.get(self.turn)
        if call is None:
            return _response(
                "脚本演示完成；请查看测试证据和补丁。"
                if self.scenario == "pagination"
                else "synthetic repair complete"
            )
        return _response(None, call)


def _git(root: Path, *arguments: str) -> None:
    env = os.environ.copy()
    if arguments[:1] == ("commit",):
        env["GIT_AUTHOR_DATE"] = "2000-01-01T00:00:00+00:00"
        env["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00+00:00"
    subprocess.run(["git", *arguments], cwd=root, env=env, check=True, capture_output=True)


def _make_source(root: Path, scenario: str = "smoke") -> Path:
    repo = root / "source"
    repo.mkdir(parents=True)
    (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    if scenario == "smoke":
        (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (tests / "test_value.py").write_text(
            "from app import VALUE\n\ndef test_value_is_fixed():\n    assert VALUE == 2\n",
            encoding="utf-8",
        )
    elif scenario == "pagination":
        catalog = repo / "catalog"
        catalog.mkdir()
        (catalog / "__init__.py").write_text("", encoding="utf-8")
        (catalog / "api.py").write_text(
            "from catalog.pagination import page_items\n\n"
            "def list_products(items: list[str], page: int, page_size: int) -> list[str]:\n"
            "    return page_items(items, page, page_size)\n",
            encoding="utf-8",
        )
        (catalog / "pagination.py").write_text(
            "def page_items(items: list[str], page: int, page_size: int) -> list[str]:\n"
            "    start = page * page_size\n"
            "    return items[start : start + page_size]\n",
            encoding="utf-8",
        )
        (tests / "test_pagination.py").write_text(
            "from catalog.api import list_products\n\n"
            "def test_first_page():\n"
            "    assert list_products(['A', 'B', 'C', 'D'], 1, 2) == ['A', 'B']\n\n"
            "def test_second_page():\n"
            "    assert list_products(['A', 'B', 'C', 'D'], 2, 2) == ['C', 'D']\n\n"
            "def test_empty_list():\n"
            "    assert list_products([], 1, 2) == []\n",
            encoding="utf-8",
        )
    else:
        raise ValueError(f"unsupported reproduction scenario: {scenario}")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "TraceFix smoke")
    _git(repo, "config", "user.email", "tracefix-smoke@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic reproduction fixture")
    _git(repo, "tag", "tracefix-synthetic-v1")
    return repo


def _make_docker_inputs(root: Path, source: Path) -> Path:
    task_id = "tracefix-synthetic"
    stage = root / task_id
    (stage / "recipes").mkdir(parents=True)
    bundle = stage / "source.bundle"
    subprocess.run(
        ["git", "bundle", "create", str(bundle), "--all"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    recipe = {"task_id": task_id, "environment_variables": {}}
    (stage / "recipes" / f"{task_id}.json").write_text(
        json.dumps(recipe, sort_keys=True), encoding="utf-8"
    )
    manifest = {
        "task_id": task_id,
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip(),
        "files": {"source.bundle": hashlib.sha256(bundle.read_bytes()).hexdigest()},
    }
    (stage / "input-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return root


@contextmanager
def _forbid_provider_access(
    provider_request_attempts: list[str], network_connect_attempts: list[str]
) -> Iterator[Any]:
    """Install the smoke's fail-fast guards and expose the constructor spy."""

    def forbidden_provider_call(*_args, **_kwargs):
        provider_request_attempts.append("attempt")
        raise AssertionError("provider requests are forbidden in smoke runs")

    def forbidden_network_call(*_args, **_kwargs):
        network_connect_attempts.append("attempt")
        raise AssertionError("network access is forbidden in smoke runs")

    with ExitStack() as guards:
        provider_constructor = guards.enter_context(
            mock_patch(
                "tracefix.runtime.LiteLLMAdapter",
                side_effect=AssertionError(
                    "provider client construction is forbidden in smoke runs"
                ),
            )
        )
        guards.enter_context(
            mock_patch(
                "tracefix.models.litellm_adapter.LiteLLMAdapter.complete",
                side_effect=forbidden_provider_call,
            )
        )
        # The parent Runner has no network requirement: local tests run in
        # child processes, and Docker is controlled through the Docker CLI.
        guards.enter_context(
            mock_patch("socket.create_connection", side_effect=forbidden_network_call)
        )
        guards.enter_context(
            mock_patch("socket.socket.connect", side_effect=forbidden_network_call)
        )
        yield provider_constructor


def run_smoke(
    output: Path,
    *,
    skills_enabled: bool = False,
    backend: str = "local",
    image_id: str | None = None,
    scenario: str = "smoke",
) -> dict[str, object]:
    """Run a real TraceFixRunner clone, tool, pytest, patch and trace cycle."""
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="tracefix-repro-") as temporary:
        temporary_root = Path(temporary)
        source = _make_source(temporary_root, scenario)
        docker_inputs = (
            _make_docker_inputs(temporary_root / "inputs", source) if backend == "docker" else None
        )
        llm_instances: list[_ScriptedLLM] = []

        def factory(config: LLMConfig) -> BaseLLM:
            model = _ScriptedLLM(config, skills_enabled, scenario)
            llm_instances.append(model)
            return model

        provider_request_attempts: list[str] = []
        network_connect_attempts: list[str] = []

        with _forbid_provider_access(provider_request_attempts, network_connect_attempts) as (
            provider_constructor
        ):
            result = TraceFixRunner(factory).run(
                RunConfig(
                    repo=source,
                    task=(
                        "Fix pagination: page one skips products in catalog.api."
                        if scenario == "pagination"
                        else "Fix the synthetic app.VALUE test failure."
                    ),
                    model_name="offline/scripted",
                    output_dir=output / "runs",
                    env_file=None,
                    agent_config=AgentConfig(
                        skills_enabled=skills_enabled,
                        max_steps=11 if scenario == "pagination" else 8,
                        max_test_runs=2,
                        wall_time_seconds=120,
                    ),
                    execution_backend="docker" if backend == "docker" else "local",
                    docker_task_id="tracefix-synthetic" if backend == "docker" else None,
                    docker_input_root=docker_inputs,
                    docker_profile="synthetic" if backend == "docker" else "frozen",
                    docker_image_id=image_id,
                )
            )
        if provider_constructor.call_count:
            raise AssertionError("TraceFix constructed a provider client during the smoke run")
        if provider_request_attempts or network_connect_attempts:
            raise AssertionError("a provider or network request was attempted during the smoke run")
    if len(llm_instances) != 1:
        raise AssertionError("unexpected scripted model construction count")
    if (
        result.status is not AgentStatus.COMPLETED
        or result.changed_files
        != (("catalog/pagination.py",) if scenario == "pagination" else ("app.py",))
        or result.test_runs != (2 if scenario == "pagination" else 1)
        or llm_instances[0].requests != result.step_count
    ):
        raise AssertionError(f"synthetic run did not satisfy its checks: {result.status}")
    trace_path = Path(result.trace_path)
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    scripted_events = [event for event in events if event.get("event_type") == "model_requested"]
    if len(scripted_events) != llm_instances[0].requests:
        raise AssertionError("scripted request count does not match the saved trajectory")
    tool_results = [
        event.get("payload", {}).get("result", {})
        for event in events
        if event.get("event_type") == "tool_returned"
    ]
    expected_tools = (["load_skill"] if skills_enabled else []) + (
        [
            "run_tests",
            "search_code",
            "read_file",
            "read_file",
            "apply_patch",
            "run_tests",
            "get_git_diff",
        ]
        if scenario == "pagination"
        else ["search_code", "read_file", "apply_patch", "run_tests", "get_git_diff"]
    )
    if [item.get("tool_name") for item in tool_results] != expected_tools:
        raise AssertionError("tool request/result pairs do not match the scripted scenario")
    if any(
        not item.get("success")
        for index, item in enumerate(tool_results)
        if scenario != "pagination" or index != int(skills_enabled)
    ):
        raise AssertionError("a scripted smoke tool returned an unsuccessful result")
    test_results = [item for item in tool_results if item.get("tool_name") == "run_tests"]
    if scenario == "pagination":
        first = test_results[0]
        first_audit = first.get("output", {}).get("audit", {})
        if (
            first.get("success") is not False
            or first.get("output", {}).get("returncode") == 0
            or first_audit.get("exitstatus") == 0
        ):
            raise AssertionError("pagination fixture did not preserve its initial failing test")
    test_result = test_results[-1]
    audit = test_result.get("output", {}).get("audit", {})
    if test_result.get("output", {}).get("returncode") != 0 or audit.get("exitstatus") != 0:
        raise AssertionError("the recorded pytest evidence does not show a successful test")
    diff_result = next(item for item in tool_results if item.get("tool_name") == "get_git_diff")
    recorded_diff = diff_result.get("output", {}).get("diff", "")
    if recorded_diff != Path(result.diff_path).read_text(encoding="utf-8"):
        raise AssertionError("the saved diff does not match the tool result in the trajectory")
    saved_result = json.loads(Path(result.result_path).read_text(encoding="utf-8"))
    if (
        saved_result.get("status") != result.status.value
        or saved_result.get("source_commit") != result.source_commit
        or saved_result.get("changed_files") != list(result.changed_files)
        or saved_result.get("test_runs") != result.test_runs
    ):
        raise AssertionError("the reproduction summary does not match the saved result")
    skill_catalog = [
        event.get("payload", {}).get("skills", [])
        for event in events
        if event.get("event_type") == "skill_catalog_exposed"
    ]
    skill_activations = [
        {key: value for key, value in event.get("payload", {}).items() if key != "content"}
        for event in events
        if event.get("event_type") == "skill_activated"
    ]
    diff_sha256 = hashlib.sha256(Path(result.diff_path).read_bytes()).hexdigest()
    report = {
        "kind": "tracefix_zero_provider_synthetic_reproduction",
        "scenario": scenario,
        "tracefix_source_tree_sha256": _source_sha256(),
        "tracefix_runtime_package_path": str(Path(__file__).resolve().parent),
        "source_commit": result.source_commit,
        "model_name": result.model_name,
        "scripted_model_requests": llm_instances[0].requests,
        "provider_client_constructions": 0,
        "provider_request_attempts": len(provider_request_attempts),
        "network_connect_attempts": len(network_connect_attempts),
        "execution_backend": backend,
        "docker_image_id": image_id,
        "skills_enabled": skills_enabled,
        "status": result.status.value,
        "test_runs": result.test_runs,
        "changed_files": list(result.changed_files),
        "trace_path": str(trace_path),
        "diff_path": result.diff_path,
        "result_path": result.result_path,
        "workspace_preparation": result.workspace_preparation,
        "skill_events": sum(
            event.get("event_type") in {"skill_catalog_exposed", "skill_activated"}
            for event in events
        ),
        "skill_catalog": skill_catalog,
        "skill_activations": skill_activations,
        "trajectory_validation": {
            "model_requests": len(scripted_events),
            "tool_results": len(tool_results),
            "tool_names": [item["tool_name"] for item in tool_results],
            "pytest_returncode": test_result["output"]["returncode"],
            "initial_pytest_returncode": (
                test_results[0]["output"]["returncode"] if scenario == "pagination" else None
            ),
            "pytest_audit_path": test_result["output"]["audit_path"],
            "diff_sha256": diff_sha256,
        },
    }
    (output / "reproduction.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if scenario == "pagination":
        report_path = render_report(Path(result.result_path).parent, output / "report.html")
        report["report_path"] = str(report_path)
        (output / "reproduction.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skills-enabled", action="store_true")
    parser.add_argument("--backend", choices=("local", "docker"), default="local")
    parser.add_argument("--image-id", help="full sha256 image ID required for Docker mode")
    parser.add_argument(
        "--scenario", choices=("smoke", "pagination", "regression-feedback", "memory-experience"),
        default="smoke"
    )
    args = parser.parse_args(argv)
    if args.scenario == "memory-experience":
        if args.backend != "local" or args.skills_enabled or args.image_id:
            parser.error("memory-experience requires local execution without explicit Skills")
        from tracefix.memory_replay import run_memory_replay

        print(json.dumps(run_memory_replay(args.output), ensure_ascii=True, indent=2))
        return 0
    if args.scenario == "regression-feedback":
        if args.backend != "local" or args.skills_enabled or args.image_id:
            parser.error(
                "regression-feedback supports local backend only, without Skills or image ID"
            )
        from tracefix.regression_replay import run_regression_feedback

        print(json.dumps(run_regression_feedback(args.output), ensure_ascii=False, indent=2))
        return 0
    if args.backend == "docker" and not args.image_id:
        parser.error("--image-id is required with --backend docker")
    report = run_smoke(
        args.output,
        skills_enabled=args.skills_enabled,
        backend=args.backend,
        image_id=args.image_id,
        **({"scenario": args.scenario} if args.scenario != "smoke" else {}),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
