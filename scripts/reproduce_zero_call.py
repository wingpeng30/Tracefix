"""Run TraceFixRunner end to end on a synthetic task without a provider client."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from tracefix import (
    AgentConfig,
    AgentStatus,
    BaseLLM,
    LLMConfig,
    LLMResponse,
    Message,
    MessageRole,
    RunConfig,
    TokenUsage,
    ToolCall,
    TraceFixRunner,
)

PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""


def _source_sha256() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(
        item for item in (root / "src" / "tracefix").rglob("*")
        if item.is_file() and "__pycache__" not in item.parts
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _response(content: str | None = None, *calls: ToolCall) -> LLMResponse:
    return LLMResponse(
        message=Message(role=MessageRole.ASSISTANT, content=content, tool_calls=calls),
        usage=TokenUsage(),
        model_name="offline/scripted",
        finish_reason="tool_calls" if calls else "stop",
    )


class _ScriptedLLM(BaseLLM):
    """A fixed fixture that cannot create a provider client or access a network."""

    def __init__(self, config: LLMConfig, skills_enabled: bool) -> None:
        super().__init__(config)
        self.skills_enabled = skills_enabled
        self.requests = 0
        self.turn = 0

    def complete(self, messages, tools=()) -> LLMResponse:
        self.requests += 1
        self.turn += 1
        tool_names = {tool.name for tool in tools}
        if self.skills_enabled and self.turn == 1:
            if "load_skill" not in tool_names or "tracefix-debugging" not in (
                messages[1].content or ""
            ):
                raise AssertionError("enabled skill catalog was not provided to the fixture")
            return _response(
                None,
                ToolCall(
                    id="skill-1", name="load_skill", arguments={"name": "tracefix-debugging"}
                ),
            )
        if self.skills_enabled and not any(
            item.metadata.get("kind") == "skill_instructions" for item in messages
        ):
            raise AssertionError("loaded skill instructions did not enter model context")
        offset = int(self.skills_enabled)
        calls = {
            1 + offset: ToolCall(
                id="search-1", name="search_code",
                arguments={"query": "VALUE", "path": ".", "max_results": 10},
            ),
            2 + offset: ToolCall(id="read-1", name="read_file", arguments={"path": "app.py"}),
            3 + offset: ToolCall(
                id="patch-1", name="apply_patch", arguments={"patch": PATCH}
            ),
            4 + offset: ToolCall(
                id="test-1", name="run_tests", arguments={"command": "pytest -q"}
            ),
            5 + offset: ToolCall(
                id="diff-1", name="get_git_diff", arguments={"context_lines": 3}
            ),
        }
        call = calls.get(self.turn)
        if call is None:
            return _response("synthetic repair complete")
        return _response(None, call)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def _make_source(root: Path) -> Path:
    repo = root / "source"
    repo.mkdir(parents=True)
    (repo / ".gitignore").write_text(".pytest_cache/\n__pycache__/\n", encoding="utf-8")
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_value.py").write_text(
        "from app import VALUE\n\ndef test_value_is_fixed():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "TraceFix smoke")
    _git(repo, "config", "user.email", "tracefix-smoke@example.invalid")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "synthetic reproduction fixture")
    return repo


def run_smoke(output: Path, *, skills_enabled: bool = False) -> dict[str, object]:
    """Run a real TraceFixRunner clone, tool, pytest, patch and trace cycle."""
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="tracefix-repro-") as temporary:
        source = _make_source(Path(temporary))
        llm_instances: list[_ScriptedLLM] = []

        def factory(config: LLMConfig) -> BaseLLM:
            model = _ScriptedLLM(config, skills_enabled)
            llm_instances.append(model)
            return model

        result = TraceFixRunner(factory).run(
            RunConfig(
                repo=source,
                task="Fix the synthetic app.VALUE test failure.",
                model_name="offline/scripted",
                output_dir=output / "runs",
                env_file=None,
                agent_config=AgentConfig(
                    skills_enabled=skills_enabled,
                    max_steps=8,
                    max_test_runs=2,
                    wall_time_seconds=120,
                ),
            )
        )
    if len(llm_instances) != 1:
        raise AssertionError("unexpected scripted model construction count")
    if (
        result.status is not AgentStatus.COMPLETED
        or result.changed_files != ("app.py",)
        or result.test_runs != 1
        or llm_instances[0].requests != result.step_count
    ):
        raise AssertionError(f"synthetic run did not satisfy its checks: {result.status}")
    trace_path = Path(result.trace_path)
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    supplier_events = [event for event in events if event.get("event_type") == "model_requested"]
    if len(supplier_events) != llm_instances[0].requests:
        raise AssertionError("scripted request count does not match the saved trajectory")
    report = {
        "kind": "tracefix_zero_provider_synthetic_reproduction",
        "tracefix_source_tree_sha256": _source_sha256(),
        "model_name": result.model_name,
        "model_requests": llm_instances[0].requests,
        "provider_client_constructed": False,
        "skills_enabled": skills_enabled,
        "status": result.status.value,
        "test_runs": result.test_runs,
        "changed_files": list(result.changed_files),
        "trace_path": str(trace_path),
        "diff_path": result.diff_path,
        "result_path": result.result_path,
        "skill_events": sum(
            event.get("event_type") in {"skill_catalog_exposed", "skill_activated"}
            for event in events
        ),
    }
    (output / "reproduction.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skills-enabled", action="store_true")
    args = parser.parse_args(argv)
    report = run_smoke(args.output, skills_enabled=args.skills_enabled)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
