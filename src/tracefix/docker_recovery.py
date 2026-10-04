"""Read-only validation of the durable ordinary Docker batch artifacts."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path, PureWindowsPath

from tracefix.checkpoint import CheckpointError
from tracefix.memory import digest
from tracefix.workspace_snapshot import validate_archive


def tool_identity(specs) -> str:
    """Hash exact JSON contracts with equivalent integral JSON numbers normalized.

    Pydantic versions emit numeric schema bounds as either 1 or 1.0. JSON Schema
    treats these numbers identically; every key and actual numeric value remains bound.
    """
    def normalize(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    data = [spec.model_dump(mode="json") for spec in specs]
    return hashlib.sha256(json.dumps(normalize(data), sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def owned_file(root: Path, name: str) -> Path:
    """Reject traversal and links before reading a checkpoint-owned artifact."""
    if not isinstance(name, str) or not name or PureWindowsPath(name).drive:
        raise CheckpointError("invalid Docker checkpoint artifact path")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise CheckpointError("Docker checkpoint artifact escapes run directory")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise CheckpointError("Docker checkpoint artifact is a link")
    if not current.is_file() or not current.resolve().is_relative_to(root.resolve()):
        raise CheckpointError("Docker checkpoint artifact is missing")
    return current


def validate_batch(root: Path, snapshot: dict, *, sequence: int, image_id: str) -> None:
    """Verify all batch bindings without starting a container or constructing a model."""
    if snapshot.get("schema_version") != 1 or snapshot.get("sequence") != sequence:
        raise CheckpointError("Docker checkpoint batch sequence changed")
    batch = snapshot.get("batch_id")
    if (not isinstance(batch, str) or len(batch) != 41
            or batch[:8] != f"{sequence:08d}" or batch[8] != "-"
            or any(char not in "0123456789abcdef" for char in batch[9:])):
        raise CheckpointError("invalid Docker checkpoint batch identity")
    archive = owned_file(root, snapshot["archive"])
    expected = root / "docker-checkpoints" / f"{batch}.tar"
    if archive != expected:
        raise CheckpointError("Docker snapshot belongs to another batch")
    sidecar = owned_file(root, f"docker-checkpoints/{batch}.json")
    if json.loads(sidecar.read_text(encoding="utf-8")) != snapshot:
        raise CheckpointError("Docker batch metadata changed")
    if snapshot.get("image_id") != image_id:
        raise CheckpointError("Docker image identity changed")
    baseline = owned_file(root, "agent-base.tar")
    if hashlib.sha256(baseline.read_bytes()).hexdigest() != snapshot["baseline_sha256"]:
        raise CheckpointError("Docker source baseline changed")
    if digest(snapshot["bridge_state"]) != snapshot["bridge_state_sha256"]:
        raise CheckpointError("Docker bridge state changed")
    validate_archive(archive, snapshot["metadata"])
    for name, expected_sha in snapshot["test_evidence"].items():
        if Path(name).name != name or "\\" in name:
            raise CheckpointError("invalid Docker test evidence path")
        evidence = owned_file(root, f"test-evidence/{name}")
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != expected_sha:
            raise CheckpointError("Docker test evidence changed")
    journal = owned_file(root, "tool-events.jsonl").read_bytes()
    size = snapshot["journal_size"]
    if (not isinstance(size, int) or size < 0 or size > len(journal)
            or hashlib.sha256(journal[:size]).hexdigest() != snapshot["journal_sha256"]):
        raise CheckpointError("Docker journal prefix changed")
    pending = {}
    for line in journal.splitlines():
        event = json.loads(line)
        if event["event"] == "handshake":
            continue
        call_id = event["call_id"]
        if event["event"] == "dispatch_started":
            if call_id in pending:
                raise CheckpointError("duplicate unresolved Docker tool call")
            pending[call_id] = event["tool"]
        elif event["event"] == "result_received":
            if call_id not in pending:
                raise CheckpointError("Docker result has no dispatch")
            pending.pop(call_id)
    if pending:
        raise CheckpointError("Docker tool outcome is unknown")
    for line in journal[size:].splitlines():
        if json.loads(line)["tool"] not in {"get_git_diff", "__recovery_state__"}:
            raise CheckpointError("uncommitted Docker tool batch after checkpoint")


def inspect_session(root: Path, manifest: dict, config, *, implementation_sha256: str,
                    config_sha256: str, for_continue: bool = False) -> dict:
    """Inspect an ordinary Docker session independently of its former container."""
    from tracefix.agent import AgentState, AgentStatus, MinimalAgent
    from tracefix.checkpoint import CheckpointStore
    from tracefix.messages import Message, MessageHistory
    from tracefix.repository import RepoMap
    from tracefix.tools.builtin import (
        ApplyPatchTool,
        GetGitDiffTool,
        ReadFileTool,
        RunTestsTool,
        SearchCodeTool,
    )

    reasons = []
    if not config.docker_recovery_enabled or manifest.get("checkpoint_supported") is not True:
        return {"resumable": False, "reasons": ["Docker recovery was not enabled"],
                "run": str(root)}
    identity = manifest["identity"]
    actual = dict(identity, implementation_sha256=implementation_sha256,
                  config_sha256=config_sha256)
    map_file = root / "repo-map.json"
    repo_map = RepoMap.model_validate_json(map_file.read_text(encoding="utf-8")) \
        if map_file.is_file() else None
    actual["repo_map_sha256"] = hashlib.sha256(
        (repo_map.text if repo_map else "").encode("utf-8")).hexdigest()
    store = CheckpointStore(root, identity)
    inspection = store.inspect(current_identity=actual)
    reasons.extend(inspection.reasons)
    if not inspection.resumable:
        return {"resumable": False, "reasons": reasons, "run": str(root)}
    checkpoint = store.load(current_identity=actual)
    payload = checkpoint.payload
    snapshot = payload["docker_snapshot"]
    if snapshot.get("tool_sha256") != identity["tool_sha256"]:
        raise CheckpointError("Docker batch tool identity disagrees with session")
    validate_batch(root, snapshot, sequence=checkpoint.sequence, image_id=config.docker_image_id)
    bridge = snapshot["bridge_state"]
    if (payload["skills"] != bridge["skills"]
            or payload["protected_dirs"] != bridge["protected_dirs"]):
        raise CheckpointError("Agent and Docker bridge state disagree")
    protected = bridge["protected_dirs"]
    if (not isinstance(protected, list) or not all(isinstance(name, str) for name in protected)
            or ".tracefix-build-tmp" not in protected
            or not set(protected).issubset({".tracefix-build-tmp", ".tracefix-test-tmp"})):
        raise CheckpointError("invalid Docker protected directory identity")
    definitions = [tool._SPEC.model_copy(deep=True) for tool in (
        SearchCodeTool, ReadFileTool, ApplyPatchTool, RunTestsTool, GetGitDiffTool,
    )]
    skill = None
    if config.agent_config.skills_enabled or config.memory_enabled:
        from tracefix.tools.skills import SkillActivationTool

        skill_root = root / "experience-skills" if config.memory_enabled else config.skills_root
        skill = (SkillActivationTool(root=skill_root, limits=config.agent_config.skill_limits)
                 if skill_root else SkillActivationTool(limits=config.agent_config.skill_limits))
        definitions.append(skill.spec)
    tool_sha = tool_identity(definitions)
    if tool_sha != identity["tool_sha256"]:
        reasons.append("tool definitions changed")
    if skill is not None:
        skill.restore_recovery_state(payload["skills"])
    elif payload["skills"] is not None:
        reasons.append("checkpoint contains disabled Skills")
    diff_sha = hashlib.sha256(bridge["diff"]["diff"].encode("utf-8")).hexdigest()
    if diff_sha != payload["workspace_diff_sha256"]:
        raise CheckpointError("Agent and Docker product diff disagree")
    state = AgentState.model_validate(payload["agent"]["state"])
    history = MessageHistory(Message.model_validate(row) for row in payload["agent"]["history"])
    if history.pending_tool_call_ids:
        reasons.append("checkpoint message history has pending calls")
    runtime_memory = payload["agent"]["memory"]
    if not isinstance(runtime_memory, dict) or any(
        name not in runtime_memory for name in MinimalAgent._RECOVERY_FIELDS
    ):
        reasons.append("checkpoint runtime memory is incomplete")
    fact = runtime_memory.get("_last_test_evidence") if isinstance(runtime_memory, dict) else None
    if (isinstance(fact, dict) and fact.get("valid") is True
            and fact.get("source_sha256") != diff_sha):
        reasons.append("passing test does not match Docker checkpoint products")
    if not state.cost_complete or not state.usage_complete:
        reasons.append("session has unresolved model accounting")
    if state.memory_status.get("status") in {"outcome_unknown", "response_received"}:
        reasons.append("session has an unresolved memory request")
    trace = owned_file(root, "trajectory.jsonl").read_bytes()
    size = payload["trace_size"]
    if (not isinstance(size, int) or size < 0 or size > len(trace)
            or hashlib.sha256(trace[:size]).hexdigest() != payload["trace_sha256"]):
        raise CheckpointError("Docker trajectory prefix changed")
    allowed_status = {AgentStatus.INTERRUPTED.value}
    if for_continue:
        allowed_status.add(AgentStatus.COMPLETED.value)
        if not config.conversation_enabled or manifest.get("schema_version") != 2:
            reasons.append("run is not a continuous session")
        if (state.step_count >= config.agent_config.max_steps
                or state.test_runs >= config.agent_config.max_test_runs
                or state.input_tokens >= config.agent_config.max_input_tokens
                or state.output_tokens >= config.agent_config.max_output_tokens
                or payload["agent"]["active_seconds"] >= config.agent_config.wall_time_seconds):
            reasons.append("cumulative session budget exhausted")
        result_data = json.loads(owned_file(root, "result.json").read_text(encoding="utf-8"))
        if result_data.get("turn_number", 1) != state.turn_number:
            reasons.append("current round result is not committed")
        if config.conversation_enabled:
            for turn in range(1, state.turn_number + 1):
                prefix = f"turns/{turn:04d}"
                record = json.loads(owned_file(root, f"{prefix}/latest.json").read_text(
                    encoding="utf-8"))
                attempt = record["attempt"]
                if (record.get("schema_version") != 1 or record.get("turn_number") != turn
                        or not isinstance(attempt, str) or len(attempt) != 32
                        or any(char not in "0123456789abcdef" for char in attempt)):
                    raise CheckpointError("invalid conversation round identity")
                required = {"patch.diff", "trajectory.jsonl", "session.json",
                            "validation.json", "result.json"}
                if set(record["artifacts"]) != required:
                    raise CheckpointError("incomplete conversation round artifacts")
                for name, expected in record["artifacts"].items():
                    artifact = owned_file(root, f"{prefix}/{attempt}/{name}")
                    if hashlib.sha256(artifact.read_bytes()).hexdigest() != expected:
                        raise CheckpointError("conversation round artifact changed")
    for line in trace[size:].splitlines():
        event = json.loads(line)
        if event["event_type"] not in {"agent_state_changed", "task_finished"}:
            reasons.append("uncommitted model or tool outcome after checkpoint")
        elif event["event_type"] == "task_finished" and event.get("payload", {}).get(
            "state", {}).get("status") not in allowed_status:
            reasons.append("run has a non-interrupted terminal event")
    result_file = root / "result.json"
    if result_file.is_file() and json.loads(result_file.read_text(encoding="utf-8"))[
        "status"] not in allowed_status:
        reasons.append("run is not interrupted")
    try:
        image = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}",
                                config.docker_image_id], capture_output=True, text=True,
                               timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckpointError("Docker image inspection is unavailable") from exc
    if image.returncode or image.stdout.strip() != config.docker_image_id:
        reasons.append("saved Docker image is unavailable or changed")
    return {"resumable": not reasons, "reasons": reasons, "run": str(root),
            "sequence": checkpoint.sequence, "step_count": state.step_count,
            "turn_number": state.turn_number,
            "task": state.task, "phase": state.phase.value,
            "validation_status": state.validation_status,
            "workspace_diff_sha256": diff_sha}
