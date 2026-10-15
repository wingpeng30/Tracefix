from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tracefix import AgentConfig, AgentStatus, BaseLLM, RunConfig, TraceFixRunner
from tracefix.checkpoint import CheckpointError
from tracefix.jobs.test_budget import TestExecutionLedger
from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.models.base import LLMResponse, TokenUsage
from tracefix.security import minimal_subprocess_environment


def repository(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    ready = tmp_path / "test-started"
    (repo / "test_wait.py").write_text(
        "import time\nfrom pathlib import Path\ndef test_wait():\n"
        f"    Path({str(ready)!r}).touch()\n"
        "    print('started',flush=True)\n    time.sleep(60)\n", encoding="utf-8")
    for args in [("init", "-q"), ("add", "."),
                 ("-c", "user.name=TraceFix Test", "-c", "user.email=test@example.invalid",
                  "commit", "-qm", "fixture")]:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo, ready


class PauseModel(BaseLLM):
    def __init__(self, config, ready):
        super().__init__(config)
        self.ready, self.requests = ready, 0

    def complete(self, messages, tools=()):
        self.requests += 1
        if self.requests == 1:
            calls = (ToolCall(id="start", name="start_test_job",
                              arguments={"command": "pytest -q -s"}),)
        else:
            until = time.monotonic() + 10
            while not self.ready.exists() and time.monotonic() < until:
                time.sleep(0.02)
            assert self.ready.exists()
            ticket = next(json.loads(message.content) for message in messages
                          if message.role is MessageRole.TOOL
                          and json.loads(message.content).get("tool_name") == "start_test_job")
            calls = (ToolCall(id="status", name="get_job_status", arguments={
                "job_id": ticket["output"]["job"]["identity"]["job_id"]}),)
        return LLMResponse(message=Message(role=MessageRole.ASSISTANT, tool_calls=calls),
                           usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2,
                                            cost_usd=0), model_name=self.config.model_name)


@pytest.mark.skipif(os.name != "nt", reason="Windows native Runner background lifecycle")
@pytest.mark.parametrize("mcp_catalog", [False, True])
def test_runner_preserves_safe_checkpoint_while_active_then_cancels_and_commits(
    tmp_path, monkeypatch, mcp_catalog,
):
    if mcp_catalog:
        from tracefix.mcp_serena import SerenaMCP

        # This case checks catalog/restore identity, not real MCP transport.
        monkeypatch.setattr(SerenaMCP, "preflight", lambda self: None)
        monkeypatch.setattr(SerenaMCP, "preflight_image", staticmethod(lambda image: None))
    repo, ready = repository(tmp_path)
    models = []

    def factory(config):
        model = PauseModel(config, ready)
        models.append(model)
        return model

    result = TraceFixRunner(factory).run(RunConfig(
        repo=repo, task="start test and pause safely", execution_backend="local",
        model_name="offline/job-runner", output_dir=tmp_path / "runs", env_file=None,
        harness_foundations_enabled=True, background_jobs_enabled=True,
        mcp_serena_image_id="sha256:" + "b" * 64 if mcp_catalog else None,
        test_python_executable=Path(sys.executable),
        agent_config=AgentConfig(max_steps=2, max_test_runs=2)))
    assert result.status is AgentStatus.INTERRUPTED, result.error
    assert result.test_runs == 1 and models[0].requests == 2
    run = Path(result.result_path).parent
    checkpoint = json.loads((run / "checkpoint.json").read_text(encoding="utf-8"))
    progress = json.loads((run / "job-progress" / "checkpoint.json").read_text(encoding="utf-8"))
    assert progress["payload"]["resumable"] is False
    assert progress["payload"]["last_safe_sequence"] < checkpoint["sequence"]
    consumed = checkpoint["payload"]["agent"]["jobs"]["consumed"]
    assert len(consumed) == 1 and checkpoint["payload"]["job_state"]["test_executions"]
    assert checkpoint["payload"]["artifact_store"]
    receipts = json.loads((run / "jobs" / "checkpoint.json").read_text(encoding="utf-8"))
    job = next(iter(receipts["payload"]["jobs"].values()))["record"]
    assert job["status"] == "cancelled" and job["tree_exit_confirmed"]
    assert job["process_started"] is True
    inspected = TraceFixRunner.inspect(run)
    assert inspected["resumable"], inspected
    resumed = TraceFixRunner(factory).resume(run)
    assert resumed.status is AgentStatus.INTERRUPTED and resumed.test_runs == 1
    assert len(models) == 2 and models[-1].requests == 0
    after = json.loads((run / "checkpoint.json").read_text(encoding="utf-8"))
    assert after["payload"]["job_state"] == checkpoint["payload"]["job_state"]
    assert after["payload"]["agent"]["jobs"]["consumed"] == consumed
    script = '''
import json,sys
from pathlib import Path
from tracefix import BaseLLM,TraceFixRunner
if sys.argv[2]=='metadata-only-mcp':
    from tracefix.mcp_serena import SerenaMCP
    SerenaMCP.preflight=lambda self:None
    SerenaMCP.preflight_image=staticmethod(lambda image:None)
class NoCalls(BaseLLM):
    calls=0
    def complete(self,messages,tools=()):
        self.calls+=1
        raise AssertionError('resume may not replay a completed or cancelled job')
models=[]
def factory(config):
    model=NoCalls(config)
    models.append(model)
    return model
result=TraceFixRunner(factory).resume(Path(sys.argv[1]))
print(json.dumps({'status':result.status.value,'test_runs':result.test_runs,
                  'requests':sum(model.calls for model in models)}))
'''
    environment = minimal_subprocess_environment()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    fresh = subprocess.run([sys.executable, "-c", script, str(run),
                            "metadata-only-mcp" if mcp_catalog else "none"], cwd=tmp_path,
                           env=environment, capture_output=True, text=True, timeout=30)
    assert fresh.returncode == 0, fresh.stderr
    assert json.loads(fresh.stdout) == {"status": "interrupted", "test_runs": 1, "requests": 0}
    # A newer, unacknowledged launch must invalidate even this otherwise safe checkpoint.
    with TestExecutionLedger(run, run_id=result.run_id, limit=2) as ledger:
        ticket = ledger.reserve(call_id="unacknowledged-start", invocation_sha256="a" * 64)
        ledger.before_dispatch(ticket.execution_id)
    inspected = TraceFixRunner.inspect(run)
    assert not inspected["resumable"] and any("pending or unknown" in reason
                                              for reason in inspected["reasons"])
    factories_before = len(models)
    with pytest.raises(CheckpointError, match="cannot be resumed"):
        TraceFixRunner(factory).resume(run)
    assert len(models) == factories_before


@pytest.mark.skipif(os.name != "nt", reason="Windows native shared baseline/final test accounting")
def test_baseline_and_final_gate_share_one_managed_execution_ledger(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (repo / "sample.py").write_text(
        "def value(): return 0\ndef identity(x): return x\n", encoding="utf-8")
    (repo / "test_primary.py").write_text(
        "from sample import value\ndef test_value():\n    assert value() == 1\n", encoding="utf-8")
    (repo / "test_existing.py").write_text(
        "from sample import identity\ndef test_identity():\n    assert identity(2) == 2\n",
        encoding="utf-8")
    for args in [("init", "-q"), ("add", "."),
                 ("-c", "user.name=TraceFix Test", "-c", "user.email=test@example.invalid",
                  "commit", "-qm", "fixture")]:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    class FixModel(BaseLLM):
        requests = 0

        def complete(self, messages, tools=()):
            self.requests += 1
            calls = ()
            if self.requests == 1:
                calls = (ToolCall(id="patch", name="apply_patch", arguments={"patch":
                    "diff --git a/sample.py b/sample.py\n--- a/sample.py\n+++ b/sample.py\n"
                    "@@ -1,2 +1,2 @@\n-def value(): return 0\n+def value(): return 1\n"
                    " def identity(x): return x\n"}),)
            return LLMResponse(message=Message(role=MessageRole.ASSISTANT, tool_calls=calls,
                                               content=None if calls else "done"),
                               usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2,
                                                cost_usd=0), model_name=self.config.model_name)

    result = TraceFixRunner(FixModel).run(RunConfig(
        repo=repo, task="fix value and preserve identity", execution_backend="local",
        model_name="offline/jobs-final-validation", output_dir=tmp_path / "runs", env_file=None,
        harness_foundations_enabled=True, background_jobs_enabled=True,
        test_python_executable=Path(sys.executable), test_target="test_primary.py",
        source_import="sample", regression_targets=("test_existing.py",),
        agent_config=AgentConfig(max_steps=3, max_test_runs=3)))
    assert result.status is AgentStatus.COMPLETED, result.error
    assert result.validation_gate_status == "passed" and result.test_runs == 3
    run = Path(result.result_path).parent
    ledger = json.loads((run / "test-executions" / "checkpoint.json").read_text(encoding="utf-8"))
    rows = ledger["payload"]["executions"]
    assert ledger["payload"]["baseline_runs"] == 0
    assert len(rows) == len({row["execution_id"] for row in rows}) == 3
    assert all(row["phase"] == "settled" and row["process_started"]
               and row["tree_exit_confirmed"] for row in rows)
    artifacts = json.loads((run / "tool-artifacts" / "manifest.json").read_text(encoding="utf-8"))
    assert len(artifacts["artifacts"]) == 3
