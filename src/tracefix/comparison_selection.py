"""Deterministic expansion of the existing task pool before any paid evaluation."""

from pathlib import Path

from tracefix.comparison import file_sha, read_json
from tracefix.comparison_holdout import HOLDOUT_TASK_IDS


def next_candidate(pool: dict, selected: list[str], attempted: set[str], excluded: set[str]) -> str:
    rows = {r["instance_id"]: r for r in pool["selected"]}
    order = pool["candidate_order"]
    counts = {repo: 0 for repo in pool["requested_repositories"]}
    for task in selected:
        counts[rows[task]["repo"]] += 1
    available = {}
    for task in order:
        if task not in set(selected) | attempted | excluded:
            available.setdefault(rows[task]["repo"], task)
    if not available:
        raise ValueError("candidate pool exhausted before thirty qualified tasks")
    repo = min(available, key=lambda name: (counts[name], name))
    return available[repo]


def validate_selection(freeze: dict, task_ids: list[str]) -> None:
    """Replay selection decisions and bind all qualification evidence on the host."""
    if (
        freeze.get("kind") != "tracefix_ac_thirty_selection"
        or len(task_ids) != 30
        or len(set(task_ids)) != 30
        or task_ids[:20] != list(HOLDOUT_TASK_IDS)
        or freeze.get("selected_task_ids") != task_ids
    ):
        raise ValueError("thirty-task selection identity changed")
    pool_path = Path(freeze["pool_path"])
    if file_sha(pool_path) != freeze["pool_sha256"]:
        raise ValueError("candidate pool changed")
    pool = read_json(pool_path)
    if pool["revision"] != "78f471bf655a3137b2e8a75af1501690ec009ec3":
        raise ValueError("candidate dataset revision changed")
    selected = list(HOLDOUT_TASK_IDS)
    attempted: set[str] = set()
    excluded = set(freeze["excluded_task_ids"]) | set(pool["excluded_task_ids"])
    for attempt in freeze["attempts"]:
        if len(selected) >= 30:
            raise ValueError("selection contains attempts after the final task")
        expected = next_candidate(pool, selected, attempted, excluded)
        if attempt["task_id"] != expected or type(attempt["accepted"]) is not bool:
            raise ValueError("deterministic candidate order changed")
        attempted.add(expected)
        evidence = Path(attempt["evidence_path"])
        if file_sha(evidence) != attempt["evidence_sha256"]:
            raise ValueError("selection qualification evidence changed")
        if attempt["accepted"]:
            record = read_json(evidence)
            qualification = record.get("qualification", {})
            if (
                record.get("task_id") != expected
                or record.get("kind") != "real"
                or qualification.get("eligible_for_llm_prescreen") is not True
                or qualification.get("qualification_type") != "assertion_failure"
            ):
                raise ValueError("selection requires real task qualification evidence")
            selected.append(expected)
    if selected != task_ids:
        raise ValueError("selection did not establish thirty qualified tasks")
