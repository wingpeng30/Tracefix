"""Zero-provider rules replay across separate host processes and optional Docker.

Use a new output directory. Task preparation never installs dependencies; Docker
mode requires an already prepared immutable ordinary image. Optional Serena uses
its separately pinned image and the existing read-only snapshot provider.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from tracefix import AgentConfig, RunConfig, TraceFixRunner
from tracefix.checkpoint import CheckpointStore
from tracefix.models.litellm_adapter import LiteLLMAdapter

PATCH = (
    "diff --git a/widget.py b/widget.py\n"
    "--- a/widget.py\n"
    "+++ b/widget.py\n"
    "@@ -1,5 +1,5 @@\n"
    " def next_page(page):\n"
    "-    return page\n"
    "+    return page + 1\n"
    " \n"
    " # needle first\n"
    " # needle middle\n"
)


class RulesReplayClient:
    """Select only known committed results; never retry an unknown request."""

    def __init__(self, *, mcp: bool = False) -> None:
        self.calls, self.mcp = 0, mcp

    def token_counter(self, **_kwargs):
        return 100

    def completion_cost(self, **_kwargs):
        return 0.0

    def completion(self, **kwargs):
        self.calls += 1
        messages = kwargs["messages"]
        calls, results = {}, []
        for message in messages:
            for call in message.get("tool_calls") or []:
                calls[call["id"]] = (call["function"]["name"],
                                      json.loads(call["function"]["arguments"]))
            if message.get("role") == "tool":
                results.append((calls[message["tool_call_id"]], json.loads(message["content"])))
        rules_pending = bool(results and results[-1][1].get("metadata", {}).get(
            "reason_code") == "rules_pending")
        if results and not results[-1][1]["success"] and not rules_pending:
            raise AssertionError("unexpected failure: " + json.dumps(results[-1][1]))
        if rules_pending:
            action = results[-1][0]
        else:
            success = [(call, result) for call, result in results if result["success"]]
            searches = [(call, result) for call, result in success if call[0] == "search_code"]
            if not searches:
                action = ("search_code", {"query": "needle", "path": "widget.py", "max_results": 1})
            elif searches[-1][1]["output"].get("continuation"):
                continuation = searches[-1][1]["output"]["continuation"]
                action = (continuation["tool_name"], continuation["arguments"])
            else:
                actions = [("read_file", {"path": "widget.py"})]
                if self.mcp:
                    actions.append(("mcp_serena_find_symbol", {
                        "name_path_pattern": "next_page", "relative_path": "widget.py",
                        "include_body": True, "depth": 0}))
                actions.append(("apply_patch", {"patch": PATCH}))
                if self.mcp:
                    actions.append(("mcp_serena_find_symbol", {
                        "name_path_pattern": "next_page", "relative_path": "widget.py",
                        "include_body": True, "depth": 1}))
                actions.append(("run_tests", {"command": "pytest tests/test_widget.py -q"}))
                completed = [call for call, _ in success]
                action = next((call for call in actions if call not in completed), None)
        tool_calls = ([] if action is None else [{"id": uuid4().hex, "type": "function",
            "function": {"name": action[0], "arguments": json.dumps(action[1])}}])
        return {"model": "offline/replay", "usage": {"prompt_tokens": 100,
            "completion_tokens": 10, "total_tokens": 110}, "choices": [{"message": {
                "role": "assistant", "content": None if action else "Verified under scoped rules",
                "tool_calls": tool_calls}, "finish_reason": "tool_calls" if action else "stop"}]}


def source_fixture(output: Path) -> Path:
    root = output / "source"
    root.mkdir()
    (root / "tests").mkdir()
    (root / ".tracefix/rules").mkdir(parents=True)
    for name, content in {
        ".gitignore": "__pycache__/\n.pytest_cache/\n",
        "AGENTS.md": "保留两处 needle 注释🙂，修复后执行声明测试。\n",
        "tests/AGENTS.md": "保留测试断言，不能靠删除测试通过。\n",
        ".tracefix/rules.toml": 'schema_version = 1\n[[rules]]\npaths = ["widget.py"]\n'
                                'file = "pagination.md"\n',
        ".tracefix/rules/pagination.md": "next_page 返回下一页，保留原函数签名。\n",
        "widget.py": "def next_page(page):\n    return page\n\n# needle first\n# needle middle\n",
        "tests/test_widget.py": "from widget import next_page\n"
                                "def test_page(): assert next_page(1) == 2\n",
    }.items():
        (root / name).write_text(content, encoding="utf-8")
    for arguments in [("init",), ("add", "."), ("-c", "user.name=Replay", "-c",
                     "user.email=replay@example.invalid", "commit", "-m", "fixture")]:
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    return root


def child(args) -> dict:
    client = RulesReplayClient(mcp=args.mcp_image_id is not None)
    runner = TraceFixRunner(lambda config: LiteLLMAdapter(config, client=client))
    if args.phase == "start":
        save = CheckpointStore.save

        def pause(store, payload, **kwargs):
            saved = save(store, payload, **kwargs)
            rules = payload["agent"]["repository_rules"]["payload"]
            cursors = rules["search"]["payload"]["cursors"]
            if any(state["line_offset"] > 0 for state in cursors.values()):
                raise KeyboardInterrupt
            return saved

        CheckpointStore.save = pause
        try:
            result = runner.run(RunConfig(repo=args.output / "source",
                task="Fix next_page; 保留注释",
                output_dir=args.output / "runs", env_file=None, model_name="offline/replay",
                execution_backend=args.backend, repository_rules_enabled=True,
                docker_recovery_enabled=args.backend == "docker", harness_foundations_enabled=True,
                conversation_enabled=True, docker_image_id=args.image_id,
                mcp_serena_image_id=args.mcp_image_id, test_target="tests/test_widget.py",
                source_import="widget", test_python_executable=(Path(sys.executable)
                    if args.backend == "local" else None), agent_config=AgentConfig(
                        max_steps=32, max_test_runs=3, wall_time_seconds=600)))
        finally:
            CheckpointStore.save = save
    else:
        inspection = runner.inspect(args.run_root)
        if not inspection["resumable"]:
            raise AssertionError(inspection["reasons"])
        result = runner.resume(args.run_root)
    return {"status": result.status.value, "test_runs": result.test_runs,
            "step_count": result.step_count, "result_path": result.result_path,
            "error": result.error, "provider_calls": 0, "scripted_requests": client.calls,
            "host_pid": os.getpid()}


def invoke(args, phase: str, root: Path | None = None) -> dict:
    command = [sys.executable, str(Path(__file__).resolve()), "--phase", phase,
               "--output", str(args.output), "--backend", args.backend]
    for name, value in (("--image-id", args.image_id), ("--mcp-image-id", args.mcp_image_id),
                        ("--run-root", root)):
        if value is not None:
            command.extend((name, str(value)))
    process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=660)
    (args.output / f"{phase}-stdout.log").write_text(process.stdout, encoding="utf-8")
    (args.output / f"{phase}-stderr.log").write_text(process.stderr, encoding="utf-8")
    if process.returncode:
        raise AssertionError(f"{phase} process failed: {process.stderr[-4000:]}")
    return json.loads(process.stdout.strip().splitlines()[-1])


def parent(args) -> dict:
    if args.output.exists():
        raise ValueError("output must be new")
    args.output.mkdir(parents=True)
    source_fixture(args.output)
    first = invoke(args, "start")
    if first["status"] != "interrupted":
        raise AssertionError(first)
    root = Path(first["result_path"]).parent
    checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))["payload"]
    before = checkpoint["agent"]["repository_rules"]["payload"]
    old_cursors = set(before["search"]["payload"]["cursors"])
    old_container = checkpoint.get("docker_snapshot", {}).get("container_id")
    if old_container:
        # Exact committed owned ID, never a name pattern or another run's container.
        removal = subprocess.run(["docker", "rm", "-f", old_container], capture_output=True,
                                  text=True, timeout=30)
        if removal.returncode and not any(marker in removal.stderr.lower()
                                           for marker in ("no such container", "no such object")):
            raise AssertionError("owned container removal was not confirmed")
    second = invoke(args, "resume", root)
    if second["status"] != "completed" or second["test_runs"] != 1 or second["error"] is not None:
        raise AssertionError(second)
    if first["host_pid"] == second["host_pid"]:
        raise AssertionError("replay did not use distinct host processes")
    final = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))["payload"]
    current = final["agent"]["repository_rules"]["payload"]
    if not old_cursors <= set(current["search"]["payload"]["cursors"]):
        raise AssertionError("recovery reset search state")
    if old_container == final.get("docker_snapshot", {}).get("container_id") and old_container:
        raise AssertionError("Docker recovery reused the previous task container")
    report = {"backend": args.backend, "image_id": args.image_id,
              "mcp_image_id": args.mcp_image_id, "first": first, "resumed": second,
              "old_cursor_count": len(old_cursors), "final_cursor_count": len(
                  current["search"]["payload"]["cursors"]), "provider_calls": 0,
              "rule_message_count": len(current["delivery"]["payload"]["messages"])}
    (args.output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=("local", "docker"), default="local")
    parser.add_argument("--image-id")
    parser.add_argument("--mcp-image-id")
    parser.add_argument("--phase", choices=("parent", "start", "resume"), default="parent")
    parser.add_argument("--run-root", type=Path)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.backend == "docker" and args.image_id is None:
        parser.error("Docker replay requires an immutable prepared --image-id")
    if args.backend == "local" and args.image_id is not None:
        parser.error("local replay does not use a task image")
    print(json.dumps(parent(args) if args.phase == "parent" else child(args), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
