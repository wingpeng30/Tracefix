"""Preparation, isolated execution and evidence-only reporting for comparisons."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from tracefix import AgentConfig, RunConfig, TraceFixRunner
from tracefix.checkpoint import ProcessLock
from tracefix.comparison import (
    LIMITS,
    PATCH_RULE,
    PRODUCT_BASE,
    TASK_IDS,
    ComparisonBudget,
    digest,
    extract_patch,
    file_sha,
    read_json,
    schedule,
    simple_loop,
    static_material,
    summarize,
    write_json,
)
from tracefix.exceptions import PreRequestBudgetExceeded, ToolError
from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.models.base import LLMConfig
from tracefix.provenance import inspect_test_environment
from tracefix.real_benchmark import load_real_issue_tasks
from tracefix.real_experiment import (
    _create_behavior_checkout,
    _probe_source_import,
    _pytest_evidence,
    validate_agent_patch_strict,
    validate_real_task_behavior,
)
from tracefix.real_recipes import EnvironmentRecipe
from tracefix.runtime import load_environment_file
from tracefix.tools.builtin import RunTestsTool, create_default_tool_registry


def git(source: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=source).decode("utf-8").strip()


def source_identity(source: Path) -> dict:
    if git(source, "status", "--porcelain"):
        raise ValueError("source must be completely clean")
    files = git(source, "ls-files", "-z").split("\0")
    return {
        "commit": git(source, "rev-parse", "HEAD"),
        "files": {name: file_sha(source / name) for name in files if name},
    }


def config_for(spec: dict, task: str, output: Path, env_file: Path | None) -> RunConfig:
    return RunConfig(
        repo=Path(spec["source"]),
        task=task,
        output_dir=output,
        env_file=env_file,
        model_name="deepseek/deepseek-flash",
        llm_max_retries=0,
        llm_timeout_seconds=60,
        per_request_output_tokens=2048,
        test_python_executable=Path(spec["python"]),
        source_import=spec.get("source_import")
        or (spec.get("recipe") or {}).get("source_import_probe"),
        test_pythonpath_entries=tuple(Path(p) for p in spec.get("pythonpath", [])),
        environment_recipe=EnvironmentRecipe.model_validate(spec["recipe"])
        if spec.get("recipe")
        else None,
        test_environment_variables=spec.get(
            "environment_variables", (spec.get("recipe") or {}).get("environment_variables", {})
        ),
        agent_config=AgentConfig(
            max_steps=20,
            max_input_tokens=60000,
            max_output_tokens=8000,
            max_test_runs=16,
            wall_time_seconds=900,
            record_request_views=True,
        ),
    )


def checked_test(source: Path, directory: Path, spec: dict, selectors: list[str]) -> dict:
    config = config_for(spec, "Offline verification", directory / "unused", None)
    directory.mkdir(parents=True, exist_ok=True)
    # Strict hidden verification has already built/probed these harness-owned checkouts.
    preparation = (
        {"success": True, "reused_qualified_build": True}
        if (source / ".tracefix-build-tmp").is_dir()
        else TraceFixRunner._prepare_workspace(config, source, directory)
    )
    if preparation.get("success") is not True:
        raise ValueError(f"workspace preparation failed: {preparation}")
    tool = RunTestsTool(
        source,
        python_executable=spec["python"],
        pythonpath_entries=config.test_pythonpath_entries,
        pytest_config=config.environment_recipe.pytest_config
        if config.environment_recipe
        else None,
        environment_variables=config.test_environment_variables,
        evidence_dir=directory / "evidence",
        max_output_chars=1_000_000,
    )
    result = tool.execute(
        ToolCall(
            id="independent",
            name="run_tests",
            arguments={"command": "pytest -q " + " ".join(shlex.quote(s) for s in selectors)},
        )
    )
    write_json(directory / "result.json", result.model_dump(mode="json"))
    evidence = _pytest_evidence(result, source)
    # RunTestsTool has a separate complete audit contract; strict P2 additionally
    # validates hidden F2P selectors using its own collection/execution plugin.
    output = result.output or {}
    counts = output.get("test_counts", {})
    probe_recipe = config.environment_recipe or EnvironmentRecipe(
        task_id=spec["task_id"], source_import_probe=config.source_import
    )
    probe_error, probe_path = _probe_source_import(probe_recipe, source, Path(spec["python"]))
    return {
        "passed": result.success
        and output.get("test_status") == "passed"
        and counts.get("tests", 0) > 0
        and not counts.get("errors", 0)
        and not counts.get("skipped", 0)
        and probe_error is None,
        "source_probe": {"error": probe_error, "record": probe_path},
        "test": result.model_dump(mode="json"),
        "p2_evidence": evidence.model_dump(mode="json"),
    }


def qualify_task(spec: dict, root: Path) -> dict:
    """Reference material is handled only here, never passed to model executors."""
    source = Path(spec["source"])
    output = root / "qualification" / spec["task_id"]
    if spec["kind"] == "public":
        # Existing reviewed qualifier preserves corrected-reference provenance.
        helper = Path(spec["package"]).resolve().parents[2] / "scripts/qualify_public_task.py"
        module_spec = importlib.util.spec_from_file_location("comparison_public_qualifier", helper)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        record = module.qualify(source, Path(spec["python"]), output, spec["task_id"])
        prepared = output / "prepared-source"
        manifest = module.load_manifest(Path(spec["package"]))
        updated = {
            **spec,
            "source": str(prepared),
            "source_import": manifest["source_import"],
            "selectors": [manifest["task_target"], *manifest["regression_targets"]],
            "issue": (Path(spec["package"]) / "task.md").read_text(encoding="utf-8"),
        }
        public_failure = read_json(output / "base-1" / "record.json")["targets"][0]["test"]
        # Tests in these reviewed public contracts are intentionally user-visible.
        updated["public_failure"] = (public_failure.get("output") or {}).get("stdout", "")
        updated["qualification"] = record
        base = read_json(output / "base-1" / "record.json")
        updated["verification_nodes"] = sorted(
            node
            for row in base["targets"]
            for node in row["test"]["output"]["test_counts"]["node_ids"]
        )
    elif spec["kind"] == "real":
        task = load_real_issue_tasks(Path(spec["task_root"]), task_ids=(spec["task_id"],))[0]
        recipe = EnvironmentRecipe.model_validate(spec["recipe"])
        record = validate_real_task_behavior(
            task,
            source=source,
            test_python=Path(spec["python"]),
            output_dir=output / "hidden",
            recipe=recipe,
        )
        write_json(output / "behavior.json", record.model_dump(mode="json"))
        if (
            not record.eligible_for_llm_prescreen
            or record.qualification_type != "assertion_failure"
        ):
            raise ValueError(f"task unqualified: {record.eligibility_reason}")
        regressions = []
        for variant in ("initial", "gold"):
            checkout = output / "hidden" / f"{task.id}-{variant}"
            regressions.append(
                checked_test(checkout, output / f"regression-{variant}", spec, spec["regressions"])
            )
        if not all(r["passed"] for r in regressions):
            raise ValueError("base/reference regressions failed")
        nodes = [sorted(r["test"]["output"]["test_counts"]["node_ids"]) for r in regressions]
        if nodes[0] != nodes[1]:
            raise ValueError("base/reference regression identities differ")
        # Do not expose hidden failure logs or newly injected tests to any arm.
        updated = {
            **spec,
            "issue": task.problem_statement,
            "public_failure": "本任务仅公开原始问题描述；隐藏验收日志不向模型提供。",
            "qualification": record.model_dump(mode="json"),
            "qualification_nodes": list(record.gold_evidence.executed_node_ids),
            "task_metadata_sha256": file_sha(task.task_dir / "task.json"),
            "artifact_hashes": task.hashes.model_dump(mode="json"),
            "regression_nodes": nodes[0],
        }
    elif spec["kind"] == "fixture":
        output.mkdir(parents=True)
        results = []
        for variant in ("base", "reference"):
            checkout = output / variant
            _create_behavior_checkout(source, checkout)
            if variant == "reference":
                patch = spec["fixture_patch"]
                tools = create_default_tool_registry(checkout)
                application = tools.get("apply_patch").execute(
                    ToolCall(id="reference", name="apply_patch", arguments={"patch": patch})
                )
                if not application.success:
                    raise ValueError(application.error)
            result = checked_test(checkout, output / (variant + "-test"), spec, spec["selectors"])
            results.append(result)
        if results[0]["passed"] or not results[1]["passed"]:
            raise ValueError("fixture qualification failed")
        updated = {
            **spec,
            "qualification": {"offline_fixture": True},
            "public_failure": "sample.add(1, 2) returns 0, expected 3",
        }
    else:
        raise ValueError("unsupported task kind")
    updated["source_identity"] = source_identity(Path(updated["source"]))
    updated["environment_identity"] = inspect_test_environment(
        Path(updated["python"]),
        pythonpath_entries=tuple(Path(p) for p in updated.get("pythonpath", [])),
    ).fingerprint_sha256
    write_json(output / "comparison-qualification.json", updated)
    return updated


def prepare(catalog: Path, root: Path, prices: Path, *, mode: str = "live") -> dict:
    root.mkdir(parents=True, exist_ok=True)
    with ProcessLock(root):
        if (root / "protocol.json").exists():
            raise ValueError("protocol already frozen; cannot overwrite")
        specifications = read_json(catalog)
        if [s["task_id"] for s in specifications] != list(TASK_IDS):
            raise ValueError("exact ten-task catalog required")
        if (
            mode == "live"
            and any(s["kind"] == "fixture" for s in specifications)
            or mode not in {"live", "offline"}
        ):
            raise ValueError("invalid campaign mode/task kind")
        price = read_json(prices)
        if (
            price.get("model") != "deepseek-flash"
            or price.get("input_peak_cny") != 2.0
            or (price.get("output_peak_cny") != 8.0)
        ):
            raise ValueError("price identity mismatch")
        qualified = []
        for spec in specifications:
            qualified.append(qualify_task(spec, root))
        tasks = {}
        artifacts = {}
        for spec in qualified:
            source = Path(spec["source"])
            material = static_material(
                source, spec["issue"], list(spec["source_identity"]["files"])
            )
            prompt = (
                spec["issue"]
                + "\n"
                + PATCH_RULE
                + "\n[公开初始证据]\n"
                + spec["public_failure"][:6000]
                + "\n"
                + material
            )
            prompt_path = root / "prompts" / (spec["task_id"] + ".txt")
            prompt_path.parent.mkdir(exist_ok=True)
            prompt_path.write_text(prompt, encoding="utf-8")
            tasks[spec["task_id"]] = {
                **spec,
                "prompt_path": str(prompt_path),
                "prompt_sha256": file_sha(prompt_path),
            }
            artifacts[str(prompt_path.relative_to(root))] = file_sha(prompt_path)
        write_json(root / "prices.json", price)
        artifacts["prices.json"] = file_sha(root / "prices.json")
        implementation = Path(__file__).resolve().parents[2]
        protocol = {
            "schema_version": 1,
            "product_base": PRODUCT_BASE,
            "implementation_commit": git(implementation, "rev-parse", "HEAD"),
            "implementation_sha256": TraceFixRunner._implementation_sha256(),
            "mode": mode,
            "limits": LIMITS,
            "tasks": tasks,
            "schedule": schedule(),
            "artifacts": artifacts,
            "arm_c_config": AgentConfig().model_dump(mode="json"),
            "arm_b": "plain tool loop; no reminders, caching, repo map or compression",
            "bootstrap": {"seed": 20261005, "resamples": 10000},
        }
        write_json(root / "protocol.json", protocol)
        write_json(root / "protocol.sha256.json", {"sha256": digest(protocol)})
        return protocol


def check_protocol(root: Path, *, require_ci: bool = True) -> dict:
    protocol = read_json(root / "protocol.json")
    if digest(protocol) != read_json(root / "protocol.sha256.json")["sha256"]:
        raise ValueError("protocol corrupted")
    if protocol["limits"] != LIMITS or protocol["schedule"] != schedule():
        raise ValueError("frozen schedule/limits changed")
    implementation = Path(__file__).resolve().parents[2]
    if (
        git(implementation, "rev-parse", "HEAD") != protocol["implementation_commit"]
        or TraceFixRunner._implementation_sha256() != protocol["implementation_sha256"]
    ):
        raise ValueError("implementation identity changed")
    if protocol["mode"] == "live" and git(
        implementation, "status", "--porcelain", "--untracked-files=no"
    ):
        raise ValueError("live implementation must be clean")
    for relative, expected in protocol["artifacts"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or file_sha(path) != expected:
            raise ValueError("frozen artifact changed")
    for spec in protocol["tasks"].values():
        if source_identity(Path(spec["source"])) != spec["source_identity"]:
            raise ValueError("source identity changed")
        fingerprint = inspect_test_environment(
            Path(spec["python"]),
            pythonpath_entries=tuple(Path(p) for p in spec.get("pythonpath", [])),
        ).fingerprint_sha256
        if fingerprint != spec["environment_identity"]:
            raise ValueError("test environment changed")
        if spec.get("service_identity"):
            identity = spec["service_identity"]
            opener = build_opener(ProxyHandler({}))
            with opener.open(identity["identity_url"], timeout=5) as response:
                if json.load(response) != identity:
                    raise ValueError("controlled service identity changed")
    if protocol["mode"] == "live" and require_ci:
        ci = read_json(root / "ci-evidence.json")
        if (
            ci["head_sha"] != protocol["implementation_commit"]
            or ci["conclusion"] != "success"
            or len(ci["jobs"]) != 5
            or any(j["conclusion"] != "success" for j in ci["jobs"])
        ):
            raise ValueError("exact implementation CI five-job gate not passed")
    return protocol


class FixtureClient:
    """Explicit zero-supplier replay through the same provider adapter."""

    def __init__(self, patch: str):
        self.patch, self.calls = patch, 0

    def completion(self, **kwargs):
        self.calls += 1
        tool_call = None
        content = self.patch
        if kwargs.get("tools"):
            if self.calls == 1:
                tool_call = {
                    "id": "read",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "sample.py"}),
                    },
                }
            elif self.calls == 2:
                tool_call = {
                    "id": "patch",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps({"patch": self.patch}),
                    },
                }
            elif self.calls == 3:
                tool_call = {
                    "id": "test",
                    "type": "function",
                    "function": {
                        "name": "run_tests",
                        "arguments": json.dumps({"command": "pytest -q"}),
                    },
                }
            elif self.calls == 4:
                tool_call = {
                    "id": "diff",
                    "type": "function",
                    "function": {"name": "get_git_diff", "arguments": "{}"},
                }
            content = None if tool_call else "Finished"
        return {
            "model": "offline/comparison-fixture",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [tool_call] if tool_call else [],
                    },
                    "finish_reason": "tool_calls" if tool_call else "stop",
                }
            ],
        }


def verify(spec: dict, patch: Path, root: Path) -> dict:
    if not patch.is_file() or not patch.stat().st_size:
        return {"passed": False, "reason": "empty patch"}
    if spec["kind"] == "real":
        task = load_real_issue_tasks(Path(spec["task_root"]), task_ids=(spec["task_id"],))[0]
        if (
            file_sha(task.task_dir / "task.json") != spec["task_metadata_sha256"]
            or task.hashes.model_dump(mode="json") != spec["artifact_hashes"]
        ):
            raise ValueError("hidden artifact identity changed")
        record = validate_agent_patch_strict(
            task,
            source=Path(spec["source"]),
            agent_patch=patch,
            test_python=Path(spec["python"]),
            output_dir=root,
            recipe=EnvironmentRecipe.model_validate(spec["recipe"]),
            expected_node_ids=tuple(spec["qualification_nodes"]),
        )
        result = record.model_dump(mode="json")
        result["passed"] = record.eligible
        if record.eligible:
            checkout = root / f"{task.id}-agent"
            regressions = checked_test(checkout, root / "regressions", spec, spec["regressions"])
            result["regressions"] = regressions
            result["passed"] = regressions["passed"]
            actual_nodes = sorted(regressions["test"]["output"]["test_counts"]["node_ids"])
            if actual_nodes != spec["regression_nodes"]:
                result.update(passed=False, reason="regression execution set changed")
    else:
        workspace = TraceFixRunner._clone_repository(Path(spec["source"]), root / "workspace")
        tools = create_default_tool_registry(workspace)
        try:
            applied = tools.get("apply_patch").execute(
                ToolCall(
                    id="independent-apply",
                    name="apply_patch",
                    arguments={"patch": patch.read_text(encoding="utf-8")},
                )
            )
        except ToolError as exc:
            return {"passed": False, "reason": f"invalid_patch: {exc}"}
        if not applied.success:
            return {"passed": False, "reason": applied.error}
        from tracefix.real_experiment import classify_p2_paths

        changed = git(workspace, "diff", "--name-only", "HEAD").splitlines()
        changed += git(workspace, "ls-files", "--others", "--exclude-standard").splitlines()
        _, forbidden = classify_p2_paths(tuple(changed))
        if forbidden:
            return {"passed": False, "reason": "modified tests/configuration"}
        result = checked_test(workspace, root / "tests", spec, spec["selectors"])
        if (
            spec.get("verification_nodes")
            and sorted(result["test"]["output"]["test_counts"]["node_ids"])
            != spec["verification_nodes"]
        ):
            result.update(passed=False, reason="public execution set changed")
    fingerprint = inspect_test_environment(
        Path(spec["python"]), pythonpath_entries=tuple(Path(p) for p in spec.get("pythonpath", []))
    ).fingerprint_sha256
    if fingerprint != spec["environment_identity"]:
        result.update(passed=False, reason="verification environment changed")
    write_json(root / "verification.json", result)
    return result


def execute_trial(root: Path, protocol: dict, row: dict, env_file: Path | None) -> dict:
    directory = root / "trials" / row["id"]
    directory.mkdir(parents=True)
    record = {**row, "finished": False, "passed": False, "status": "started"}
    write_json(directory / "record.json", record)
    started = time.monotonic()
    spec = protocol["tasks"][row["task_id"]]
    prompt = Path(spec["prompt_path"]).read_text(encoding="utf-8")
    client = FixtureClient(spec["fixture_patch"]) if protocol["mode"] == "offline" else None
    adapter = None

    def factory(config):
        nonlocal adapter
        if protocol["mode"] == "offline":
            config = config.model_copy(
                update={
                    "model_name": "deepseek/deepseek-flash",
                    "extra_kwargs": {
                        "api_base": "https://api.deepseek.com",
                        "extra_body": {"thinking": {"type": "disabled"}},
                    },
                }
            )
        adapter = ComparisonBudget(
            config, root=root, trial=row, protocol_sha=digest(protocol), client=client
        )
        return adapter

    patch = directory / "patch.diff"
    try:
        config = config_for(spec, prompt, directory / "agent", env_file)
        if row["arm"] == "C":
            if protocol["mode"] == "offline":
                config = config.model_copy(update={"model_name": "offline/comparison-fixture"})
            result = TraceFixRunner(llm_factory=factory).run(config)
            record["agent_result"] = result.result_path
            record["status"] = result.status.value
            record["reason"] = result.stop_reason
            saved = Path(result.result_path).parent / "patch.diff"
            if saved.exists():
                patch.write_bytes(saved.read_bytes())
        else:
            workspace = TraceFixRunner._clone_repository(
                Path(spec["source"]), directory / "workspace"
            )
            preparation = TraceFixRunner._prepare_workspace(config, workspace, directory)
            if preparation.get("success") is not True:
                raise ValueError("workspace preparation failed")
            tools = create_default_tool_registry(
                workspace,
                test_python_executable=config.test_python_executable,
                test_pythonpath_entries=config.test_pythonpath_entries,
                pytest_config=config.environment_recipe.pytest_config
                if config.environment_recipe
                else None,
                test_environment_variables=config.test_environment_variables,
                protected_dirs={".tracefix-build-tmp"},
                evidence_dir=directory / "tools",
            )
            llm_config = LLMConfig(
                model_name=config.model_name,
                temperature=0.0,
                max_output_tokens=8000 if row["arm"] == "A" else 2048,
                timeout_seconds=60,
                max_retries=0,
                extra_kwargs={
                    "api_base": "https://api.deepseek.com",
                    "extra_body": {"thinking": {"type": "disabled"}},
                },
            )
            llm = factory(llm_config)
            if row["arm"] == "A":
                response = llm.complete(
                    [
                        Message(
                            role=MessageRole.SYSTEM,
                            content="修复Python仓库。"
                            + PATCH_RULE
                            + "仅输出一个可应用的Git unified diff，不得调用工具。",
                        ),
                        Message(role=MessageRole.USER, content=prompt),
                    ]
                )
                application = tools.get("apply_patch").execute(
                    ToolCall(
                        id="bare-apply",
                        name="apply_patch",
                        arguments={"patch": extract_patch(response.message.content or "")},
                    )
                )
                write_json(directory / "application.json", application.model_dump(mode="json"))
                record["status"] = "completed" if application.success else "invalid_patch"
            else:
                record.update(simple_loop(llm, tools, prompt, directory / "messages.json"))
            diff, _ = TraceFixRunner._collect_diff(workspace, {".tracefix-build-tmp"})
            patch.write_text(diff, encoding="utf-8")
    except PreRequestBudgetExceeded as exc:
        record.update(status="budget_exhausted", reason=str(exc))
    except (ValueError, ToolError) as exc:
        record.update(status="invalid_patch" if row["arm"] == "A" else "failed", reason=str(exc))
    except Exception as exc:
        record.update(
            status="failed", reason=f"{type(exc).__name__}: {exc}", infrastructure_failure=True
        )
    # Preserve candidate patches even when a simple loop reaches its limit.
    if not patch.exists() and (directory / "workspace").exists():
        diff, _ = TraceFixRunner._collect_diff(directory / "workspace", {".tracefix-build-tmp"})
        patch.write_text(diff, encoding="utf-8")
    record["seconds"] = time.monotonic() - started
    write_json(directory / "record.json", record)
    requests = (
        read_json(root / "requests.json")["requests"] if (root / "requests.json").exists() else []
    )
    if any(r["status"] != "completed" for r in requests):
        record["status"] = "unknown_request"
        write_json(directory / "record.json", record)
        raise ValueError("unknown provider result; campaign halted")
    record["phase"] = "agent_completed"
    record["patch_sha256"] = file_sha(patch) if patch.exists() else None
    write_json(directory / "record.json", record)
    return finalize_trial(directory, record, spec)


def finalize_trial(directory: Path, record: dict, spec: dict) -> dict:
    patch = directory / "patch.diff"
    if (file_sha(patch) if patch.exists() else None) != record["patch_sha256"]:
        raise ValueError("candidate patch changed before verification")
    verification_dir = directory / "verification"
    if verification_dir.exists():
        # A crashed verifier's products are evidence: never overwrite them.
        verification_dir = directory / f"verification-{time.time_ns()}"
    verification = verify(spec, patch, verification_dir)
    record.update(finished=True, passed=verification["passed"], verification=verification)
    record["artifacts"] = {
        str(p.relative_to(directory)): file_sha(p)
        for p in directory.rglob("*")
        if p.is_file() and ".git" not in p.parts and p.name != "record.json"
    }
    record["record_sha256"] = digest(record)
    write_json(directory / "record.json", record)
    return record


def validate_record(record: dict, directory: Path, row: dict) -> None:
    for field in ("id", "task_id", "arm", "repetition"):
        if record.get(field) != row[field]:
            raise ValueError("trial identity mismatch")
    copied = dict(record)
    expected = copied.pop("record_sha256", None)
    if digest(copied) != expected:
        raise ValueError("trial record corrupted")
    for relative, expected in record["artifacts"].items():
        artifact = (directory / relative).resolve()
        if not artifact.is_relative_to(directory.resolve()) or file_sha(artifact) != expected:
            raise ValueError("completed trial artifact corrupted")


def validate_requests(root: Path, protocol: dict) -> list[dict]:
    if not (root / "requests.json").exists():
        return []
    ledger = read_json(root / "requests.json")
    if ledger["protocol_sha256"] != digest(protocol) or ledger["limit_cny"] != 20.0:
        raise ValueError("request ledger identity mismatch")
    for row in ledger["requests"]:
        for suffix, key in (("request", "request_sha256"), ("response", "response_sha256")):
            if (
                key in row
                and file_sha(root / "provider" / f"{row['id']}-{suffix}.json") != row[key]
            ):
                raise ValueError("provider evidence corrupted")
    return ledger["requests"]


def run(root: Path, env_file: Path | None = None, *, max_trials: int = 90) -> list[dict]:
    with ProcessLock(root):
        protocol = check_protocol(root)
        requests = validate_requests(root, protocol)
        if any(r["status"] != "completed" for r in requests):
            raise ValueError("unknown request; no automatic replay")
        if protocol["mode"] == "live":
            load_environment_file(env_file)
        results = []
        for row in protocol["schedule"]:
            path = root / "trials" / row["id"] / "record.json"
            if path.exists():
                previous = read_json(path)
                if not previous.get("finished"):
                    if previous.get("phase") != "agent_completed":
                        raise ValueError("incomplete prior trial; no automatic replay")
                    previous = finalize_trial(
                        path.parent, previous, protocol["tasks"][row["task_id"]]
                    )
                validate_record(previous, path.parent, row)
                results.append(previous)
                continue
            if len(results) >= max_trials:
                break
            results.append(execute_trial(root, protocol, row, env_file))
            print(
                json.dumps(
                    {
                        "id": row["id"],
                        "task": row["task_id"],
                        "arm": row["arm"],
                        "passed": results[-1]["passed"],
                        "status": results[-1]["status"],
                    }
                ),
                flush=True,
            )
        return results


def report(root: Path) -> dict:
    protocol = read_json(root / "protocol.json")
    if digest(protocol) != read_json(root / "protocol.sha256.json")["sha256"]:
        raise ValueError("protocol corrupted")
    trials = [
        read_json(root / "trials" / r["id"] / "record.json")
        for r in protocol["schedule"]
        if (root / "trials" / r["id"] / "record.json").exists()
    ]
    for row, record in (
        (row, read_json(root / "trials" / row["id"] / "record.json"))
        for row in protocol["schedule"]
        if (root / "trials" / row["id"] / "record.json").exists()
    ):
        if record.get("finished"):
            validate_record(record, root / "trials" / row["id"], row)
    requests = validate_requests(root, protocol)
    summary = summarize(protocol, trials, requests)
    write_json(root / "summary.json", summary)
    lines = [
        "# Cold-start three-arm comparison",
        "",
        f"Mode: {protocol['mode']}; "
        f"started {summary['started']}/90, unstarted {summary['unstarted']}.",
        "",
        "| Arm | Success/started | Input | Output | Peak CNY | Seconds |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for arm, row in summary["arms"].items():
        lines.append(
            f"| {arm} | {row['successes']}/{row['started']} | {row['input_tokens']} | "
            f"{row['output_tokens']} | {row['conservative_peak_cny']:.6f} | {row['seconds']:.2f} |"
        )
    lines += ["", "| Task | A | B | C |", "|---|---:|---:|---:|"]
    for row in summary["per_task"]:
        values = [f"{row[a]['successes']}/{row[a]['started']}" for a in ("A", "B", "C")]
        lines.append("| " + " | ".join([row["task_id"], *values]) + " |")
    lines += [
        "",
        "```json",
        json.dumps(summary["comparisons"], indent=2),
        "```",
        "",
        "Known development tasks, exploratory only. Repetitions are clustered by task.",
        "Peak-cache-miss accounting is conservative, not a supplier invoice.",
        "Memory, continuous dialogue and Docker recovery gains were not measured.",
    ]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_json(
        root / "evidence-index.json",
        {
            str(p.relative_to(root)): file_sha(p)
            for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts and p.name != "evidence-index.json"
        },
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "report"))
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--mode", choices=("live", "offline"), default="live")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--max-trials", type=int, default=90)
    args = parser.parse_args(argv)
    root = args.campaign_dir.resolve()
    if args.action == "prepare":
        if args.catalog is None or args.prices is None:
            parser.error("prepare requires --catalog and --prices")
        prepare(args.catalog, root, args.prices, mode=args.mode)
    elif args.action == "run":
        run(root, args.env_file, max_trials=args.max_trials)
    else:
        print(json.dumps(report(root), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
