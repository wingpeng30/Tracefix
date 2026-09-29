from __future__ import annotations

from scripts.reverify_frozen_patches import isolate_product_patch
from scripts.summarize_frozen_reverification import classify_replay, stable_qualification


def test_isolate_product_patch_removes_only_known_runner_files(tmp_path) -> None:
    source = tmp_path / "frozen.diff"
    audit = ".tracefix-test-tmp/agent-audit-" + "a" * 32 + ".json"
    source.write_text(
        f"diff --git a/{audit} b/{audit}\n"
        "new file mode 100644\n--- /dev/null\n+++ b/" + audit + "\n+{}\n"
        "diff --git a/.tracefix-product.py b/.tracefix-product.py\n"
        "new file mode 100644\n--- /dev/null\n+++ b/.tracefix-product.py\n+VALUE=1\n",
        encoding="utf-8",
    )
    output = tmp_path / "product.diff"
    result = isolate_product_patch(source, output)
    assert result["removed_runner_files"] == [audit]
    assert audit not in output.read_text(encoding="utf-8")
    assert ".tracefix-product.py" in output.read_text(encoding="utf-8")


def test_summary_rejects_missing_or_inconsistent_repeat_evidence() -> None:
    valid = {
        "eligible_for_llm_prescreen": True,
        "initial_evidence": {
            "status": "assertion_failed",
            "executed_node_ids": ["test_a.py::test_a"],
        },
        "gold_evidence": {"status": "passed", "executed_node_ids": ["test_a.py::test_a"]},
        "dependency_drift_detected": False,
    }
    assert not stable_qualification([{"repeat": 1, "result": valid}])
    changed = {
        **valid,
        "gold_evidence": {"status": "passed", "executed_node_ids": ["test_a.py::test_b"]},
    }
    assert not stable_qualification(
        [{"repeat": 1, "result": valid}, {"repeat": 2, "result": changed}]
    )
    assert classify_replay([{"repeat": 1, "result": {"eligible": True}}]) == "unresolved"
