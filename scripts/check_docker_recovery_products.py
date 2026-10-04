"""Real Linux recovery of product deletion/modes/Skills, with identity fault injection."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
from pathlib import Path

from tracefix.docker_backend import DockerToolBackend
from tracefix.exceptions import WorkspaceError
from tracefix.memory import atomic_json
from tracefix.memory_replay import _PATCH
from tracefix.messages import ToolCall
from tracefix.reproduction import _git
from tracefix.runtime import TraceFixRunner


def check(output: Path, image_id: str) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    (source / "tests").mkdir()
    (source / "widget.py").write_text("def next_page(page):\n    return page\n", encoding="utf-8")
    (source / "old.txt").write_bytes(b"tracked deletion")
    owned_cache = source / "data/__pycache__"
    owned_cache.mkdir(parents=True)
    (owned_cache / "owned.py").write_text("user-owned product", encoding="utf-8")
    (source / "tests/test_widget.py").write_text(
        "from widget import next_page\ndef test_page(): assert next_page(1) == 2\n",
        encoding="utf-8")
    _git(source, "init")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "-m", "snapshot product baseline")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True,
    ).strip()
    commands = []
    containers = []

    def backend():
        return DockerToolBackend(task_id="tracefix-ordinary", input_root=output, run_dir=output,
                                 run_id=output.name, profile="ordinary", image_id=image_id)

    def execute(container, code):
        command = ["docker", "exec", container, "python", "-c", code]
        process = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        commands.append({"command": command, "exit_code": process.returncode,
                         "stdout": process.stdout, "stderr": process.stderr})
        atomic_json(output / "commands.json", commands)
        assert process.returncode == 0, commands[-1]
        return process.stdout

    def missing(container):
        return subprocess.run(["docker", "inspect", container], capture_output=True,
                              timeout=30, check=False).returncode != 0

    def test(tools, call_id):
        result = tools.get("run_tests").execute(ToolCall(
            id=call_id, name="run_tests", arguments={"command": "pytest -q tests/test_widget.py"}))
        atomic_json(output / f"{call_id}.json", result.model_dump(mode="json"))
        assert result.success and result.output["test_status"] == "passed"
        assert result.output["returncode"] == 0 and result.output["test_counts"]["tests"] == 1

    first = backend()
    restored = None
    try:
        tools = first.prepare(commit, source, output, skills_enabled=True, recovery_enabled=True,
                              source_import_probe="widget")
        containers.append(first.container_id)
        name = tools.skill_catalog[0].name
        assert tools.get("load_skill").execute(ToolCall(
            id="load", name="load_skill", arguments={"name": name})).success
        assert tools.get("apply_patch").execute(ToolCall(
            id="patch", name="apply_patch", arguments={"patch": _PATCH})).success
        test(tools, "before-recovery")
        execute(first.container_name, "from pathlib import Path; import os; "
                "Path('/work/agent/old.txt').unlink(); p=Path('/work/agent/new.py'); "
                "p.write_bytes(b'print(42)\\n'); os.chmod(p,0o755)")
        snapshot = first.save_snapshot(1)
        atomic_json(output / "snapshot.json", snapshot)
        first.close(remove=False)
        assert not missing(first.container_id)
        faults = []
        for field in ("tool_sha256", "interpreter"):
            bad = copy.deepcopy(snapshot)
            bad[field] = "invalid-identity"
            candidate = backend()
            try:
                candidate.prepare(commit, source, output, skills_enabled=True,
                                  recovery_enabled=True,
                                  source_import_probe="widget",
                                  baseline_archive=output / "agent-base.tar", recovery_snapshot=bad)
                raise AssertionError(f"{field} mismatch was accepted")
            except WorkspaceError as exc:
                assert "identity changed" in str(exc) or "definitions changed" in str(exc)
                faults.append({"field": field, "rejected": True, "reason": str(exc)})
            finally:
                if candidate.container_id:
                    containers.append(candidate.container_id)
                candidate.close(remove=True)
                assert candidate.container_id and missing(candidate.container_id)
        restored = backend()
        tools = restored.prepare(commit, source, output, skills_enabled=True, recovery_enabled=True,
                                 source_import_probe="widget",
                                 baseline_archive=output / "agent-base.tar",
                                 recovery_snapshot=snapshot)
        containers.append(restored.container_id)
        restored.remove_previous_container(snapshot)
        assert missing(first.container_id) and restored.container_id != first.container_id
        probe = json.loads(execute(restored.container_name,
            "from pathlib import Path; import stat,json; p=Path('/work/agent/new.py'); "
            "print(json.dumps({'mode':stat.S_IMODE(p.stat().st_mode),'content':p.read_text(),"
            "'deleted':not Path('/work/agent/old.txt').exists(),"
            "'owned_cache':Path('/work/agent/data/__pycache__/owned.py').read_text(),"
            "'test_tmp_empty':not list(Path('/work/agent/.tracefix-test-tmp').iterdir())}))"))
        assert probe == {"mode": 0o755, "content": "print(42)\n", "deleted": True,
                         "owned_cache": "user-owned product",
                         "test_tmp_empty": True}
        assert restored.session.call("__recovery_state__", {}).output["skills"] == (
            snapshot["bridge_state"]["skills"])
        test(tools, "after-recovery")
    finally:
        if restored:
            restored.close(remove=True)
        first.close(remove=True)
    assert len(set(containers)) == 4 and all(missing(container) for container in containers)
    summary = {"passed": True, "provider_calls": 0, "source_commit": commit,
               "image_id": image_id, "containers": containers, "all_containers_removed": True,
               "probe": probe, "identity_faults": faults,
               "implementation_sha256": TraceFixRunner._implementation_sha256(),
               "evidence_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in output.glob("*.json")}}
    atomic_json(output / "acceptance.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    arguments = parser.parse_args()
    print(json.dumps(check(arguments.output.resolve(), arguments.image_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
