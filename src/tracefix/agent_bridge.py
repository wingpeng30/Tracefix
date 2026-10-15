"""Run TraceFix's existing repository tools inside a Linux task container."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from tracefix.capabilities import (
    CallerOrigin,
    CapabilityCatalog,
    CapabilityPolicy,
    ExecutionProfile,
    ToolDispatcher,
)
from tracefix.exceptions import TraceFixError
from tracefix.messages import ToolCall
from tracefix.tools import create_default_tool_registry
from tracefix.tools.skills import SkillLimits


def _tool_names(tools) -> tuple[str, ...]:
    """Read the frozen registry names without depending on a concrete registry type."""
    names = getattr(tools, "names", None)
    if names is not None:
        return tuple(names)
    return tuple(spec.name for spec in tools.specs())


def recovery_state(tools, protected: set[str]) -> dict:
    """Return internal bridge state only between complete synchronous tool calls."""
    diff = tools.get("get_git_diff").execute(
        ToolCall(
            id="checkpoint-diff",
            name="get_git_diff",
            arguments={"context_lines": 3},
        )
    )
    if not diff.success or diff.output.get("truncated"):
        raise ValueError("cannot checkpoint the complete product diff")
    return {
        "schema_version": 1,
        "protected_dirs": sorted(protected),
        "skills": tools.get("load_skill").recovery_state()
        if "load_skill" in _tool_names(tools)
        else None,
        "diff": diff.output,
    }


def restore_state(tools, protected: set[str], workspace: Path, state: dict) -> None:
    """Recreate owned temporary identities and restore immutable Skills accounting."""
    names = state.get("protected_dirs")
    if (
        state.get("schema_version") != 1
        or not isinstance(names, list)
        or not all(isinstance(name, str) for name in names)
        or not set(names).issubset({".tracefix-build-tmp", ".tracefix-test-tmp"})
        or ".tracefix-build-tmp" not in names
    ):
        raise ValueError("invalid bridge recovery state")
    if ".tracefix-test-tmp" in names:
        temporary = workspace / ".tracefix-test-tmp"
        if temporary.is_symlink() or temporary.exists():
            raise ValueError("new container test temporary path already exists")
        temporary.mkdir()
    if "load_skill" in _tool_names(tools):
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
    parser.add_argument(
        "--execution-profile",
        choices=tuple(profile.value for profile in ExecutionProfile),
        default=ExecutionProfile.WORKSPACE_WRITE.value,
    )
    parser.add_argument("--repo-map-task")
    parser.add_argument("--repo-map-config")
    parser.add_argument("--skills-enabled", action="store_true")
    parser.add_argument("--artifacts-enabled", action="store_true")
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--skill-limits-json")
    parser.add_argument("--skills-root", type=Path)
    parser.add_argument("--capability-policy-file", type=Path)
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
    artifact_store = None
    if args.artifacts_enabled:
        from tracefix.artifacts import ArtifactStore

        if args.artifact_root is None:
            raise ValueError("artifact-enabled bridges require a dedicated evidence root")
        artifact_store = ArtifactStore(
            args.artifact_root,
            run_id=args.run_id,
            workspace=args.workspace,
            secret_values=tuple(environment.values()),
        )
    tools = create_default_tool_registry(
        args.workspace,
        max_output_chars=10_000_000,
        test_timeout_seconds=args.timeout,
        test_python_executable=args.python,
        test_pythonpath_entries=tuple(Path(path) for path in args.pythonpath),
        pytest_config=args.pytest_config,
        test_environment_variables=environment,
        evidence_dir=args.evidence,
        artifact_store=artifact_store,
        protected_dirs=protected,
        skills_enabled=args.skills_enabled,
        skills_root=args.skills_root,
        skill_limits=(
            SkillLimits.model_validate_json(args.skill_limits_json)
            if args.skills_enabled and args.skill_limits_json
            else None
        ),
    )
    dispatcher = ToolDispatcher(
        CapabilityPolicy(
            CapabilityCatalog.for_tool_names(_tool_names(tools)),
            ExecutionProfile(args.execution_profile),
        )
    )
    policy_config_sha256 = None
    if args.capability_policy_file is not None:
        policy_path = args.capability_policy_file
        if policy_path.is_symlink() or not policy_path.is_file():
            raise ValueError("executor capability policy bundle is unavailable")
        raw_policy = policy_path.read_bytes()
        policy_config_sha256 = hashlib.sha256(raw_policy).hexdigest()
        try:
            policy_config = json.loads(raw_policy.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("executor capability policy bundle is malformed") from exc
        expected_policy_config = {
            "schema_version": 1,
            "policy_version": dispatcher.policy.VERSION,
            "execution_profile": dispatcher.policy.profile.value,
            "capability_catalog_sha256": dispatcher.policy.catalog.sha256,
            "capability_policy_sha256": dispatcher.policy.sha256,
            "capabilities": [
                item.model_dump(mode="json") for item in dispatcher.policy.catalog.descriptors
            ],
        }
        if policy_config != expected_policy_config:
            raise ValueError("executor capability policy bundle does not match tool catalog")
    repo_map = None
    if args.repo_map_task is not None:
        from tracefix.repository import RepoMapConfig, RepositoryIndexer

        config = RepoMapConfig.model_validate_json(args.repo_map_config or "{}")
        indexer = RepositoryIndexer(args.workspace, config)
        repo_map = indexer.make_repo_map(indexer.build(), args.repo_map_task).model_dump(
            mode="json"
        )
    hello = {
        "type": "hello",
        "protocol": 1,
        "run_id": args.run_id,
        "execution_profile": args.execution_profile,
        "capability_catalog_sha256": dispatcher.policy.catalog.sha256,
        "capability_policy_sha256": dispatcher.policy.sha256,
        "executor_policy_config_sha256": policy_config_sha256,
        "tools": [spec.model_dump(mode="json") for spec in tools.specs()],
        "skill_catalog": [entry.model_dump(mode="json") for entry in tools.skill_catalog],
        "repo_map": repo_map,
    }
    if artifact_store is not None:
        hello["artifact_protocol"] = {
            "version": 1,
            "max_artifact_bytes": 8 * 1024 * 1024,
            "max_run_bytes": 64 * 1024 * 1024,
            "max_run_artifacts": 128,
        }
    sys.stdout.write(json.dumps(hello) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        call = None
        try:
            call = ToolCall.model_validate_json(line)
            if call.name in {"__recovery_state__", "__restore_state__"}:
                from tracefix.tools import ToolResult

                if call.name == "__restore_state__":
                    restore_state(tools, protected, args.workspace, call.arguments)
                result = ToolResult(
                    call_id=call.id,
                    tool_name=call.name,
                    success=True,
                    output=recovery_state(tools, protected),
                )
            elif call.name == "__prepare__":
                target = ToolCall.model_validate(call.arguments)
                decision = dispatcher.authorize(target.name, CallerOrigin.MODEL)
                from tracefix.tools import ToolResult

                if not decision.allowed:
                    result = ToolResult(
                        call_id=call.id,
                        tool_name=call.name,
                        success=False,
                        error=f"capability policy denied: {decision.reason_code}",
                        metadata={
                            "policy_blocked": True,
                            "reason_code": decision.reason_code,
                            "policy_sha256": decision.policy_sha256,
                            "catalog_sha256": decision.catalog_sha256,
                        },
                    )
                else:
                    tool = tools.get(target.name)
                    from tracefix.tools.invocation import PreparedInvocation

                    if target.name == "run_tests":
                        PreparedInvocation(tool, target)
                    else:
                        prepare = getattr(tool, "prepare", None)
                        if prepare is not None:
                            prepare(target)
                    result = ToolResult(call_id=call.id, tool_name=call.name, success=True)
            else:
                decision = dispatcher.authorize(call.name, CallerOrigin.MODEL)
                from tracefix.tools import ToolResult

                if not decision.allowed:
                    result = ToolResult(
                        call_id=call.id,
                        tool_name=call.name,
                        success=False,
                        error=f"capability policy denied: {decision.reason_code}",
                        metadata={
                            "policy_blocked": True,
                            "reason_code": decision.reason_code,
                            "policy_sha256": decision.policy_sha256,
                            "catalog_sha256": decision.catalog_sha256,
                        },
                    )
                else:
                    tool = tools.get(call.name)
                    if call.name == "run_tests":

                        def started(call_id: str = call.id) -> None:
                            sys.stdout.write(
                                json.dumps({"type": "process_started", "call_id": call_id}) + "\n"
                            )
                            sys.stdout.flush()

                        from tracefix.tools.invocation import PreparedInvocation

                        result = PreparedInvocation(tool, call).execute(started)
                    else:
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
