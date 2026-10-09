"""Independent A/C funding, task expansion and paired reporting without vendor calls."""

from collections import Counter

import pytest

from tracefix.comparison import file_sha, read_json, schedule, summarize, write_json
from tracefix.comparison_funding import freeze_new_budget, prior_spend
from tracefix.comparison_holdout import HOLDOUT_TASK_IDS
from tracefix.comparison_profiles import AC_HOLDOUT_PROFILE, BC_HOLDOUT_PROFILE, profile_config
from tracefix.comparison_selection import next_candidate, validate_selection


def authorization(path):
    write_json(
        path,
        {
            "kind": "tracefix_paid_authorization",
            "profile": AC_HOLDOUT_PROFILE,
            "limit_cny": 300.0,
            "authorization_id": "explicit-new-test-authorization",
            "authorization_path": str(path.resolve()),
            "user_instruction": "new 300 CNY",
        },
    )


def test_ac_schedule_once_and_task_level_statistics():
    selected = profile_config(AC_HOLDOUT_PROFILE)
    tasks = [*HOLDOUT_TASK_IDS, *(f"new__task-{i}" for i in range(10))]
    rows = schedule(selected, tasks)
    assert len(rows) == 60 and rows == schedule(selected, tasks)
    assert Counter(tuple(r["arm"] for r in rows[i : i + 2]) for i in range(0, 60, 2)) == {
        ("A", "C"): 15,
        ("C", "A"): 15,
    }
    assert set(Counter((r["task_id"], r["arm"]) for r in rows).values()) == {1}
    assert selected["funding_mode"] == "new_authorization"
    records = [
        {**r, "finished": True, "status": "completed", "passed": r["arm"] == "C", "seconds": 1}
        for r in rows
    ]
    protocol = {
        "mode": "offline",
        "profile": selected,
        "tasks": {t: {} for t in tasks},
        "schedule": rows,
    }
    result = summarize(protocol, records, [])
    assert result["complete"] and set(result["comparisons"]) == {"C-A"}
    assert result["task_cohorts"]["original_twenty"]["planned_tasks"] == 20
    assert result["task_cohorts"]["additional_ten"]["comparisons"]["C-A"]["matched_tasks"] == 10
    contrast = result["comparisons"]["C-A"]
    assert contrast["matched_tasks"] == 30 and contrast["percentage_point_difference"] == 100
    assert contrast["bootstrap_95_percent_points"] == [100, 100]
    assert contrast["cost_per_success_ratio"] is None
    partial = summarize(protocol, records[:3], [])
    assert partial["started"] == 3 and partial["unstarted"] == 57
    assert partial["comparisons"]["C-A"]["matched_tasks"] == 1
    with pytest.raises(ValueError):
        schedule(selected, tasks[:-1])
    with pytest.raises(ValueError):
        schedule(selected, tasks[:-1] + [tasks[0]])


def test_new_authorization_is_independent_claimed_once_and_tamper_evident(tmp_path):
    path = tmp_path / "authorization.json"
    root = tmp_path / "campaign"
    authorization(path)
    funding = freeze_new_budget(path, root)
    protocol = {"profile": profile_config(AC_HOLDOUT_PROFILE), "funding": funding}
    assert prior_spend(protocol) == 0
    assert funding == freeze_new_budget(path, root)
    with pytest.raises(ValueError, match="already claimed"):
        freeze_new_budget(path, tmp_path / "second-campaign")
    copied = tmp_path / "copied.json"
    copied.write_bytes(path.read_bytes())
    with pytest.raises(ValueError, match="explicit"):
        freeze_new_budget(copied, root)
    with pytest.raises(ValueError, match="identity"):
        prior_spend({"profile": profile_config(BC_HOLDOUT_PROFILE), "funding": funding})
    for field, value in (
        ("authorization_cny", 301),
        ("prior_conservative_cny", 79),
        ("claim_sha256", "wrong"),
        ("authorization_id", "wrong"),
    ):
        with pytest.raises(ValueError, match="identity"):
            prior_spend({**protocol, "funding": {**funding, field: value}})
    original = read_json(path)
    write_json(path, {**original, "user_instruction": "changed"})
    with pytest.raises(ValueError, match="identity"):
        prior_spend(protocol)
    write_json(path, original)
    write_json(path.with_suffix(".claim.json"), {"campaign": "changed"})
    with pytest.raises(ValueError, match="identity"):
        prior_spend(protocol)


def test_live_prepare_rejects_missing_or_ambiguous_authorization_before_counting(tmp_path):
    from tracefix.comparison_campaign import prepare

    for kwargs, reason in (
        ({"profile": AC_HOLDOUT_PROFILE}, "explicit new authorization"),
        ({"profile": AC_HOLDOUT_PROFILE, "budget_parent_campaign": tmp_path}, "inherit"),
        ({"profile": BC_HOLDOUT_PROFILE, "authorization": tmp_path / "missing"}, "only supported"),
        ({"profile": BC_HOLDOUT_PROFILE, "task_freeze": tmp_path / "missing"}, "only supported"),
    ):
        with pytest.raises(ValueError, match=reason):
            prepare(
                tmp_path / "missing-catalog",
                tmp_path / "campaign",
                tmp_path / "missing-prices",
                **kwargs,
            )
    assert not (tmp_path / "campaign" / "requests.json").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"limit_cny": 50},
        {"authorization_id": ""},
        {"kind": "old"},
        {"profile": BC_HOLDOUT_PROFILE},
        {"user_instruction": ""},
    ],
)
def test_fresh_budget_requires_explicit_matching_authorization(tmp_path, change):
    path = tmp_path / "authorization.json"
    authorization(path)
    write_json(path, {**read_json(path), **change})
    with pytest.raises(ValueError, match="explicit"):
        freeze_new_budget(path, tmp_path / "campaign")
    assert not path.with_suffix(".claim.json").exists()
    inside = tmp_path / "inside.json"
    authorization(inside)
    with pytest.raises(ValueError, match="outside"):
        freeze_new_budget(inside, tmp_path)


def test_selection_replays_balanced_order_failures_exclusions_and_evidence(tmp_path):
    old = list(HOLDOUT_TASK_IDS)
    new = [f"pytest-dev__pytest-new-{i}" for i in range(11)]
    rows = [{"instance_id": t, "repo": t.split("__")[0]} for t in old + new]
    pool = {
        "selected": rows,
        "candidate_order": old + new,
        "requested_repositories": sorted({r["repo"] for r in rows}),
        "excluded_task_ids": [],
        "revision": "78f471bf655a3137b2e8a75af1501690ec009ec3",
    }
    path = tmp_path / "pool.json"
    write_json(path, pool)
    selected, attempts = old[:], []
    attempted = set()
    for i in range(11):
        task = next_candidate(pool, selected, attempted, set())
        attempted.add(task)
        evidence = tmp_path / f"qualification-{i}.json"
        write_json(
            evidence,
            {
                "task_id": task,
                "kind": "real",
                "qualification": {
                    "eligible_for_llm_prescreen": True,
                    "qualification_type": "assertion_failure",
                },
            },
        )
        attempts.append(
            {
                "task_id": task,
                "accepted": i != 0,
                "evidence_path": str(evidence),
                "evidence_sha256": file_sha(evidence),
            }
        )
        if i != 0:
            selected.append(task)
    freeze = {
        "kind": "tracefix_ac_thirty_selection",
        "selected_task_ids": selected,
        "pool_path": str(path),
        "pool_sha256": file_sha(path),
        "excluded_task_ids": [],
        "attempts": attempts,
    }
    validate_selection(freeze, selected)
    with pytest.raises(ValueError, match="exhausted"):
        next_candidate(pool, selected, attempted, set())
    assert next_candidate(pool, old, set(), {new[0]}) == new[1]
    with pytest.raises(ValueError, match="order"):
        validate_selection({**freeze, "attempts": list(reversed(attempts))}, selected)
    with pytest.raises(ValueError, match="establish"):
        validate_selection({**freeze, "attempts": attempts[:-1]}, selected)
    with pytest.raises(ValueError, match="identity"):
        validate_selection(freeze, selected[:-1])
    evidence.write_text("broken")
    with pytest.raises(ValueError, match="evidence changed"):
        validate_selection(freeze, selected)
    write_json(path, {**pool, "revision": "changed"})
    with pytest.raises(ValueError, match="pool changed"):
        validate_selection(freeze, selected)
    with pytest.raises(ValueError, match="revision"):
        validate_selection({**freeze, "pool_sha256": file_sha(path)}, selected)
