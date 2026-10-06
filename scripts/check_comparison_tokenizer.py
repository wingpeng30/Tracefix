"""Exercise the installed official worker using real text and tool schemas, offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tracefix.comparison_profiles import OfficialCounter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    runtime = {
        "command": [
            sys.executable,
            "-m",
            "tracefix.comparison_counter",
            "--tokenizer",
            str(args.tokenizer.resolve()),
        ]
    }
    bodies = []
    for content in ("Fix Python code", "修复 Unicode：测试✓", 'escaped: \\"\\n'):
        bodies.append(
            {
                "model": "deepseek-flash",
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": content}],
                "tools": [],
            }
        )
    bodies.append(
        {
            "model": "deepseek-flash",
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": "Read code"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read source",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    },
                }
            ],
        }
    )
    result = OfficialCounter(runtime).invoke(bodies)
    result["supplier_calls"] = 0
    result["accepted"] = all(
        count["status"] == "estimate" and count["tokens"] > 0 for count in result["counts"]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
