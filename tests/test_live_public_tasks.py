import importlib.util
import json
import sys
from pathlib import Path

import pytest


def test_campaign_boundaries(tmp_path):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("campaign", scripts / "live_public_tasks.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    tasks = list(module.TASKS)
    path = tmp_path / "runs.json"
    data = module.read_ledger(path, tasks[0], tasks[0])
    with pytest.raises(ValueError, match="order"):
        module.read_ledger(path, tasks[1], tasks[1])
    data["runs"] = [{"stage": tasks[0], "status": "started"}]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unknown"):
        module.read_ledger(path, tasks[1], tasks[1])
    data["runs"] = [
        {"stage": task, "status": "budget_exceeded", "usage_complete": True}
        for task in tasks
    ]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="targeted"):
        module.read_ledger(path, tasks[0], "sample-again")
    assert module.read_ledger(path, tasks[0], "recheck-1")
    data["runs"] += [dict(data["runs"][0], stage=f"recheck-{i}") for i in (1, 2)]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="limit"):
        module.read_ledger(path, tasks[0], "recheck-3")
    data["max_runs"] = 8
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="identity"):
        module.read_ledger(path, tasks[0], "recheck-3")
