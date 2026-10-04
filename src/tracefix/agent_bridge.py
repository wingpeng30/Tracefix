"""Run TraceFix's existing repository tools inside a Linux task container."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tracefix.exceptions import TraceFixError
from tracefix.messages import ToolCall
from tracefix.tools import create_default_tool_registry
from tracefix.tools.skills import SkillLimits


def recovery_state(tools, protected: set[str]) -> dict:
    """Return internal bridge state only between complete synchronous tool calls."""
    diff = tools.get("get_git_diff").execute(ToolCall(
        id="checkpoint-diff", name="get_git_diff", arguments={"context_lines": 3},
    ))
    if not diff.success or diff.output.get("truncated"):
        raise ValueError("cannot checkpoint the complete product diff")
    return {"schema_version": 1, "protected_dirs": sorted(protected),
            "skills": tools.get("load_skill").recovery_state()
            if "load_skill" in tools.names else None, "diff": diff.output}


def restore_state(tools, protected: set[str], workspace: Path, state: dict) -> None:
    """Recreate owned temporary identities and restore immutable Skills accounting."""
    names = state.get("protected_dirs")
    if (state.get("schema_version") != 1 or not isinstance(names, list)
            or not all(isinstance(name, str) for name in names)
            or not set(names).issubset({".tracefix-build-tmp", ".tracefix-test-tmp"})
            or ".tracefix-build-tmp" not in names):
        raise ValueError("invalid bridge recovery state")
    if ".tracefix-test-tmp" in names:
        temporary = workspace / ".tracefix-test-tmp"
        if temporary.is_symlink() or temporary.exists():
            raise ValueError("new container test temporary path already exists")
        temporary.mkdir()
    if "load_skill" in tools.names:
        tools.get("load_skill").restore_recovery_state(state["skills"])
    elif state.get("skills") is not None:
        raise ValueError("recovery state contains disabled Skills")
    protected.clear()
    protected.update(names)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--pytest-config")
    parser.add_argument("--pythonpath", action="append", default=[])
    parser.add_argument("--environment-json", type=Path)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repo-map-task")
    parser.add_argument("--repo-map-config")
    parser.add_argument("--skills-enabled", action="store_true")
    parser.add_argument("--skill-limits-json")
    parser.add_argument("--skills-root", type=Path)
    args = parser.parse_args()
    environment = (
        json.loads(args.environment_json.read_text(encoding="utf-8"))
        if args.environment_json
        else {}
    )
    if not isinstance(environment, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()
    ):
        raise ValueError("environment overrides must be a string mapping")
    # The test tool creates and registers its own protected tmp directory on
    # first execution; pre-registering it before creation breaks identity checks.
    protected = {".tracefix-build-tmp"}
    tools = create_default_tool_registry(
        args.workspace,
        max_output_chars=10_000_000,
        test_timeout_seconds=args.timeout,
        test_python_executable=args.python,
        test_pythonpath_entries=tuple(Path(path) for path in args.pythonpath),
        pytest_config=args.pytest_config,
        test_environment_variables=environment,
        evidence_dir=args.evidence,
        protected_dirs=protected,
        skills_enabled=args.skills_enabled,
        skills_root=args.skills_root,
        skill_limits=(
            SkillLimits.model_validate_json(args.skill_limits_json)
            if args.skills_enabled and args.skill_limits_json
            else None
        ),
    )
    repo_map = None
    if args.repo_map_task is not None:
        from tracefix.repository import RepoMapConfig, RepositoryIndexer

        config = RepoMapConfig.model_validate_json(args.repo_map_config or "{}")
        indexer = RepositoryIndexer(args.workspace, config)
        repo_map = indexer.make_repo_map(indexer.build(), args.repo_map_task).model_dump(
            mode="json"
        )
    sys.stdout.write(
        json.dumps(
            {
                "type": "hello",
                "protocol": 1,
                "run_id": args.run_id,
                "tools": [spec.model_dump(mode="json") for spec in tools.specs()],
                "skill_catalog": [entry.model_dump(mode="json") for entry in tools.skill_catalog],
                "repo_map": repo_map,
            }
        )
        + "\n"
    )
    sys.stdout.flush()
    for line in sys.stdin:
        call = None
        try:
            call = ToolCall.model_validate_json(line)
            if call.name in {"__recovery_state__", "__restore_state__"}:
                from tracefix.tools import ToolResult

                if call.name == "__restore_state__":
                    restore_state(tools, protected, args.workspace, call.arguments)
                result = ToolResult(call_id=call.id, tool_name=call.name, success=True,
                                    output=recovery_state(tools, protected))
            elif call.name == "__prepare__":
                target = ToolCall.model_validate(call.arguments)
                tool = tools.get(target.name)
                prepare = getattr(tool, "prepare", None)
                if prepare is not None:
                    prepare(target)
                result = __import__("tracefix.tools", fromlist=["ToolResult"]).ToolResult(
                    call_id=call.id, tool_name=call.name, success=True
                )
            else:
                tool = tools.get(call.name)
                if call.name == "run_tests":

                    def started(call_id: str = call.id) -> None:
                        sys.stdout.write(
                            json.dumps({"type": "process_started", "call_id": call_id}) + "\n"
                        )
                        sys.stdout.flush()

                    tool.on_process_started = started
                result = tool.execute(call)
        except Exception as exc:  # Keep the protocol alive; record the tool rejection.
            call_id = "invalid-call"
            name = "unknown"
            try:
                value = json.loads(line)
                call_id = value.get("id", call_id)
                name = value.get("name", "unknown")
            except (ValueError, AttributeError):
                pass
            from tracefix.tools import ToolResult

            result = ToolResult(
                call_id=str(call_id),
                tool_name=str(name),
                success=False,
                error=(exc.message if isinstance(exc, TraceFixError) else str(exc)),
            )
        sys.stdout.write(
            json.dumps(
                {
                    "type": "result",
                    "call_id": call.id if call is not None else "invalid-call",
                    "result": result.model_dump(mode="json"),
                }
            )
            + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
