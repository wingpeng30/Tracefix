from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tracefix import AgentConfig, AgentError, BaseLLM, LLMConfig, MinimalAgent
from tracefix.artifacts import ArtifactStore
from tracefix.jobs.domain import JobError, JobStatus
from tracefix.jobs.local_runtime import LocalJobRuntime
from tracefix.jobs.ports import JobSourceState
from tracefix.messages import ToolCall
from tracefix.security import minimal_subprocess_environment
from tracefix.tools.base import ToolRegistry
from tracefix.tools.builtin import RunTestsTool

_WORKER = '''
import json,sys,time
from datetime import UTC,datetime,timedelta
from pathlib import Path
from tracefix.artifacts import ArtifactStore
from tracefix.jobs.local_runtime import LocalJobRuntime
from tracefix.jobs.ports import JobSourceState
from tracefix.messages import ToolCall
from tracefix.tools.base import ToolRegistry
from tracefix.tools.builtin import RunTestsTool
root=Path(sys.argv[1])
store=ArtifactStore(root/'artifacts',run_id='crash-run',streaming_enabled=True)
tool=RunTestsTool(root/'workspace',evidence_dir=root/'audit',artifact_store=store)
runtime=LocalJobRuntime(
    tools=ToolRegistry([tool]),artifacts=store,run_dir=root,run_id='crash-run',
    checkout_id='a'*64,limit=2,deadline=datetime.now(UTC)+timedelta(seconds=60),
    profile='workspace_write',source_identity=lambda:JobSourceState(
        product_diff_sha256=tool._product_diff_sha(),nonempty_diff=False))
job=runtime.service.start(ToolCall(id='crash-start',name='run_tests',
                                 arguments={'command':'pytest -q -s'}))
until=time.monotonic()+20
while time.monotonic()<until:
    record=runtime.service.get(job.identity.job_id)
    page=runtime.service.read(job.identity.job_id)
    if (record.process_started is True and (root/'pids.json').exists()
            and 'CANARY-passed' in page['content']):
        (root/'ready.json').write_text(record.model_dump_json(),encoding='utf-8')
        break
    time.sleep(0.1)
else:
    raise RuntimeError('worker did not commit the canary output and start receipt')
# Deliberately omit graceful shutdown; the parent will terminate this host process.
time.sleep(120)
'''


@pytest.mark.skipif(os.name != "nt", reason="real Windows Job Object host-crash contract")
def test_hard_host_crash_kills_owned_processes_but_does_not_resolve_unknown_execution(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pids_path = tmp_path / "pids.json"
    (workspace / "test_canary.py").write_text(
        "import json,os,subprocess,sys,time\nfrom pathlib import Path\n"
        "def test_canary():\n"
        "    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],"
        "creationflags=512)\n"
        f"    Path({str(pids_path)!r}).write_text(json.dumps([os.getpid(),child.pid]))\n"
        "    print('CANARY-passed',flush=True)\n    time.sleep(120)\n", encoding="utf-8")
    for args in [("init", "-q"), ("add", "."),
                 ("-c", "user.name=TraceFix Test", "-c", "user.email=test@example.invalid",
                  "commit", "-qm", "canary")]:
        subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)
    environment = minimal_subprocess_environment()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    worker = subprocess.Popen([sys.executable, "-c", _WORKER, str(tmp_path)],
                              cwd=tmp_path, env=environment,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.WaitForSingleObject.restype = ctypes.c_uint32
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handles = []
    try:
        until = time.monotonic() + 25
        while not (tmp_path / "ready.json").exists() and time.monotonic() < until:
            if worker.poll() is not None:
                pytest.fail(f"canary worker exited early: {worker.communicate(timeout=2)!r}")
            time.sleep(0.03)
        assert (tmp_path / "ready.json").exists()
        record = json.loads((tmp_path / "ready.json").read_text(encoding="utf-8"))
        for pid in json.loads(pids_path.read_text(encoding="utf-8")):
            handle = kernel.OpenProcess(0x100001, False, pid)
            assert handle
            handles.append(handle)
        worker.kill()
        worker.communicate(timeout=5)
        # Waiting on the original kernel handles cannot mistake a recycled PID for our process.
        assert all(kernel.WaitForSingleObject(handle, 5000) == 0 for handle in handles)

        store = ArtifactStore(tmp_path / "artifacts", run_id="crash-run", streaming_enabled=True)
        tool = RunTestsTool(workspace, evidence_dir=tmp_path / "audit", artifact_store=store)
        runtime = LocalJobRuntime(
            tools=ToolRegistry([tool]), artifacts=store, run_dir=tmp_path, run_id="crash-run",
            checkout_id="a" * 64, limit=2, deadline=datetime.now(UTC) + timedelta(seconds=20),
            profile="workspace_write", source_identity=lambda: JobSourceState(
                product_diff_sha256=tool._product_diff_sha(), nonempty_diff=False))

        class NoRequests(BaseLLM):
            calls = 0

            def complete(self, messages, tools=()):
                self.calls += 1
                raise AssertionError("unknown job must prevent even a first model request")

        try:
            job_id = record["identity"]["job_id"]
            recovered = runtime.service.get(job_id)
            assert recovered.status == JobStatus.OUTCOME_UNKNOWN
            assert recovered.process_started is True and not recovered.tree_exit_confirmed
            assert runtime.ledger.charged == 1 and runtime.ledger.held == 0
            assert "CANARY-passed" in runtime.service.read(job_id)["content"]
            assert runtime.service.result(job_id) is None
            with pytest.raises(JobError, match="stopped_or_unknown"):
                runtime.service.start(ToolCall(id="retry", name="run_tests", arguments={}))
            model = NoRequests(LLMConfig(model_name="zero-provider-crash-recovery"))
            agent = MinimalAgent(model, runtime.tools, config=AgentConfig(max_steps=1))
            agent.job_session, agent.tool_dispatcher = runtime.session, runtime.dispatcher
            with pytest.raises(AgentError, match="outcome unknown"):
                agent.run("inspect unknown job without replay")
            assert model.calls == 0 and agent.state.test_runs == 1
        finally:
            assert not runtime.close()
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=5)
        for handle in handles:
            if kernel.WaitForSingleObject(handle, 0) != 0:
                # Test cleanup only. A surviving process would already fail the contract above.
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 5000)
            kernel.CloseHandle(handle)
