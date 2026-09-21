import json
from pathlib import Path

from tracefix.holdout import freeze_holdout, freeze_long_context_mechanism


def test_freeze_holdout_uses_order_and_only_ordinary_qualification(tmp_path) -> None:
    pool = {
        "requested_repositories": ["org/repo"],
        "selection_seed": 20260921,
        "candidate_order_sha256": "abc",
        "candidate_order": ["one", "two", "three"],
        "selected": [
            {"instance_id": "one", "repo": "org/repo"},
            {"instance_id": "two", "repo": "org/repo"},
            {"instance_id": "three", "repo": "org/repo"},
        ],
    }
    behavior = [
        {"task_id": "one", "eligible_for_llm_prescreen": False},
        {"task_id": "two", "reviewed_collection_failure": True},
        {"task_id": "three", "eligible_for_llm_prescreen": True},
    ]
    pool_path = tmp_path / "pool.json"
    report_path = tmp_path / "behavior.json"
    pool_path.write_text(json.dumps(pool), encoding="utf-8")
    report_path.write_text(json.dumps(behavior), encoding="utf-8")
    result = freeze_holdout(pool_path, report_path, tmp_path / "freeze.json", per_repository=1)
    assert result["selected_task_ids"] == ["three"]
    assert [item["category"] for item in result["decisions"]] == [
        "not_qualified",
        "reviewed_collection_failure",
        "ordinary_qualified",
    ]


def test_freeze_long_context_replays_two_independent_mechanism_tasks(tmp_path) -> None:
    result = freeze_long_context_mechanism(
        Path(__file__).parents[1] / "benchmarks" / "long_context_tasks",
        tmp_path / "work",
        tmp_path / "mechanism.json",
    )
    assert result["valid"] is True
    assert len(result["tasks"]) == 2
    assert all(item["contract_count"] == 24 for item in result["tasks"])
    assert all(item["control"]["compacted"] is False for item in result["tasks"])
    assert all(item["treatment"]["compacted"] is True for item in result["tasks"])
