"""Real ordinary-container job RPC probe; no model client, provider request or network."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from tracefix.docker_backend import DockerToolBackend
from tracefix.exceptions import TraceFixError
from tracefix.jobs.artifact_reader import RemoteJobArtifactReader
from tracefix.jobs.domain import TERMINAL_STATUSES, JobStatus
from tracefix.jobs.protocol import JobWorkerSettings
from tracefix.jobs.remote import RemoteJobService
from tracefix.jobs.test_budget import TestExecutionLedger
from tracefix.messages import ToolCall


def probe(output: Path, image_id: str) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n*.egg-info/\n",
                                        encoding="utf-8")
    (source / "sample.py").write_text("VALUE=1\n", encoding="utf-8")
    (source / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='tracefix_job_probe',version='0.0.0',"
        "py_modules=['sample'])\n", encoding="utf-8")
    (source / "test_long.py").write_text(
        "import os,subprocess,sys,time\n"
        "def test_long():\n"
        "    subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        "    os.write(1,b'x'*50000+b'G5-MIDDLE'+b'y'*50000+b'\\n')\n"
        "    os.write(2,b'password=fixture-only\\n')\n"
        "    time.sleep(60)\n", encoding="utf-8")
    for args in [("init", "-q"), ("add", "."),
                 ("-c", "user.name=TraceFix Probe", "-c", "user.email=probe@example.invalid",
                  "commit", "-qm", "fixture")]:
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source).decode().strip()
    run = output / "run"
    run.mkdir()
    run_id = "g56-bridge-" + uuid4().hex[:12]
    backend = DockerToolBackend(task_id="tracefix-ordinary", input_root=output, run_dir=run,
                                 run_id=run_id, profile="ordinary", image_id=image_id,
                                 timeout_seconds=20)
    with TestExecutionLedger(run, run_id=run_id, limit=1) as ledger:
        settings = JobWorkerSettings(
            logical_run_id=run_id, worker_epoch=uuid4().hex, host_owner_epoch=ledger.owner_epoch,
            session_deadline=datetime.now(UTC) + timedelta(seconds=120))
        try:
            backend.prepare(commit, source, Path(__file__).resolve().parents[1],
                            source_import_probe="sample", artifacts_enabled=True,
                            recovery_enabled=True, job_settings=settings)
            session = backend.session
            assert session is not None and session.job_protocol == backend.job_protocol
            call = ToolCall(id="probe-test", name="run_tests",
                            arguments={"command": "pytest -q -s test_long.py",
                                       "timeout_seconds": 20})
            def prepare(target):
                prepared = session.call("__prepare__", target.model_dump(mode="json"))
                assert prepared.success, prepared.error
                return target.arguments["timeout_seconds"]

            def unavailable_history(*args, **kwargs):
                raise AssertionError("fresh worker must not use restored artifact reader")

            jobs = RemoteJobService(run / "jobs", settings=settings,
                checkout_id=hashlib.sha256(b"/work/agent").hexdigest(), ledger=ledger,
                channel=session, prepare=prepare, authorize=lambda target: None,
                historical_read=unavailable_history,
                abort_executor=lambda: backend.close(remove=True))
            started = jobs.start(call)
            job_id = started.identity.job_id
            content, offset = "", 0
            for _ in range(50):
                page = jobs.read(job_id, offset=offset, limit=4096)
                content += page["content"]
                offset = page["next_offset"]
                if "G5-MIDDLE" in content:
                    break
                time.sleep(0.05)
            assert "G5-MIDDLE" in content
            assert jobs.get(job_id).process_started is True
            jobs.cancel(job_id)
            until = time.monotonic() + 10
            while time.monotonic() < until:
                status = jobs.get(job_id)
                if status.status in TERMINAL_STATUSES:
                    break
                time.sleep(0.03)
            assert status.status == JobStatus.CANCELLED
            assert status.tree_exit_confirmed
            historical = jobs.read(job_id, stream="stderr")
            assert historical["final"] and "fixture-only" not in historical["content"]
            denied = session.call("run_tests", {"command": "pytest -q test_long.py"})
            assert not denied.success and "host execution ticket" in denied.error
            assert jobs.shutdown()
            checkpoint_identity = jobs.checkpoint_identity()
            assert ledger.charged == 1 and ledger.held == 0
            evidence_probe = subprocess.run(
                [backend.docker, "exec", backend.container_id, backend.python, "-I", "-S", "-c",
                 "import json,os; print(json.dumps({'evidence_exists':"
                 "os.path.isdir('/work/evidence'),'files':os.listdir('/work')}))"],
                capture_output=True, check=True)
            (output / "evidence-probe.json").write_bytes(evidence_probe.stdout)
            backend.export_evidence()
            subprocess.run([backend.docker, "exec", backend.container_id, backend.python,
                "-I", "-S", "-c", "from pathlib import Path; "
                "Path('/tmp/audit-canary').write_text('G5-OUTSIDE-CANARY'); "
                "Path('/work/evidence/escape').symlink_to('/tmp/audit-canary')"],
                capture_output=True, check=True)
            link_refused = False
            try:
                backend.export_evidence()
            except TraceFixError:
                link_refused = True
            finally:
                subprocess.run([backend.docker, "exec", backend.container_id, backend.python,
                    "-I", "-S", "-c", "from pathlib import Path; "
                    "p=Path('/work/evidence/escape'); assert p.is_symlink(); p.unlink()"],
                    capture_output=True, check=True)
            assert link_refused and not (run / "test-evidence/escape").exists()
            snapshot = backend.save_snapshot(1)
            result = {"provider_calls": 0, "ordinary_rpc": True, "incremental_middle": True,
                      "cancelled": True, "tree_exit_confirmed": True, "test_runs": ledger.charged,
                      "raw_run_tests_refused": True, "image_id": backend.image_id,
                      "host_checkpoint_identity": checkpoint_identity,
                      "audit_link_refused": link_refused,
                      "job_protocol": backend.job_protocol}
            plan = {"run_id": run_id, "job_id": job_id, "commit": commit,
                    "snapshot": snapshot, "job_state": checkpoint_identity,
                    "test_state": ledger.checkpoint_identity(), "image_id": backend.image_id}
            (output / "restore-plan.json").write_text(json.dumps(plan), encoding="utf-8")
            backend.close(remove=True)
            ledger.close()
            restored = subprocess.run([
                sys.executable, str(Path(__file__).resolve()), "--resume-probe",
                "--output", str(output), "--image-id", image_id,
            ], capture_output=True, timeout=180, check=False)
            (output / "restore-process.log").write_bytes(restored.stdout + restored.stderr)
            assert restored.returncode == 0, restored.stderr.decode("utf-8", "replace")[-3000:]
            result["new_process_container_restore"] = json.loads(restored.stdout)
            (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            return result
        finally:
            backend.close(remove=True)


def resume_probe(output: Path, image_id: str) -> dict:
    plan = json.loads((output / "restore-plan.json").read_text(encoding="utf-8"))
    assert plan["image_id"] == image_id
    run, run_id = output / "run", plan["run_id"]
    backend = DockerToolBackend(task_id="tracefix-ordinary", input_root=output, run_dir=run,
        run_id=run_id, profile="ordinary", image_id=image_id, timeout_seconds=20)
    with TestExecutionLedger(run, run_id=run_id, limit=1) as ledger:
        assert ledger.checkpoint_identity() == plan["test_state"]
        settings = JobWorkerSettings(logical_run_id=run_id, worker_epoch=uuid4().hex,
            host_owner_epoch=ledger.owner_epoch,
            session_deadline=datetime.now(UTC) + timedelta(seconds=120))
        try:
            backend.prepare(plan["commit"], output / "source", Path(__file__).resolve().parents[1],
                source_import_probe="sample", artifacts_enabled=True, recovery_enabled=True,
                job_settings=settings, baseline_archive=run / "agent-base.tar",
                recovery_snapshot=plan["snapshot"])
            session = backend.session
            holder = {}
            reader = RemoteJobArtifactReader(session, run_id=run_id,
                result_provider=lambda job_id: holder["service"].result(job_id))

            def never_prepare(call):
                raise AssertionError("restoring old output must never prepare another test")

            service = RemoteJobService(run / "jobs", settings=settings,
                checkout_id=hashlib.sha256(b"/work/agent").hexdigest(), ledger=ledger,
                channel=session, prepare=never_prepare, authorize=lambda call: None,
                historical_read=reader.read, abort_executor=lambda: backend.close(remove=True))
            holder["service"] = service
            assert service.checkpoint_identity() == plan["job_state"]
            assert service.get(plan["job_id"]).status == JobStatus.CANCELLED
            assert "G5-MIDDLE" in service.read(plan["job_id"], offset=49000)["content"]
            assert ledger.charged == 1 and ledger.held == 0
            assert settings.worker_epoch != plan["snapshot"]["job_protocol"]["worker_epoch"]
            assert service.shutdown()
            new_snapshot = backend.save_snapshot(2)
            previous_quota = plan["snapshot"]["artifact_receipt"]
            current_quota = new_snapshot["artifact_receipt"]
            assert current_quota["queries"] == previous_quota["queries"] + 1
            assert current_quota["returned_chars"] == previous_quota["returned_chars"] + 4096
            assert current_quota["artifact_ids"] == previous_quota["artifact_ids"]
            return {"provider_calls": 0, "old_job_not_replayed": True, "test_runs": 1,
                    "old_middle_retrieved": True, "query_quota_preserved": True}
        finally:
            backend.close(remove=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--resume-probe", action="store_true")
    args = parser.parse_args()
    try:
        operation = resume_probe if args.resume_probe else probe
        print(json.dumps(operation(args.output.resolve(), args.image_id), ensure_ascii=False))
    except TraceFixError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False))
        raise
