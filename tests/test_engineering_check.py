from __future__ import annotations

import xml.etree.ElementTree as ET

from scripts.check_engineering import _pytest_acceptance


def _reports(tmp_path, *, tests: int, failures: int = 0, errors: int = 0):
    junit = tmp_path / "junit.xml"
    ET.ElementTree(
        ET.Element(
            "testsuite",
            tests=str(tests),
            failures=str(failures),
            errors=str(errors),
            skipped="0",
        )
    ).write(junit, encoding="utf-8", xml_declaration=True)
    coverage = {
        "totals": {
            "num_statements": 100,
            "covered_lines": 89,
            "num_branches": 20,
            "covered_branches": 18,
        }
    }
    return coverage, junit


def test_gate_uses_unrounded_statement_and_branch_counts(tmp_path):
    coverage, junit = _reports(tmp_path, tests=10)
    result = _pytest_acceptance(coverage, junit, 0)
    assert result["coverage_numerator"] == 107
    assert result["coverage_denominator"] == 120
    assert result["coverage_percent_exact"] == 107 / 120 * 100
    assert result["coverage_pass"] is False
    assert result["accepted"] is False


def test_success_requires_both_coverage_and_pytest_return_code(tmp_path):
    coverage, junit = _reports(tmp_path, tests=10)
    coverage["totals"]["covered_lines"] = 90
    result = _pytest_acceptance(coverage, junit, 0)
    assert result["coverage_pass"] is True
    assert result["accepted"] is True
    failed_process = _pytest_acceptance(coverage, junit, 1)
    assert failed_process["accepted"] is False


def test_junit_failures_and_empty_suite_reject_acceptance(tmp_path):
    coverage, junit = _reports(tmp_path, tests=10, failures=1)
    coverage["totals"]["covered_lines"] = 90
    assert _pytest_acceptance(coverage, junit, 0)["accepted"] is False
    _, empty_junit = _reports(tmp_path, tests=0)
    assert _pytest_acceptance(coverage, empty_junit, 0)["accepted"] is False


def test_missing_or_zero_coverage_denominator_is_rejected(tmp_path):
    _, junit = _reports(tmp_path, tests=10)
    try:
        _pytest_acceptance({}, junit, 0)
    except ValueError as exc:
        assert "totals" in str(exc)
    else:
        raise AssertionError("missing totals were accepted")
    zero = {
        "totals": {
            "num_statements": 0,
            "covered_lines": 0,
            "num_branches": 0,
            "covered_branches": 0,
        }
    }
    try:
        _pytest_acceptance(zero, junit, 0)
    except ValueError as exc:
        assert "no measurable" in str(exc)
    else:
        raise AssertionError("empty coverage was accepted")
