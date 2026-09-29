"""Exercise real ordinary Docker timeout, bridge disconnect, and preparation cleanup."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from tracefix.docker_backend import DockerToolBackend
from tracefix.exceptions import WorkspaceError
from tracefix.messages import ToolCall


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments], cwd=repo, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _backend(root: Path, image_id: str, name: str) -> DockerToolBackend:
    run_dir = root / name
    run_dir.mkdir()
    return DockerToolBackend(
        task_id="tracefix-ordinary", input_root=run_dir, run_dir=run_dir,
        run_id=name, profile="ordinary", image_id=image_id, timeout_seconds=15,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    repo = root / "source"
    repo.mkdir()
    (repo / "widget.py").write_text("value = 1\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(5)\n", encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "--all")
    _git(repo, "-c", "user.name=FailureProbe", "-c",
         "user.email=probe@invalid.local", "commit", "-qm", "base")
    commit = _git(repo, "rev-parse", "HEAD")
    tracefix_root = Path(__file__).resolve().parents[1]
    outcome: dict[str, object] = {"image_id": args.image_id, "source_commit": commit}

    backend = _backend(root, args.image_id, "ordinary-timeout-disconnect")
    try:
        registry = backend.prepare(commit, repo, tracefix_root, source_import_probe="widget")
        timeout = registry.get("run_tests").execute(ToolCall(
            id="timeout-test", name="run_tests",
            arguments={"command": "pytest -q tests/test_slow.py", "timeout_seconds": 1},
        ))
        assert not timeout.success and timeout.output.get("test_status") == "timed_out"
        outcome["timeout"] = timeout.model_dump(mode="json")
        assert backend.session is not None
        backend.session.close(force=True)
        try:
            registry.get("read_file").execute(ToolCall(
                id="disconnected-read", name="read_file", arguments={"path": "widget.py"},
            ))
        except WorkspaceError as exc:
            outcome["bridge_disconnect"] = {"blocked": True, "error": str(exc)}
        else:
            raise AssertionError("closed bridge unexpectedly accepted another tool call")
    finally:
        backend.close(remove=True)

    backend = _backend(root, args.image_id, "ordinary-import-error")
    try:
        try:
            backend.prepare(commit, repo, tracefix_root,
                            source_import_probe="missing_project_dependency")
        except WorkspaceError as exc:
            outcome["missing_dependency"] = {"blocked": True, "error": str(exc)}
        else:
            raise AssertionError("missing import unexpectedly passed preparation")
    finally:
        backend.close(remove=True)

    remaining = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=tracefix-ordinary-",
         "--format", "{{.ID}}"], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert not remaining, remaining
    outcome["containers_cleaned"] = True
    (root / "failure-audit.json").write_text(
        json.dumps(outcome, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    print(json.dumps({"passed": True, "audit": str(root / "failure-audit.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
