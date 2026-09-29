"""Run TraceFix's engineering checks and preserve their original evidence.

The pytest process return code and the acceptance decision are recorded
separately. Coverage thresholds are calculated from Coverage.py JSON counts,
not its rounded terminal display.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _pytest_acceptance(
    coverage: dict[str, Any], junit_path: Path, process_code: int
) -> dict[str, Any]:
    totals = coverage.get("totals")
    if not isinstance(totals, dict):
        raise ValueError("coverage JSON is missing totals")
    statements = int(totals.get("num_statements", -1))
    covered_statements = int(totals.get("covered_lines", -1))
    branches = int(totals.get("num_branches", -1))
    covered_branches = int(totals.get("covered_branches", -1))
    if min(statements, covered_statements, branches, covered_branches) < 0:
        raise ValueError("coverage JSON contains invalid coverage counts")
    if covered_statements > statements or covered_branches > branches:
        raise ValueError("coverage JSON has more covered items than measured items")
    denominator = statements + branches
    numerator = covered_statements + covered_branches
    if denominator == 0:
        raise ValueError("coverage JSON contains no measurable statements or branches")
    root = ET.parse(junit_path).getroot()
    suites = [root] if root.tag == "testsuite" else [
        child for child in root if child.tag == "testsuite"
    ]
    failures = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
    tests = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
    exact_percent = numerator * 100 / denominator
    return {
        "process_return_code": process_code,
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "covered_statements": covered_statements,
        "statements": statements,
        "covered_branches": covered_branches,
        "branches": branches,
        "coverage_numerator": numerator,
        "coverage_denominator": denominator,
        "coverage_percent_exact": exact_percent,
        "coverage_pass": numerator * 100 >= 90 * denominator,
        "tests_pass": tests > 0 and failures == 0 and errors == 0,
        "accepted": (
            process_code == 0
            and tests > 0
            and failures == 0
            and errors == 0
            and numerator * 100 >= 90 * denominator
        ),
    }


def _run(
    name: str,
    command: list[str],
    cwd: Path,
    output: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.time()
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env or os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    (output / f"{name}.log").write_text(completed.stdout, encoding="utf-8")
    return {
        "command": command,
        "process_return_code": completed.returncode,
        "duration_seconds": time.time() - started,
        "log": f"{name}.log",
    }


def _resolve_diff_base(root: Path, requested: str) -> tuple[str | None, str | None]:
    """Resolve a caller-supplied Git commit without silently choosing a fallback."""
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{requested}^{{commit}}"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        return None, completed.stdout.strip() or f"cannot resolve diff base {requested!r}"
    return completed.stdout.strip(), None


def _coverage_gaps(coverage: dict[str, Any]) -> list[dict[str, Any]]:
    """Return line and branch gaps so CI artifacts can direct follow-up tests."""
    gaps = []
    for filename, report in sorted(coverage.get("files", {}).items()):
        missing_lines = report.get("missing_lines", [])
        missing_branches = report.get("missing_branches", [])
        if missing_lines or missing_branches:
            gaps.append(
                {
                    "file": filename,
                    "missing_lines": missing_lines,
                    "missing_branches": missing_branches,
                }
            )
    return gaps


def _coverage_gap_summary(gaps: list[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    """Keep the largest uncovered paths visible in CI logs as well as artifacts."""
    ranked = [
        {
            "file": item["file"],
            "missing_line_count": len(item["missing_lines"]),
            "missing_branch_count": len(item["missing_branches"]),
            "missing_lines": item["missing_lines"],
            "missing_branches": item["missing_branches"],
        }
        for item in gaps
    ]
    ranked.sort(
        key=lambda item: (
            -(item["missing_line_count"] + item["missing_branch_count"]),
            item["file"],
        )
    )
    return ranked[:limit]


def _python_files(root: Path) -> list[str]:
    """Return repository source, collected tests, and the supported CLI scripts.

    This intentionally excludes benchmark/task data and arbitrary untracked
    workspaces which can live beside the repository during qualification.
    """
    tracked = subprocess.check_output(
        ["git", "ls-files", "--cached", "--", "src", "tests", "scripts"],
        cwd=root,
        text=True,
    ).splitlines()
    expected_new = {
        "scripts/audit_docker_smoke.py",
        "scripts/check_engineering.py",
        "scripts/reproduce_zero_call.py",
        "src/tracefix/agent_bridge.py",
        "src/tracefix/reproduction.py",
        "src/tracefix/report.py",
        "tests/test_audit_docker_smoke.py",
        "tests/test_engineering_check.py",
        "tests/test_agent_bridge.py",
        "tests/test_reproduction.py",
        "tests/test_report.py",
    }
    paths = set(tracked)
    paths.update(path for path in expected_new if (root / path).is_file())
    return sorted(
        path for path in paths
        if path.endswith(".py")
        and (
            path.startswith("src/")
            or (path.startswith("tests/") and Path(path).name.startswith("test_"))
            or (path.startswith("scripts/") and "/" not in path.removeprefix("scripts/"))
        )
    )


def run(output: Path, root: Path, python: str, diff_base: str = "HEAD^") -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    started_at = datetime.now(UTC)
    coverage_json = output / "coverage.json"
    coverage_xml = output / "coverage.xml"
    junit_xml = output / "pytest.junit.xml"
    coverage_data = output / ".coverage"
    coverage_data.unlink(missing_ok=True)
    checks: dict[str, Any] = {
        "python_executable": python,
        "python_version": subprocess.check_output(
            [python, "--version"], text=True, stderr=subprocess.STDOUT
        ).strip(),
        "platform": platform.platform(),
        "working_tree": str(root),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "tracked_working_tree_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, text=True
            ).strip()
        ),
        "started_at_utc": started_at.isoformat(),
        "checks": {},
    }
    pytest_command = [
        python,
        "-m",
        "pytest",
        "-q",
        "--cov=tracefix",
        "--cov-branch",
        "-p",
        "no:cacheprovider",
        f"--basetemp={output / 'pytest-tmp'}",
        "--cov-report=term-missing",
        f"--cov-report=json:{coverage_json}",
        f"--cov-report=xml:{coverage_xml}",
        f"--junitxml={junit_xml}",
    ]
    pytest_env = os.environ.copy()
    pytest_env["COVERAGE_FILE"] = str(coverage_data)
    checks["checks"]["pytest"] = _run(
        "pytest", pytest_command, root, output, env=pytest_env
    )
    pytest_run = checks["checks"]["pytest"]
    if not coverage_json.is_file() or not junit_xml.is_file() or not coverage_xml.is_file():
        checks["pytest_acceptance"] = {
            "accepted": False,
            "reason": "pytest coverage or JUnit report is missing",
            "process_return_code": pytest_run["process_return_code"],
        }
    else:
        try:
            checks["pytest_acceptance"] = _pytest_acceptance(
                json.loads(coverage_json.read_text(encoding="utf-8")),
                junit_xml,
                pytest_run["process_return_code"],
            )
            coverage_gaps = _coverage_gaps(
                json.loads(coverage_json.read_text(encoding="utf-8"))
            )
            (output / "coverage-gaps.json").write_text(
                json.dumps(coverage_gaps, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            checks["coverage_gap_summary"] = _coverage_gap_summary(coverage_gaps)
        except (ValueError, OSError, ET.ParseError, TypeError) as exc:
            checks["pytest_acceptance"] = {
                "accepted": False,
                "reason": f"invalid pytest evidence: {exc}",
                "process_return_code": pytest_run["process_return_code"],
            }
    source_files = _python_files(root)
    checks["checks"]["ruff"] = _run(
        "ruff", [python, "-m", "ruff", "check", *source_files], root, output
    )
    checks["checks"]["compileall"] = _run(
        "compileall", [python, "-m", "py_compile", *source_files], root, output
    )
    resolved_diff_base, diff_base_error = _resolve_diff_base(root, diff_base)
    checks["diff_base"] = {
        "requested": diff_base,
        "resolved_commit": resolved_diff_base,
        "error": diff_base_error,
    }
    if resolved_diff_base is None:
        (output / "diff-check.log").write_text(diff_base_error + "\n", encoding="utf-8")
        checks["checks"]["diff_check"] = {
            "command": ["git", "rev-parse", "--verify", diff_base],
            "process_return_code": 128,
            "duration_seconds": 0.0,
            "log": "diff-check.log",
        }
    else:
        checks["checks"]["diff_check"] = _run(
            "diff-check",
            ["git", "diff", "--check", f"{resolved_diff_base}...HEAD"],
            root,
            output,
        )
    checks["accepted"] = bool(checks["pytest_acceptance"].get("accepted")) and all(
        item["process_return_code"] == 0 for item in checks["checks"].values()
    )
    checks["finished_at_utc"] = datetime.now(UTC).isoformat()
    checks["evidence_sha256"] = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.iterdir())
        if path.is_file() and path.name != "summary.json"
    }
    (output / "summary.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--diff-base",
        default="HEAD^",
        help="Git commit/ref used as the left side of the whitespace diff check",
    )
    args = parser.parse_args(argv)
    report = run(args.output.resolve(), args.root.resolve(), args.python, args.diff_base)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
