"""Zero-provider public Runner replay: cancellation, natural completion and process recovery."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from tracefix import AgentConfig, BaseLLM, RunConfig, TraceFixRunner
from tracefix.checkpoint import CheckpointStore
from tracefix.jobs.domain import JobError
from tracefix.jobs.test_budget import TestExecutionLedger
from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.models.base import LLMResponse, TokenUsage


def outputs(messages, tool_name):
    result = []
    for message in messages:
        if message.role is MessageRole.TOOL:
            value = json.loads(message.content)
            if value.get("success") and value.get("tool_name") == tool_name:
                result.append(value["output"])
    return result


class JobModel(BaseLLM):
    def __init__(self, config, *, restore=False):
        super().__init__(config)
        self.restore, self.requests = restore, 0
        self.phase, self.job_id, self.cursor = "start", None, None
        self.middle_seen = False

    def complete(self, messages, tools=()):
        self.requests += 1
        time.sleep(0.05)
        if self.restore:
            pages = outputs(messages, "read_job_output")
            page = next(row for row in pages if "G5-MIDDLE" in row.get("content", ""))
            name, arguments = "read_job_output", {
                "job_id": page["job_id"], "cursor": page["start_cursor"]}
        elif self.phase == "start":
            name, arguments = "start_test_job", {"command": "pytest -q -s test_long.py::test_long"}
            self.phase = "read"
        elif self.phase == "read":
            self.job_id = outputs(messages, "start_test_job")[0]["job"]["identity"]["job_id"]
            pages = outputs(messages, "read_job_output")
            if pages:
                self.cursor = pages[-1]["cursor"]
                self.middle_seen |= "G5-MIDDLE" in pages[-1].get("content", "")
            if self.middle_seen:
                name, arguments = "cancel_job", {"job_id": self.job_id}
                self.phase = "cancelled"
            else:
                name, arguments = "read_job_output", {"job_id": self.job_id, "cursor": self.cursor}
        elif self.phase == "cancelled":
            states = outputs(messages, "get_job_status")
            if states and states[-1]["job"]["status"] == "cancelled":
                assert states[-1]["job"]["tree_exit_confirmed"]
                name, arguments = "start_test_job", {"command": "pytest -q test_long.py::test_fast"}
                self.phase = "short"
            else:
                name, arguments = "get_job_status", {"job_id": self.job_id}
        else:
            starts = outputs(messages, "start_test_job")
            assert len(starts) == 2
            name, arguments = "get_job_status", {"job_id": starts[-1]["job"]["identity"]["job_id"]}
        return LLMResponse(message=Message(role=MessageRole.ASSISTANT, tool_calls=(
            ToolCall(id=f"request-{self.requests}", name=name, arguments=arguments),)),
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2, cost_usd=0),
            model_name=self.config.model_name)


class FixModel(BaseLLM):
    requests = 0

    def complete(self, messages, tools=()):
        self.requests += 1
        calls = ()
        if self.requests == 1:
            calls = (ToolCall(id="fix", name="apply_patch", arguments={"patch":
                "diff --git a/sample.py b/sample.py\n--- a/sample.py\n+++ b/sample.py\n"
                "@@ -1,2 +1,2 @@\n-def value(): return 0\n+def value(): return 1\n"
                " def identity(x): return x\n"}),)
        return LLMResponse(message=Message(role=MessageRole.ASSISTANT, tool_calls=calls,
            content=None if calls else "done"), usage=TokenUsage(input_tokens=1, output_tokens=1,
            total_tokens=2, cost_usd=0), model_name=self.config.model_name)


class EscapeModel(BaseLLM):
    requests = 0
    cancelled = False

    def complete(self, messages, tools=()):
        self.requests += 1
        time.sleep(0.1)
        starts = outputs(messages, "start_test_job")
        if not starts:
            name, args = "start_test_job", {"command": "pytest -q -s test_escape.py"}
        else:
            job_id = starts[-1]["job"]["identity"]["job_id"]
            states = outputs(messages, "get_job_status")
            assert not any(row["job"]["status"] == "outcome_unknown" for row in states), (
                "unknown execution must stop further model requests")
            if self.cancelled:
                name, args = "get_job_status", {"job_id": job_id}
            elif any("G5-ESCAPE-READY" in row.get("content", "")
                     for row in outputs(messages, "read_job_output")):
                name, args = "cancel_job", {"job_id": job_id}
                self.cancelled = True
            else:
                name, args = "read_job_output", {"job_id": job_id}
        return LLMResponse(message=Message(role=MessageRole.ASSISTANT, tool_calls=(
            ToolCall(id=f"escape-{self.requests}", name=name, arguments=args),)),
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2, cost_usd=0),
            model_name=self.config.model_name)


def child(output: Path, image_id: str, phase: str, run: Path | None) -> dict:
    models = []

    def factory(config):
        model = (FixModel(config) if phase == "baseline" else EscapeModel(config)
                 if phase == "escape" else JobModel(config, restore=phase == "resume"))
        models.append(model)
        return model

    runner = TraceFixRunner(factory)
    original = CheckpointStore.save
    paused = False

    def save(store, payload, **kwargs):
        nonlocal paused
        saved = original(store, payload, **kwargs)
        agent = payload.get("agent", {})
        consumed = agent.get("jobs", {}).get("consumed", {})
        history = agent.get("history", [])
        resumed_page = any(row.get("role") == "tool" and "G5-MIDDLE" in row.get("content", "")
                           and "read_job_output" in row.get("content", "") for row in history[-3:])
        if (not paused and phase in {"start", "resume"} and len(consumed) == 2
                and (phase == "start" or resumed_page)):
            paused = True
            raise KeyboardInterrupt("zero-provider replay paused after a committed job batch")
        return saved

    CheckpointStore.save = save
    try:
        if phase == "resume":
            assert run is not None
            inspected = runner.inspect(run)
            assert inspected["resumable"], inspected
            result = runner.resume(run)
        else:
            baseline = phase == "baseline"
            result = runner.run(RunConfig(repo=output / "source", output_dir=output / "runs",
                task="fix value and retain identity" if baseline else "manage long and short tests",
                model_name="offline/docker-background-runner", env_file=None,
                source_import="sample", execution_backend="docker", docker_profile="ordinary",
                docker_image_id=image_id, docker_recovery_enabled=True,
                harness_foundations_enabled=True, background_jobs_enabled=True,
                test_target="test_primary.py" if baseline else "test_long.py::test_fast",
                regression_targets=("test_existing.py",) if baseline else (),
                agent_config=AgentConfig(max_steps=3 if baseline else 40,
                                         max_test_runs=(3 if baseline else 1
                                                        if phase == "escape" else 2),
                                         wall_time_seconds=240)))
    finally:
        CheckpointStore.save = original
    if phase == "escape":
        root = Path(result.result_path).parent
        assert result.test_runs == 1 and result.error
        assert result.status.value in {"failed", "interrupted"}
        receipts = json.loads((root / "jobs/checkpoint.json").read_text(encoding="utf-8"))
        record = next(iter(receipts["payload"]["jobs"].values()))["record"]
        assert record["status"] == "outcome_unknown" and not record["tree_exit_confirmed"]
        assert not runner.inspect(root)["resumable"]
        with TestExecutionLedger(root, run_id=root.name, limit=1) as ledger:
            before = ledger.checkpoint_identity()
            assert ledger.charged == 1 and ledger.has_unknown
            try:
                ledger.reserve(call_id="must-not-replay", invocation_sha256="a" * 64)
            except JobError:
                pass
            else:
                raise AssertionError("unknown execution incorrectly refunded or replayed")
            assert ledger.checkpoint_identity() == before
        container_id = result.workspace_preparation["container_id"]
        removed = subprocess.run(["docker", "inspect", container_id], capture_output=True)
        assert removed.returncode != 0
        return {"provider_calls": 0, "test_runs": 1, "outcome_unknown": True,
                "whole_container_removed": True, "no_replay_or_refund": True}
    assert result.status.value == ("completed" if phase == "baseline" else "interrupted"), (
        result.status, result.stop_reason, result.error)
    assert result.test_runs == (3 if phase == "baseline" else 2), result.error
    if phase == "baseline":
        assert result.validation_gate_status == "passed", result.error
    return {"run": str(Path(result.result_path).parent), "status": result.status.value,
            "test_runs": result.test_runs, "simulated_requests": sum(m.requests for m in models),
            "provider_calls": 0}


def run_probe(output: Path, image_id: str) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.egg-info/\n",
                                        encoding="utf-8")
    (source / "sample.py").write_text("def value(): return 0\ndef identity(x): return x\n",
                                        encoding="utf-8")
    (source / "setup.py").write_text("from setuptools import setup\nsetup(name='g5_probe',"
        "version='0.0.0',py_modules=['sample'])\n", encoding="utf-8")
    (source / "test_primary.py").write_text("from sample import value\ndef test_value():\n"
        "    assert value()==1\n", encoding="utf-8")
    (source / "test_existing.py").write_text("from sample import identity\ndef test_identity():\n"
        "    assert identity(2)==2\n", encoding="utf-8")
    (source / "test_long.py").write_text("import os,subprocess,sys,time\ndef test_long():\n"
        "    subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        "    os.write(1,b'x'*50000+b'G5-MIDDLE'+b'y'*50000+b'\\n')\n"
        "    time.sleep(60)\ndef test_fast():\n    assert True\n", encoding="utf-8")
    (source / "test_escape.py").write_text("import subprocess,sys,time\ndef test_escape():\n"
        "    subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
        "start_new_session=True)\n"
        "    print('G5-ESCAPE-READY 1 passed',flush=True)\n    time.sleep(60)\n", encoding="utf-8")
    for args in [("init", "-q"), ("add", "."), ("-c", "user.name=TraceFix Probe", "-c",
                 "user.email=probe@example.invalid", "commit", "-qm", "fixture")]:
        subprocess.run(["git", *args], cwd=source, capture_output=True, check=True)

    def invoke(phase, run=None):
        command = [sys.executable, str(Path(__file__).resolve()), "--phase", phase,
                   "--output", str(output), "--image-id", image_id]
        if run:
            command.extend(["--run", run])
        completed = subprocess.run(command, capture_output=True, timeout=300, check=False)
        (output / f"{phase}.log").write_bytes(completed.stdout + completed.stderr)
        assert completed.returncode == 0, (completed.stdout + completed.stderr).decode(
            "utf-8", "replace")[-5000:]
        return json.loads(completed.stdout)

    started = invoke("start")
    resumed = invoke("resume", started["run"])
    baseline = invoke("baseline")
    escaped = invoke("escape")
    result = {"provider_calls": 0, "start": started, "resume": resumed, "baseline": baseline,
              "escaped_descendant": escaped}
    (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--phase", choices=("start", "resume", "baseline", "escape"))
    parser.add_argument("--run", type=Path)
    args = parser.parse_args()
    operation = (child(args.output.resolve(), args.image_id, args.phase, args.run)
                 if args.phase else run_probe(args.output.resolve(), args.image_id))
    print(json.dumps(operation, ensure_ascii=False))
