"""Qualify the frozen more-itertools task without installing dependencies or calling a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from tracefix import AgentConfig, RunConfig, ToolCall, TraceFixRunner
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.onboarding import export_patch, verify_patch
from tracefix.provenance import inspect_test_environment
from tracefix.regression import _status, _target_path, _test
from tracefix.regression_replay import forbid_live_access
from tracefix.report import render_report

PACKAGE = (
    Path(__file__).resolve().parents[1]
    / "benchmarks/public_tasks/more-itertools-windowed-empty-462"
)


def git(repo: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
    )
    return (
        subprocess.check_output(
            ["git", *arguments],
            cwd=repo,
            env=environment,
        )
        .decode("utf-8")
        .replace("\r\n", "\n")
    )


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def upstream_state(repo: Path) -> dict:
    tracked = git(repo, "ls-files", "-z").strip("\0").split("\0")
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "status": git(repo, "status", "--porcelain"),
        "tracked_files": {
            name: sha(repo / name) if (repo / name).is_file() else "missing"
            for name in tracked
            if name
        },
    }


def source_state(repo: Path) -> dict:
    files = git(repo, "ls-files", "-z").strip("\0").split("\0")
    hashes = {name: sha(repo / name) for name in sorted(files)}
    return {
        "commit": git(repo, "rev-parse", "HEAD").strip(),
        "files_sha256": hashes,
        "source_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "diff_sha256": hashlib.sha256(git(repo, "diff", "HEAD").encode()).hexdigest(),
    }


def load_manifest(package: Path = PACKAGE) -> dict:
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported task manifest version")
    for name, expected in manifest["files_sha256"].items():
        path = (package / name).resolve()
        if not path.is_relative_to(package.resolve()) or sha(path) != expected:
            raise ValueError(f"task package identity mismatch: {name}")
    return manifest


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def apply_product(workspace: Path, product_patch: str) -> None:
    from tracefix.tools.builtin import ApplyPatchTool

    result = ApplyPatchTool(workspace).execute(
        ToolCall(
            id="qualification-product-patch",
            name="apply_patch",
            arguments={"patch": product_patch},
        )
    )
    if not result.success:
        raise ValueError(f"product patch rejected: {result.error}")


def run_case(
    source: Path,
    directory: Path,
    manifest: dict,
    python: Path,
    product_patch: str | None,
    expected: tuple[str, str],
) -> dict:
    from tracefix.tools.builtin import RunTestsTool

    workspace = TraceFixRunner._clone_repository(source, directory / "workspace")
    config = RunConfig(
        repo=source,
        task="Qualification only",
        output_dir=directory / "runs",
        env_file=None,
        test_target=manifest["task_target"],
        source_import=manifest["source_import"],
        test_python_executable=python,
    )
    preparation = TraceFixRunner._prepare_workspace(config, workspace, directory)
    record = {
        "preparation": preparation,
        "targets": [],
        "passed": False,
        "environment": inspect_test_environment(python).model_dump(mode="json"),
    }
    try:
        if not preparation.get("success"):
            raise ValueError(f"environment preflight failed: {preparation.get('failure')}")
        if product_patch:
            apply_product(workspace, product_patch)
        record["source_before"] = source_state(workspace)
        targets = [manifest["task_target"], *manifest["regression_targets"]]
        tool = RunTestsTool(
            workspace,
            python_executable=python,
            evidence_dir=directory / "test-0",
            max_output_chars=1_000_000,
        )
        for index, (target, wanted) in enumerate(zip(targets, expected, strict=True)):
            _target_path(workspace, target)
            evidence = directory / f"test-{index}"
            test = _test(tool, target, evidence)
            evidence.mkdir(parents=True, exist_ok=True)
            write_json(evidence / "result.json", test)
            for channel in ("stdout", "stderr"):
                (evidence / f"{channel}.txt").write_text(
                    test.get("output", {}).get(channel, ""),
                    encoding="utf-8",
                )
            status = _status(test)
            row = {"target": target, "expected": wanted, "status": status, "test": test}
            record["targets"].append(row)
            count = test.get("output", {}).get("test_counts", {}).get("tests")
            if count != manifest["target_counts"][target]:
                raise ValueError(f"{target}: expected complete test collection, got {count}")
            counts = test.get("output", {}).get("test_counts", {})
            if counts.get("skipped", 0) or counts.get("errors", 0):
                raise ValueError(f"{target}: qualification cannot contain skips or errors")
            if status != wanted or test.get("output", {}).get("truncated"):
                raise ValueError(f"{target}: expected {wanted}, got {status}; inspect raw evidence")
        record["source_after"] = source_state(workspace)
        if record["source_before"] != record["source_after"]:
            raise ValueError("pytest changed frozen source")
        if (
            inspect_test_environment(python).fingerprint_sha256
            != (record["environment"]["fingerprint_sha256"])
        ):
            raise ValueError("test environment changed during qualification case")
        record["passed"] = True
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        write_json(directory / "record.json", record)
    return record


class ReferenceClient:
    """Reference-patch replay; deliberately provides no autonomous repair evidence."""

    def __init__(self, product_patch: str) -> None:
        self.product_patch = product_patch
        self.calls = 0

    def token_counter(self, **_kwargs):
        return 100

    def completion(self, **kwargs):
        if not kwargs.get("tools") or not kwargs.get("messages"):
            raise AssertionError("production schema/messages missing")
        self.calls += 1
        if self.calls > 2:
            raise AssertionError("unexpected reference replay request")
        calls = (
            [
                {
                    "id": "reference-patch",
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps({"patch": self.product_patch}),
                    },
                }
            ]
            if self.calls == 1
            else []
        )
        return {
            "model": "offline/reference-patch",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": calls,
                        "content": None if calls else "Reference replay completed",
                    },
                    "finish_reason": "tool_calls" if calls else "stop",
                }
            ],
        }


def replay(source: Path, output: Path, manifest: dict, python: Path, product_patch: str) -> dict:
    from tracefix.cli import _ordinary_settings, build_parser

    config_file = output / "replay.toml"
    config_file.write_text(
        '[run]\nmodel = "offline/reference-patch"\n'
        f'test_target = "{manifest["task_target"]}"\n'
        f'source_import = "{manifest["source_import"]}"\n'
        'regression_targets = ["tests/test_more.py::WindowedTests"]\n'
        "max_steps = 4\nmax_test_runs = 4\nwall_time_seconds = 300\n",
        encoding="utf-8",
    )
    settings = _ordinary_settings(
        build_parser().parse_args(
            [
                "doctor",
                "--config",
                str(config_file),
                "--repo",
                str(source),
                "--test-python",
                str(python),
                "--model",
                "offline/reference-patch",
                "--output-dir",
                str(output / "runs"),
                "--test-target",
                manifest["task_target"],
                "--source-import",
                manifest["source_import"],
            ]
        )
    )
    client = ReferenceClient(product_patch)
    result = TraceFixRunner(llm_factory=lambda config: LiteLLMAdapter(config, client=client)).run(
        RunConfig(
            repo=source,
            task=(PACKAGE / "task.md").read_text(encoding="utf-8"),
            output_dir=settings["output_dir"],
            env_file=None,
            model_name=settings["model_name"],
            test_target=settings["test_target"],
            source_import=settings["source_import"],
            regression_targets=settings["regression_targets"],
            test_python_executable=python,
            agent_config=AgentConfig(max_steps=4, max_test_runs=4, wall_time_seconds=300),
        )
    )
    if result.status.value != "completed" or result.validation_gate_status != "passed":
        raise ValueError(f"reference replay failed: {result.result_path}")
    run = Path(result.result_path).parent
    verification = verify_patch(run)
    if not verification["passed"]:
        raise ValueError("reference independent verification failed")
    report = render_report(run, output / "report.html")
    exported = export_patch(run, output / "reference-export.patch")
    return {
        "kind": "reference_patch_replay_not_autonomous_repair",
        "model_requests": client.calls,
        "usage_kind": "simulated",
        "supplier_cost": None,
        "result_path": result.result_path,
        "validation_gate_status": result.validation_gate_status,
        "test_runs": result.test_runs,
        "verification": verification["record_path"],
        "report": str(report),
        "export": str(exported),
    }


def qualify(source_repo: Path, python: Path, output: Path) -> dict:
    source_repo = source_repo.expanduser().resolve()
    output = output.expanduser().resolve()
    if output.is_relative_to(source_repo):
        raise ValueError("qualification output must be outside upstream repository")
    output.mkdir(parents=True, exist_ok=False)
    record = {"schema_version": 1, "qualified": False, "cases": [], "output": str(output)}
    original = None
    try:
        manifest = load_manifest()
        python = python.expanduser().resolve()
        original = upstream_state(source_repo)
        record.update(
            {
                "task_manifest_sha256": sha(PACKAGE / "manifest.json"),
                "upstream_base": manifest["base_commit"],
                "reference_commit": manifest["reference_commit"],
                "tracefix_commit": git(PACKAGE.parent, "rev-parse", "HEAD").strip(),
                "tracefix_dirty": bool(
                    git(PACKAGE.parent, "status", "--porcelain", "--untracked-files=no")
                ),
                "qualification_script_sha256": sha(Path(__file__)),
                "test_python": str(python),
                "environment": inspect_test_environment(python).model_dump(mode="json"),
            }
        )
        for commit, expected in (
            (manifest["base_commit"], manifest["base_product_blob"]),
            (manifest["reference_commit"], manifest["reference_product_blob"]),
        ):
            if (
                git(source_repo, "rev-parse", f"{commit}:{manifest['product_path']}").strip()
                != expected
            ):
                raise ValueError("upstream product identity mismatch")
        reference = (PACKAGE / "reference-product.patch").read_text(encoding="utf-8")
        actual_reference = git(
            source_repo,
            "diff",
            "--unified=1",
            manifest["base_commit"],
            manifest["reference_commit"],
            "--",
            manifest["product_path"],
        )
        if actual_reference != reference:
            raise ValueError("reference product patch differs from frozen upstream diff")
        with forbid_live_access():
            prepared = output / "prepared-source"
            git(output, "clone", "--quiet", "--no-checkout", str(source_repo), str(prepared))
            git(prepared, "config", "core.autocrlf", "false")
            git(prepared, "checkout", "--quiet", "--detach", manifest["base_commit"])
            git(prepared, "apply", "--index", str(PACKAGE / "prepare-tests.patch"))
            staged = git(prepared, "diff", "--cached", "--name-only").splitlines()
            if staged != [manifest["task_target"]]:
                raise ValueError("preparation changed files outside new task test")
            git(
                prepared,
                "-c",
                "user.name=TraceFix task",
                "-c",
                "user.email=task@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "Add public windowed empty-input contract",
            )
            record["prepared_source"] = source_state(prepared)
            for name, product_patch, expected in (
                ("base-1", None, ("failed", "passed")),
                ("base-2", None, ("failed", "passed")),
                ("reference-1", reference, ("passed", "passed")),
                ("reference-2", reference, ("passed", "passed")),
                (
                    "diagnostic-wrong",
                    (PACKAGE / "diagnostic-wrong.patch").read_text(encoding="utf-8"),
                    ("passed", "failed"),
                ),
            ):
                case = run_case(prepared, output / name, manifest, python, product_patch, expected)
                if (
                    case["environment"]["fingerprint_sha256"]
                    != (record["environment"]["fingerprint_sha256"])
                ):
                    raise ValueError("test environment changed between qualification cases")
                record["cases"].append(
                    {
                        "name": name,
                        "passed": case["passed"],
                        "record": str(output / name / "record.json"),
                    }
                )
                write_json(output / "qualification.json", record)
            record["replay"] = replay(prepared, output, manifest, python, reference)
            if (
                inspect_test_environment(python).fingerprint_sha256
                != (record["environment"]["fingerprint_sha256"])
            ):
                raise ValueError("test environment changed during reference replay")
            if git(prepared, "status", "--porcelain").strip():
                raise ValueError("reference replay changed prepared source")
            record["qualified"] = True
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if original is not None:
            current = upstream_state(source_repo)
            record["upstream_unchanged"] = original == current
            if original != current:
                record["qualified"] = False
                record["error"] = "source repository changed"
        record["evidence_sha256"] = {
            str(path.relative_to(output)): sha(path)
            for path in sorted(output.rglob("*"))
            if path.is_file()
            and ".git" not in path.parts
            and "workspace" not in path.parts
            and "prepared-source" not in path.parts
            and path.name != "qualification.json"
            and "__pycache__" not in path.parts
        }
        write_json(output / "qualification.json", record)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--test-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = qualify(args.source_repo, args.test_python, args.output)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
