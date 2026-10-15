"""Zero-provider ordinary Docker replay for artifact capture and recovery.

The script starts and resumes TraceFix in separate host processes. The first
process commits a Docker checkpoint after a large failing test log and a search
query; the second process destroys/replaces the old task container and reads
the historical artifact before applying a fix.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from tracefix import AgentConfig, RunConfig, TraceFixRunner
from tracefix.checkpoint import CheckpointStore
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.onboarding import verify_patch

PATCH = """diff --git a/widget.py b/widget.py
--- a/widget.py
+++ b/widget.py
@@ -1,2 +1,2 @@
 def next_page(page):
-    return page
+    return page + 1
"""


class ArtifactReplayClient:
    """Choose the next action from durable tool results; no supplier client is used."""

    def __init__(self) -> None:
        self.calls = 0

    def token_counter(self, **_kwargs) -> int:
        return 100

    def completion_cost(self, *, completion_response: object) -> float:
        """This fixture is local-only, so its recorded synthetic calls cost zero."""
        del completion_response
        return 0.0

    def completion(self, **kwargs):
        self.calls += 1
        messages = kwargs.get("messages")
        tools = kwargs.get("tools")
        if not isinstance(messages, list) or not isinstance(tools, list):
            raise AssertionError("artifact replay did not receive the frozen request contract")
        results = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            try:
                result = json.loads(message.get("content") or "")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(result, dict):
                results.append(result)

        tests = [item for item in results if item.get("tool_name") == "run_tests"]
        searches = [
            item
            for item in results
            if item.get("tool_name") == "search_tool_artifact" and item.get("success")
        ]
        reads = [
            item
            for item in results
            if item.get("tool_name") == "read_tool_artifact" and item.get("success")
        ]
        patches = [
            item
            for item in results
            if item.get("tool_name") == "apply_patch" and item.get("success")
        ]
        diffs = [item for item in results if item.get("tool_name") == "get_git_diff"]

        call = None
        if not tests:
            call = ("run_tests", {"command": "pytest -q tests/test_widget.py"})
        elif tests[-1].get("output", {}).get("test_status") != "passed":
            if not searches:
                call = ("search_tool_artifact", {"query": "G2_CONTAINER_MIDLOG"})
            elif not reads:
                hit = searches[-1]["output"]["hits"][0]
                artifact = hit.get("artifact", hit)
                call = (
                    "read_tool_artifact",
                    {
                        "artifact_id": artifact["artifact_id"],
                        "stream": hit["stream"],
                        "offset": hit["offset"],
                        "limit": 4096,
                    },
                )
            elif len(patches) < 1:
                call = ("apply_patch", {"patch": PATCH})
            elif len(tests) <= len(patches):
                call = ("run_tests", {"command": "pytest -q tests/test_widget.py"})
            else:
                raise AssertionError("artifact recovery replay remained on a failing test state")
        elif not diffs:
            call = ("get_git_diff", {"context_lines": 3})

        tool_calls = (
            []
            if call is None
            else [
                {
                    "id": f"artifact-replay-{uuid4().hex}",
                    "type": "function",
                    "function": {"name": call[0], "arguments": json.dumps(call[1])},
                }
            ]
        )
        return {
            "model": "offline/artifact-recovery",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "artifact recovery replay complete" if call is None else None,
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": "stop" if call is None else "tool_calls",
                }
            ],
        }


def _source(root: Path) -> Path:
    repo = root / "source"
    repo.mkdir()
    (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n", encoding="utf-8")
    (repo / "widget.py").write_text("def next_page(page):\n    return page\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_widget.py").write_text(
        "from widget import next_page\n\n"
        "def test_next_page():\n"
        "    print('A' * 50000 + 'G2_CONTAINER_MIDLOG' + 'B' * 50000)\n"
        "    assert next_page(1) == 2\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "--all"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Replay",
            "-c",
            "user.email=replay@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _config(repo: Path, output: Path, image_id: str) -> RunConfig:
    return RunConfig(
        repo=repo,
        task="Fix pagination and retain the failed test log across ordinary Docker recovery",
        model_name="offline/artifact-recovery",
        output_dir=output / "runs",
        env_file=None,
        test_python_executable=None,
        test_target="tests/test_widget.py",
        source_import="widget",
        execution_backend="docker",
        docker_profile="ordinary",
        docker_image_id=image_id,
        docker_recovery_enabled=True,
        conversation_enabled=True,
        harness_foundations_enabled=True,
        agent_config=AgentConfig(
            max_steps=12,
            max_test_runs=3,
            wall_time_seconds=600,
        ),
    )


def _child(phase: str, output: Path, repo: Path, image_id: str, run_root: Path | None) -> dict:
    client = ArtifactReplayClient()
    runner = TraceFixRunner(lambda config: LiteLLMAdapter(config, client=client))
    if phase == "start":
        original_save = CheckpointStore.save

        def pause_after_artifact_search(store, payload, **kwargs):
            saved = original_save(store, payload, **kwargs)
            if kwargs.get("sequence") == 3:
                raise KeyboardInterrupt()
            return saved

        CheckpointStore.save = pause_after_artifact_search
        try:
            result = runner.run(_config(repo, output, image_id))
        finally:
            CheckpointStore.save = original_save
    else:
        if run_root is None:
            raise ValueError("resume phase requires a run checkpoint")
        inspection = runner.inspect(run_root)
        if not inspection.get("resumable"):
            raise RuntimeError(
                "artifact recovery checkpoint is not resumable: "
                + json.dumps(inspection.get("reasons", []), ensure_ascii=False)
            )
        result = runner.resume(run_root)
    return {
        "phase": phase,
        "status": result.status.value,
        "step_count": result.step_count,
        "provider_calls": 0,
        "synthetic_model_requests": client.calls,
        "result_path": result.result_path,
    }


def _invoke_child(
    phase: str, output: Path, repo: Path, image_id: str, run_root: Path | None = None
) -> dict:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--phase",
        phase,
        "--output",
        str(output),
        "--repo",
        str(repo),
        "--image-id",
        image_id,
    ]
    if run_root is not None:
        command.extend(["--run-root", str(run_root)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(
            f"{phase} host process failed ({completed.returncode}):\n"
            f"{completed.stdout[-4000:]}\n{completed.stderr[-4000:]}"
        )
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{phase} host process returned no JSON summary") from exc


def _parent(output: Path, image_id: str) -> dict:
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError("output path must not already exist")
    output.mkdir(parents=True)
    repo = _source(output)
    first = _invoke_child("start", output, repo, image_id)
    if first["status"] != "interrupted":
        raise AssertionError(f"first process did not pause at the committed batch: {first}")
    run_root = Path(first["result_path"]).parent
    snapshots = sorted((run_root / "docker-checkpoints").glob("*.json"))
    if len(snapshots) != 3:
        raise AssertionError("first process did not save the expected three batch snapshots")
    before = [json.loads(path.read_text(encoding="utf-8")) for path in snapshots]
    paused = before[-1]
    receipt = paused.get("artifact_receipt", {})
    if (
        paused.get("artifacts_enabled") is not True
        or receipt.get("artifact_count") != 1
        or receipt.get("queries") != 1
        or receipt.get("run_id") != run_root.name
        or len(receipt.get("artifact_ids", [])) != 1
    ):
        raise AssertionError("checkpoint did not bind the searched artifact and quota")
    checkpoint = json.loads((run_root / "checkpoint.json").read_text(encoding="utf-8"))
    task_checkpoint = checkpoint.get("payload", {}).get("agent", {}).get("task_state")
    if not isinstance(task_checkpoint, dict):
        raise AssertionError("paused Docker checkpoint did not persist the canonical TaskState")
    paused_task_state = task_checkpoint.get("state", {})
    active_requirements = [
        item.get("text")
        for item in paused_task_state.get("requirements", [])
        if item.get("status") == "active"
    ]
    artifact_references = {
        item.get("reference_id")
        for item in paused_task_state.get("references", [])
        if item.get("kind") == "artifact"
    }
    if active_requirements != [
        "Fix pagination and retain the failed test log across ordinary Docker recovery"
    ] or artifact_references != set(receipt["artifact_ids"]):
        raise AssertionError("paused Docker TaskState lost its direct requirement or artifact ref")
    old_container = paused["container_id"]
    inspect = subprocess.run(["docker", "inspect", old_container], capture_output=True)
    if inspect.returncode == 0:
        raise AssertionError("interrupted task container was not destroyed before host restart")

    resumed = _invoke_child("resume", output, repo, image_id, run_root)
    if resumed["status"] != "completed":
        raise AssertionError(f"resumed replay did not complete: {resumed}")
    result_path = Path(resumed["result_path"])
    verification = verify_patch(result_path.parent)
    if verification.get("passed") is not True:
        raise AssertionError("independent artifact replay patch verification failed")
    resumed_checkpoint = json.loads((run_root / "checkpoint.json").read_text(encoding="utf-8"))
    resumed_task_state = (
        resumed_checkpoint.get("payload", {})
        .get("agent", {})
        .get("task_state", {})
        .get("state", {})
    )
    resumed_artifact_references = {
        item.get("reference_id")
        for item in resumed_task_state.get("references", [])
        if item.get("kind") == "artifact"
    }
    if (
        resumed_task_state.get("revision", -1) < paused_task_state.get("revision", 0)
        or resumed_artifact_references != artifact_references
        or [
            item.get("text")
            for item in resumed_task_state.get("requirements", [])
            if item.get("status") == "active"
        ]
        != active_requirements
    ):
        raise AssertionError("new Docker process did not restore the committed TaskState")
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((run_root / "docker-checkpoints").glob("*.json"))
    ]
    if not any(
        item.get("sequence", 0) > paused["sequence"]
        and item.get("container_id") != old_container
        and item.get("artifact_receipt", {}).get("queries", 0) >= 2
        for item in records
    ):
        raise AssertionError("recovered container did not restore and advance artifact query quota")
    events = [
        json.loads(line)
        for line in (run_root / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    read_results = [
        event.get("payload", {}).get("result", {})
        for event in events
        if event.get("event_type") == "tool_returned"
        and event.get("payload", {}).get("result", {}).get("tool_name") == "read_tool_artifact"
    ]
    if not any(
        item.get("success") and "G2_CONTAINER_MIDLOG" in item.get("output", {}).get("content", "")
        for item in read_results
    ):
        raise AssertionError("resumed process did not retrieve the middle of the original test log")
    remaining = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=tracefix-{run_root.name}", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if remaining:
        raise AssertionError("artifact recovery replay leaked a task container")
    return {
        "passed": True,
        "provider_calls": first["provider_calls"] + resumed["provider_calls"],
        "synthetic_model_requests": (
            first["synthetic_model_requests"] + resumed["synthetic_model_requests"]
        ),
        "first_process": first,
        "second_process": resumed,
        "artifact_id": read_results[0]["output"]["artifact_id"],
        "artifact_queries_at_pause": receipt["queries"],
        "task_state_revision_at_pause": paused_task_state["revision"],
        "task_state_revision_after_resume": resumed_task_state["revision"],
        "patch_verification": verification,
        "run_root": str(run_root),
        "all_containers_removed": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--phase", choices=("parent", "start", "resume"), default="parent")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--run-root", type=Path)
    args = parser.parse_args(argv)
    if args.phase == "parent":
        result = _parent(args.output, args.image_id)
    else:
        if args.repo is None:
            parser.error("host child phase requires --repo")
        result = _child(args.phase, args.output, args.repo, args.image_id, args.run_root)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
