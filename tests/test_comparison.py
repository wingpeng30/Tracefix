"""Paid-call refusal, fair scheduling and real offline three-arm execution."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tracefix.comparison import (
    TASK_IDS,
    ComparisonBudget,
    digest,
    extract_patch,
    read_json,
    schedule,
    static_material,
    summarize,
    write_json,
)
from tracefix.comparison_campaign import (
    FixtureClient,
    check_protocol,
    execute_trial,
    prepare,
    report,
    run,
)
from tracefix.exceptions import LLMProviderError, PreRequestBudgetExceeded
from tracefix.messages import Message, MessageRole
from tracefix.models.base import LLMConfig

PATCH = """diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return 0
+    return a + b
"""


def provider_config():
    return LLMConfig(
        model_name="deepseek/deepseek-flash",
        max_retries=0,
        max_output_tokens=2048,
        extra_kwargs={
            "api_base": "https://api.deepseek.com",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    )


def make_budget(tmp_path, arm="B", client=None):
    return ComparisonBudget(
        provider_config(),
        root=tmp_path,
        trial={"id": "001", "arm": arm},
        protocol_sha="test",
        client=client or FixtureClient(PATCH),
    )


def messages(content="Fix add"):
    return [Message(role=MessageRole.USER, content=content)]


def test_schedule_balances_every_task_and_position():
    rows = schedule()
    assert len(rows) == 90 and len({r["id"] for r in rows}) == 90
    for task in TASK_IDS:
        batches = [
            [r["arm"] for r in rows if r["task_id"] == task and r["repetition"] == rep]
            for rep in (1, 2, 3)
        ]
        assert all(set(batch) == {"A", "B", "C"} for batch in batches)
        assert all(
            {batch[position] for batch in batches} == {"A", "B", "C"} for position in range(3)
        )


@pytest.mark.parametrize(
    "content", [PATCH, f"```diff\n{PATCH}```", "```patch\n*** Begin Patch\n*** End Patch\n```"]
)
def test_patch_response_parsing(content):
    assert extract_patch(content).endswith("\n")


@pytest.mark.parametrize("content", ["done", f"```diff\n{PATCH}```\n```diff\n{PATCH}```"])
def test_ambiguous_or_empty_patch_refused(content):
    with pytest.raises(ValueError, match="exactly one"):
        extract_patch(content)


def test_static_retrieval_is_deterministic_without_hidden_metadata(tmp_path):
    (tmp_path / "sample.py").write_text("def add(a,b): return 0\n")
    (tmp_path / "test_secret.py").write_text("GOLD_ANSWER")
    outside = tmp_path.parent / (tmp_path.name + "-outside.py")
    outside.write_text("SECRET")
    text = static_material(
        tmp_path, "fix add", ["sample.py", "test_secret.py", "../" + outside.name]
    )
    assert "GOLD_ANSWER" not in text and "SECRET" not in text and "sample.py" in text
    assert text == static_material(tmp_path, "fix add", ["sample.py"])


def test_atomic_write_interruption_preserves_previous(tmp_path, monkeypatch):
    path = tmp_path / "record.json"
    write_json(path, {"old": True})

    def fail(*_args):
        raise OSError("injected replacement failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        write_json(path, {"new": True})
    assert read_json(path) == {"old": True}


def test_budget_records_exact_request_and_decreases_output(tmp_path):
    budget = make_budget(tmp_path)
    budget.complete(messages())
    ledger = budget.ledger()
    assert ledger["requests"][0]["status"] == "completed"
    assert ledger["requests"][0]["output_limit"] == 2048
    assert list((tmp_path / "provider").glob("*-response.json"))
    ledger["requests"][0]["usage"]["output_tokens"] = 7900
    write_json(tmp_path / "requests.json", ledger)
    budget.complete(messages())
    assert budget.config.max_output_tokens == 100


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("input", PreRequestBudgetExceeded),
        ("output", PreRequestBudgetExceeded),
        ("cny", PreRequestBudgetExceeded),
        ("unknown", LLMProviderError),
        ("identity", ValueError),
    ],
)
def test_budget_halts_before_transport(tmp_path, mutation, expected):
    client = FixtureClient(PATCH)
    budget = make_budget(tmp_path, client=client)
    budget.complete(messages())
    ledger = budget.ledger()
    row = ledger["requests"][0]
    if mutation == "input":
        row["usage"]["input_tokens"] = 60000
    elif mutation == "output":
        row["usage"]["output_tokens"] = 8000
    elif mutation == "cny":
        row["peak_cost_cny"] = 20
    elif mutation == "unknown":
        row["status"] = "pending"
    else:
        ledger["protocol_sha256"] = "changed"
    write_json(tmp_path / "requests.json", ledger)
    with pytest.raises(expected):
        budget.complete(messages())
    assert client.calls == 1


def test_transport_failure_is_pending_and_not_replayed(tmp_path):
    class BrokenClient:
        def completion(self, **_kwargs):
            raise ConnectionError("injected disconnect")

    budget = make_budget(tmp_path, client=BrokenClient())
    with pytest.raises(LLMProviderError):
        budget.complete(messages())
    assert read_json(tmp_path / "requests.json")["requests"][0]["status"] == "pending"
    with pytest.raises(LLMProviderError, match="unknown"):
        budget.complete(messages())


def test_bare_one_request_and_provider_reservation_violation(tmp_path):
    budget = make_budget(tmp_path, arm="A")
    budget.complete(messages())
    with pytest.raises(PreRequestBudgetExceeded, match="request/time"):
        budget.complete(messages())

    class BadUsage(FixtureClient):
        def completion(self, **kwargs):
            result = super().completion(**kwargs)
            result["usage"]["completion_tokens"] = 10000
            result["usage"]["total_tokens"] = 10100
            return result

    budget = make_budget(tmp_path / "bad", client=BadUsage(PATCH))
    with pytest.raises(LLMProviderError, match="exceeds"):
        budget.complete(messages())
    assert (
        read_json(tmp_path / "bad" / "requests.json")["requests"][0]["status"]
        == "reservation_violation"
    )


def fixture_catalog(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    (source / "test_sample.py").write_text(
        "from sample import add\ndef test_fix(): assert add(1, 2) == 3\n"
        "def test_regression(): assert callable(add)\n",
        encoding="utf-8",
    )
    for args in (
        ["init"],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
    ):
        subprocess.run(["git", *args], cwd=source, capture_output=True, check=True)
    catalog = tmp_path / "catalog.json"
    write_json(
        catalog,
        [
            {
                "task_id": task,
                "kind": "fixture",
                "source": str(source),
                "python": sys.executable,
                "issue": "Fix sample.add to return a + b",
                "selectors": ["test_sample.py"],
                "fixture_patch": PATCH,
            }
            for task in TASK_IDS
        ],
    )
    prices = tmp_path / "prices.json"
    write_json(
        prices,
        {
            "model": "deepseek-flash",
            "input_peak_cny": 2.0,
            "output_peak_cny": 8.0,
            "kind": "offline_fixture",
        },
    )
    return catalog, prices


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    directory = tmp_path_factory.mktemp("comparison-prepared")
    catalog, prices = fixture_catalog(directory)
    root = directory / "campaign"
    protocol = prepare(catalog, root, prices, mode="offline")
    return root, protocol


def test_three_arms_execute_real_patch_and_independent_pytest(prepared):
    root, protocol = prepared
    for row in protocol["schedule"][:3]:
        result = execute_trial(root, protocol, row, None)
        assert result["passed"], result
    summary = report(root)
    assert summary["started"] == 3 and not summary["complete"] and summary["mode"] == "offline"
    assert all(r["successes"] == 1 for r in summary["arms"].values())
    requests_before = read_json(root / "requests.json")
    assert len(run(root, max_trials=3)) == 3
    assert read_json(root / "requests.json") == requests_before
    assert check_protocol(root)["product_base"] == protocol["product_base"]


def test_separate_process_reentry_and_evidence_only_report(prepared):
    root, _ = prepared
    command = [
        sys.executable,
        "-m",
        "tracefix.comparison_campaign",
        "run",
        "--campaign-dir",
        str(root),
        "--max-trials",
        "3",
    ]
    environment = os.environ | {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    result = subprocess.run(
        command, cwd=root.parent, env=environment, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        command[:3] + ["report", "--campaign-dir", str(root)],
        cwd=root.parent,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_protocol_rejects_artifact_change(prepared):
    root, protocol = prepared
    path = Path(protocol["tasks"][TASK_IDS[0]]["prompt_path"])
    old = path.read_bytes()
    try:
        path.write_text("tampered", encoding="utf-8")
        with pytest.raises(ValueError, match="artifact"):
            check_protocol(root)
    finally:
        path.write_bytes(old)


def test_clustered_summary_zero_baseline_and_incomplete_trials():
    protocol = {"mode": "offline"}
    rows = [{**row, "finished": True, "passed": row["arm"] == "C"} for row in schedule()]
    result = summarize(protocol, rows, [])
    assert result["complete"] and result["comparisons"]["C-A"]["matched_tasks"] == 10
    assert result["comparisons"]["C-A"]["percentage_point_difference"] == 100
    assert result["comparisons"]["C-A"]["relative_improvement"] is None
    assert result == summarize(protocol, rows, [])
    rows[0]["finished"] = False
    assert not summarize(protocol, rows, [])["complete"]
    assert digest(protocol) == digest({"mode": "offline"})


def test_complete_offline_schedule_and_reuse_without_extra_requests(prepared):
    root, _ = prepared
    results = run(root)
    assert len(results) == 90 and all(r["passed"] for r in results)
    summary = report(root)
    assert summary["complete"] and summary["mode"] == "offline"
    assert all(row["successes"] == 30 for row in summary["arms"].values())
    before = read_json(root / "requests.json")
    assert len(run(root)) == 90
    assert read_json(root / "requests.json") == before
