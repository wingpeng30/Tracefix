"""Zero-provider ordinary Docker replay for artifact capture and recovery.

The script starts and resumes TraceFix in separate host processes. The first
process commits a Docker checkpoint after a large failing test log and a search
query; the second process destroys/replaces the old task container and reads
the historical artifact before applying a fix.
"""

from __future__ import annotations

import argparse
import hashlib
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

COMPOSITE_TASK = (
    "Fix pagination and retain the failed test log across ordinary Docker recovery; "
    "保留零页码语义。"
)
CONTINUED_REQUIREMENT = (
    "Also keep the selected zero-page behavior and report the verified regression gate."
)


class ArtifactReplayClient:
    """Choose the next action from durable tool results; no supplier client is used."""

    def __init__(self, *, composite: bool = False) -> None:
        self.calls = 0
        self.composite = composite

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
        if not isinstance(messages, list):
            raise AssertionError("artifact replay did not receive the frozen request contract")
        if not tools:
            if not self.composite:
                raise AssertionError("ordinary artifact replay unexpectedly requested reflection")
            try:
                payload = json.loads(messages[-1].get("content") or "")
                receipts = payload["evidence"]["trusted_user_inputs"]
                statement = next(
                    item for item in receipts
                    if "保留零页码语义" in item.get("content", "")
                )
            except (
                AttributeError,
                IndexError,
                KeyError,
                StopIteration,
                TypeError,
                json.JSONDecodeError,
            ) as exc:
                raise AssertionError("memory reflection had no direct user evidence") from exc
            answer = json.dumps(
                {
                    "key": "g4-replay-decision",
                    "summary": "保留零页码语义",
                    "keywords": ["页码", "pagination"],
                    "kind": "decision",
                    "user_receipt_id": statement["receipt"]["message_id"],
                    "generalized": False,
                },
                ensure_ascii=False,
            )
            call = None
            tool_calls = []
        elif not isinstance(tools, list):
            raise AssertionError("artifact replay tool catalog was not a list")
        else:
            answer = None
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

            def successful(name: str) -> list[dict]:
                return [
                    item for item in results
                    if item.get("tool_name") == name and item.get("success") is True
                ]

            tests = [item for item in results if item.get("tool_name") == "run_tests"]
            searches = successful("search_tool_artifact")
            reads = successful("read_tool_artifact")
            patches = successful("apply_patch")
            diffs = successful("get_git_diff")
            call = None

            if self.composite:
                symbols = successful("mcp_serena_symbols")
                symbol_attempts = [
                    item for item in results if item.get("tool_name") == "mcp_serena_symbols"
                ]
                if symbol_attempts and not symbols:
                    raise AssertionError("Serena symbol query failed in the composite replay")
                skills = successful("load_skill")
                skill_attempts = [
                    item for item in results if item.get("tool_name") == "load_skill"
                ]
                if skill_attempts and not skills:
                    raise AssertionError("custom Skill or Markdown reference could not be loaded")
                state_reads = successful("get_task_state")
                state_updates = successful("update_task_state")
                if not symbols:
                    call = ("mcp_serena_symbols", {"relative_path": "widget.py"})
                elif len(skills) < 1:
                    call = ("load_skill", {"name": "g4-review"})
                elif len(skills) < 2:
                    call = (
                        "load_skill",
                        {"name": "g4-review", "reference": "references/evidence.md"},
                    )
                elif not state_reads:
                    call = ("get_task_state", {})
                elif not state_updates:
                    call = (
                        "update_task_state",
                        {
                            "expected_revision": state_reads[-1]["output"]["revision"],
                            "operations": [
                                {
                                    "action": "add",
                                    "collection": "plan",
                                    "text": (
                                        "Use fixed memory, Skill evidence, and the current "
                                        "source snapshot."
                                    ),
                                }
                            ],
                        },
                    )

            if call is None and not tests:
                call = ("run_tests", {"command": "pytest -q tests/test_widget.py"})
            elif call is None and tests[-1].get("output", {}).get("test_status") != "passed":
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
                elif not patches:
                    call = ("apply_patch", {"patch": PATCH})
                elif len(tests) <= len(patches):
                    call = ("run_tests", {"command": "pytest -q tests/test_widget.py"})
                else:
                    raise AssertionError(
                        "artifact recovery replay remained on a failing test state"
                    )
            elif call is None and tests[-1].get("output", {}).get("test_status") == "passed":
                if self.composite and len(successful("mcp_serena_symbols")) < 2:
                    call = ("mcp_serena_symbols", {"relative_path": "widget.py"})
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
            answer = "artifact recovery replay complete" if call is None else None
        return {
            "model": "offline/artifact-recovery",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": answer,
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": "stop" if call is None else "tool_calls",
                }
            ],
        }


def _source(root: Path, *, composite: bool = False) -> Path:
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
    if composite:
        (repo / "identity.py").write_text(
            "def same(value):\n    return value\n", encoding="utf-8"
        )
        (tests / "test_identity.py").write_text(
            "from identity import same\n\ndef test_identity():\n    assert same(7) == 7\n",
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


def _seed_memory(repo: Path, memory_dir: Path) -> None:
    from tracefix.memory import ExperienceProposal, ExperienceStore
    from tracefix.memory_domain import KnowledgeKind, issue_user_input_receipt

    statement = "保留零页码语义"
    receipt = issue_user_input_receipt("g4-memory-seed", 1, statement)
    proposal = ExperienceProposal(
        key="pagination-decision",
        summary=statement,
        keywords=["页码", "pagination"],
        kind=KnowledgeKind.DECISION,
        user_receipt_id=receipt.message_id,
        generalized=False,
    )
    evidence = {
        # No tool/test was executed and no patch exists in this seed.
        "source_sha256": hashlib.sha256(b"").hexdigest(),
        "latest_test": None,
        "tool_results": {},
        "trusted_user_inputs": [
            {"receipt": receipt.model_dump(mode="json"), "content": statement}
        ]
    }
    result = ExperienceStore(memory_dir, repo).publish("g4-memory-seed", proposal, evidence, [])
    if result.get("status") != "verified":
        raise AssertionError("composite replay could not seed a verified user-stated memory")


def _write_custom_skill(skills_root: Path, *, changed: bool = False) -> None:
    skill = skills_root / "g4-review"
    references = skill / "references"
    references.mkdir(parents=True, exist_ok=True)
    version = "changed after checkpoint" if changed else "frozen source and artifact evidence"
    (skill / "SKILL.md").write_text(
        "---\nname: g4-review\ndescription: Check harness recovery evidence.\n"
        "metadata:\n  version: \"1.0\"\n---\n\n"
        "Read each source snapshot and artifact receipt before making a change.\n",
        encoding="utf-8",
    )
    (references / "evidence.md").write_text(
        f"# Evidence review\n\nUse the {version} recorded by this replay.\n",
        encoding="utf-8",
    )


def _config(
    repo: Path,
    output: Path,
    image_id: str,
    *,
    composite: bool = False,
    mcp_image_id: str | None = None,
) -> RunConfig:
    return RunConfig(
        repo=repo,
        task=(
            COMPOSITE_TASK
            if composite
            else "Fix pagination and retain the failed test log across ordinary Docker recovery"
        ),
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
        regression_targets=("tests/test_identity.py",) if composite else (),
        memory_enabled=composite,
        memory_dir=output / "memory",
        skills_root=output / "custom-skills" if composite else None,
        mcp_serena_image_id=mcp_image_id,
        agent_config=AgentConfig(
            max_steps=24,
            # Composite: one baseline, two agent tests, then two targets in
            # each of the two turns. Keep a finite allowance for all seven.
            max_test_runs=8 if composite else 6,
            wall_time_seconds=600,
            skills_enabled=composite,
        ),
    )


def _child(
    phase: str,
    output: Path,
    repo: Path,
    image_id: str,
    run_root: Path | None,
    *,
    composite: bool = False,
    mcp_image_id: str | None = None,
) -> dict:
    client = ArtifactReplayClient(composite=composite)
    runner = TraceFixRunner(lambda config: LiteLLMAdapter(config, client=client))
    if phase == "start":
        original_save = CheckpointStore.save

        def pause_after_artifact_search(store, payload, **kwargs):
            saved = original_save(store, payload, **kwargs)
            pause_sequence = 8 if composite else 3
            if kwargs.get("sequence") == pause_sequence:
                raise KeyboardInterrupt()
            return saved

        CheckpointStore.save = pause_after_artifact_search
        try:
            result = runner.run(
                _config(
                    repo,
                    output,
                    image_id,
                    composite=composite,
                    mcp_image_id=mcp_image_id,
                )
            )
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
        resume_status = result.status.value
        if composite:
            if resume_status != "completed":
                raise RuntimeError(
                    f"composite replay did not finish after recovery: {resume_status}"
                )
            result = runner.continue_turn(run_root, CONTINUED_REQUIREMENT)
    if phase == "start":
        resume_status = None
    return {
        "phase": phase,
        "status": result.status.value,
        "resume_status": resume_status,
        "turn_number": result.turn_number,
        "validation_gate_status": result.validation_gate_status,
        "memory_status": result.memory_status,
        "test_runs": result.test_runs,
        "step_count": result.step_count,
        "provider_calls": 0,
        "synthetic_model_requests": client.calls,
        "result_path": result.result_path,
    }


def _invoke_child(
    phase: str,
    output: Path,
    repo: Path,
    image_id: str,
    run_root: Path | None = None,
    *,
    composite: bool = False,
    mcp_image_id: str | None = None,
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
    if composite:
        command.append("--composite")
    if mcp_image_id is not None:
        command.extend(["--mcp-image-id", mcp_image_id])
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


def _parent(
    output: Path,
    image_id: str,
    *,
    composite: bool = False,
    mcp_image_id: str | None = None,
) -> dict:
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError("output path must not already exist")
    output.mkdir(parents=True)
    repo = _source(output, composite=composite)
    skills_root = output / "custom-skills"
    memory_dir = output / "memory"
    if composite:
        if mcp_image_id is None:
            raise ValueError("composite replay requires an immutable Serena MCP image ID")
        _write_custom_skill(skills_root)
        _seed_memory(repo, memory_dir)
    first = _invoke_child(
        "start", output, repo, image_id, composite=composite, mcp_image_id=mcp_image_id
    )
    if first["status"] != "interrupted":
        raise AssertionError(f"first process did not pause at the committed batch: {first}")
    run_root = Path(first["result_path"]).parent
    snapshots = sorted((run_root / "docker-checkpoints").glob("*.json"))
    expected_snapshots = 8 if composite else 3
    if len(snapshots) != expected_snapshots:
        raise AssertionError(
            f"first process did not save the expected {expected_snapshots} batch snapshots"
        )
    before = [json.loads(path.read_text(encoding="utf-8")) for path in snapshots]
    paused = before[-1]
    receipt = paused.get("artifact_receipt", {})
    receipt_artifact_ids = receipt.get("artifact_ids", [])
    if not isinstance(receipt_artifact_ids, list):
        receipt_artifact_ids = []
    receipt_artifact_id_set = set(receipt_artifact_ids)
    if (
        paused.get("artifacts_enabled") is not True
        or receipt.get("artifact_count") != len(receipt_artifact_ids)
        or not receipt_artifact_ids
        or receipt.get("queries", 0) < 1
        or receipt.get("returned_chars", 0) < 1
        or receipt.get("run_id") != run_root.name
    ):
        raise AssertionError(
            "checkpoint did not bind the searched artifact and cumulative query quota: "
            + json.dumps(receipt, sort_keys=True)
        )
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
    searched_artifact_ids = {
        artifact_id
        for item in paused_task_state.get("observations", [])
        if item.get("kind") == "artifact_lookup"
        for artifact_id in item.get("artifact_ids", [])
    }
    if active_requirements != [
        COMPOSITE_TASK if composite
        else "Fix pagination and retain the failed test log across ordinary Docker recovery"
    ] or (
        not searched_artifact_ids
        or not searched_artifact_ids.issubset(artifact_references)
        or not searched_artifact_ids.issubset(receipt_artifact_id_set)
        or not artifact_references.issubset(receipt_artifact_id_set)
    ):
        raise AssertionError("paused Docker TaskState lost its direct requirement or artifact ref")
    memory_bundle_sha256 = None
    skills_bundle_sha256 = None
    if composite:
        session = json.loads((run_root / "session.json").read_text(encoding="utf-8"))
        identity = session.get("identity", {})
        memory_path = run_root / "memory-recall.json"
        memory_bytes = memory_path.read_bytes()
        memory_bundle_sha256 = hashlib.sha256(memory_bytes).hexdigest()
        memory_bundle = json.loads(memory_bytes)
        recalled = memory_bundle.get("items", [])
        if (
            len(recalled) != 1
            or recalled[0].get("key") != "pagination-decision"
            or recalled[0].get("evidence") != "user_stated"
            or recalled[0].get("applicability", {}).get("status") != "matched"
            or not identity.get("skills_archive_sha256")
            or not identity.get("skills_bundle_sha256")
            or not identity.get("policy_sha256")
            or not identity.get("executor_tool_catalog_sha256")
            or not identity.get("agent_tool_catalog_sha256")
            or not identity.get("provider_tool_spec_sha256")
        ):
            raise AssertionError(
                "composite checkpoint lacks fixed memory, Skills, or policy identities"
            )
        skills_bundle_sha256 = identity["skills_bundle_sha256"]
        reference_kinds = {
            item.get("kind") for item in paused_task_state.get("references", [])
        }
        if not {"memory", "skill", "artifact"}.issubset(reference_kinds):
            raise AssertionError(
                "TaskState did not retain its memory, custom Skill, and artifact refs"
            )
    old_container = paused["container_id"]
    inspect = subprocess.run(["docker", "inspect", old_container], capture_output=True)
    if inspect.returncode == 0:
        raise AssertionError("interrupted task container was not destroyed before host restart")

    if composite:
        from tracefix.memory import ExperienceStore

        ExperienceStore(memory_dir, repo).disable("pagination-decision")
        _write_custom_skill(skills_root, changed=True)

    resumed = _invoke_child(
        "resume",
        output,
        repo,
        image_id,
        run_root,
        composite=composite,
        mcp_image_id=mcp_image_id,
    )
    if resumed["status"] != "completed":
        raise AssertionError(f"resumed replay did not complete: {resumed}")
    result_path = Path(resumed["result_path"])
    verification = verify_patch(
        result_path.parent,
        regression_targets=("tests/test_identity.py",) if composite else (),
    )
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
        or not artifact_references.issubset(resumed_artifact_references)
        or [
            item.get("text")
            for item in resumed_task_state.get("requirements", [])
            if item.get("status") == "active"
        ]
        != (
            [COMPOSITE_TASK, CONTINUED_REQUIREMENT]
            if composite
            else active_requirements
        )
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
    mcp_queries = [
        event.get("payload", {}).get("result", {})
        for event in events
        if event.get("event_type") == "tool_returned"
        and event.get("payload", {}).get("result", {}).get("tool_name")
        == "mcp_serena_symbols"
    ]
    if composite:
        if (
            len(mcp_queries) != 2
            or not all(item.get("success") for item in mcp_queries)
            or not all(item.get("output", {}).get("source_sha256") for item in mcp_queries)
            or mcp_queries[0]["output"]["source_sha256"]
            == mcp_queries[1]["output"]["source_sha256"]
            or resumed.get("validation_gate_status") != "passed"
            or resumed.get("turn_number") != 2
            or resumed.get("memory_status", {}).get("status") != "duplicate"
            or resumed_task_state.get("revision", 0) <= paused_task_state.get("revision", 0)
            or hashlib.sha256((run_root / "memory-recall.json").read_bytes()).hexdigest()
            != memory_bundle_sha256
            or json.loads((run_root / "session.json").read_text(encoding="utf-8"))
            .get("identity", {})
            .get("skills_bundle_sha256")
            != skills_bundle_sha256
        ):
            raise AssertionError(
                "memory, custom Skill snapshot, Serena freshness, TaskState, or regression gate "
                "did not survive the composite recovery"
            )
        live_memory = ExperienceStore(memory_dir, repo)
        disabled_memory = live_memory.show("pagination-decision")
        if (
            disabled_memory.get("disabled") is not True
            or disabled_memory.get("active") is not None
        ):
            raise AssertionError("live memory mutation was not applied during the pause")
    remaining = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=tracefix-{run_root.name}", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if remaining:
        raise AssertionError("artifact recovery replay leaked a task container")
    if composite:
        run_tag = hashlib.sha256(str(run_root).encode()).hexdigest()[:12]
        mcp_remaining = subprocess.run(
            [
                "docker", "ps", "-a", "--filter", f"name=tracefix-mcp-{run_tag}",
                "--format", "{{.ID}}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if mcp_remaining:
            raise AssertionError("composite replay leaked an isolated Serena container")
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
        "composite": composite,
        "serena_queries": len(mcp_queries),
        "memory_bundle_sha256": memory_bundle_sha256,
        "skills_bundle_sha256": skills_bundle_sha256,
        "validation_gate_status": resumed.get("validation_gate_status"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--mcp-image-id")
    parser.add_argument("--composite", action="store_true")
    parser.add_argument("--phase", choices=("parent", "start", "resume"), default="parent")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--run-root", type=Path)
    args = parser.parse_args(argv)
    if args.composite and args.mcp_image_id is None:
        parser.error("--composite requires --mcp-image-id")
    if args.phase == "parent":
        result = _parent(
            args.output,
            args.image_id,
            composite=args.composite,
            mcp_image_id=args.mcp_image_id,
        )
    else:
        if args.repo is None:
            parser.error("host child phase requires --repo")
        result = _child(
            args.phase,
            args.output,
            args.repo,
            args.image_id,
            args.run_root,
            composite=args.composite,
            mcp_image_id=args.mcp_image_id,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
