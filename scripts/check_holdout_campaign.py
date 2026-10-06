"""Execute the complete 180-trial schedule with real files/pytest and zero provider calls."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from tracefix.comparison import read_json, write_json
from tracefix.comparison_campaign import prepare, report, run
from tracefix.comparison_holdout import HOLDOUT_TASK_IDS
from tracefix.comparison_profiles import HOLDOUT_PROFILE, HOLDOUT_PROFILES

PATCH = """diff --git a/sample.py b/sample.py
--- a/sample.py
+++ b/sample.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return 0
+    return a + b
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--counter-runtime", type=Path, required=True)
    parser.add_argument("--previous-campaign", type=Path, required=True)
    parser.add_argument("--profile", choices=HOLDOUT_PROFILES, default=HOLDOUT_PROFILE)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = root / "fixture-source"
    source.mkdir()
    (source / "sample.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    (source / "test_sample.py").write_text(
        "from sample import add\n"
        "def test_fix(): assert add(1, 2) == 3\n"
        "def test_owned_artifact(tmp_path):\n"
        "    (tmp_path / 'generated.bin').write_bytes(bytes(range(256)))\n",
        encoding="utf-8",
    )
    for command in (
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
        subprocess.run(["git", *command], cwd=source, capture_output=True, check=True)
    catalog = root / "catalog.json"
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
            for task in HOLDOUT_TASK_IDS
        ],
    )
    prices = root / "prices.json"
    write_json(
        prices,
        {
            "model": "deepseek-flash",
            "input_peak_cny": 2.0,
            "output_peak_cny": 8.0,
            "kind": "offline_fixture",
        },
    )
    campaign = root / "campaign"
    prepare(
        catalog,
        campaign,
        prices,
        mode="offline",
        profile=args.profile,
        tokenizer=args.tokenizer.resolve(),
        counter_runtime=args.counter_runtime.resolve(),
        previous_campaign=args.previous_campaign.resolve(),
    )
    run(campaign)
    summary = report(campaign)
    before = (campaign / "requests.json").read_bytes()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "tracefix.comparison_campaign",
            "run",
            "--campaign-dir",
            str(campaign),
        ],
        check=True,
    )
    accepted = (
        summary["complete"]
        and all(r["successes"] == 60 for r in summary["arms"].values())
        and before == (campaign / "requests.json").read_bytes()
    )
    write_json(
        root / "acceptance.json",
        {
            "accepted": accepted,
            "supplier_calls": 0,
            "planned": summary["planned"],
            "independent_process_reentry": True,
            "requests": len(read_json(campaign / "requests.json")["requests"]),
        },
    )
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
