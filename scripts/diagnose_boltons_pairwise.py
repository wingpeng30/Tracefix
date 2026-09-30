"""Compare saved public Boltons task states without model calls or source edits."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

CASES = """import json
from boltons.iterutils import pairwise, pairwise_iter
def inspect(name, fn):
    try:
        value = fn()
        return {'case': name, 'value': list(value) if name.startswith('iter_') else value}
    except Exception as exc:
        return {'case': name, 'error': type(exc).__name__}
checks = [
    ('empty', lambda: pairwise([])),
    ('singleton', lambda: pairwise([1])),
    ('overlap', lambda: pairwise([1, 2, 3, 4])),
    ('generator', lambda: pairwise(iter([1, 2, 3]))),
    ('iter_overlap', lambda: pairwise_iter(iter([1, 2, 3]))),
    ('count_keyword', lambda: pairwise([1, 2, 3], count=1)),
]
print(json.dumps([inspect(name, fn) for name, fn in checks]))
"""


def inspect(root: Path) -> dict:
    root = root.resolve()
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
        text=True, check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "-uno"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    source = root / "boltons" / "iterutils.py"
    output = subprocess.run(
        [sys.executable, "-c", CASES], cwd=root, capture_output=True,
        text=True, timeout=30, check=False,
    )
    return {
        "root": str(root), "git_commit": revision, "tracked_status": status,
        "iterutils_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "exit_code": output.returncode,
        "cases": json.loads(output.stdout) if output.returncode == 0 else None,
        "error": output.stderr[-1000:] if output.returncode else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {
        "schema_version": 1, "python": sys.version,
        "base": inspect(args.base), "model": inspect(args.model),
        "upstream": inspect(args.upstream),
    }
    if args.output.exists():
        parser.error("output already exists")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
