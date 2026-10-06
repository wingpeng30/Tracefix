"""Isolated official tokenizer worker; stdin/stdout JSON and no provider calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from importlib import metadata
from pathlib import Path

from tracefix.models.input_bounds import (
    V41_RECIPE_VERSION,
    V41_TOKENIZER_SHA256,
    count_deepseek_v41_request,
)


def identity(tokenizer: Path) -> dict:
    if metadata.version("deepseek-recipe") != V41_RECIPE_VERSION:
        raise ValueError("official recipe version mismatch")
    tokenizer_sha = hashlib.sha256(tokenizer.read_bytes()).hexdigest()
    if tokenizer_sha != V41_TOKENIZER_SHA256:
        raise ValueError("official tokenizer identity mismatch")
    packages = {}
    for name in ("deepseek-recipe", "pydantic", "pydantic-core"):
        distribution = metadata.distribution(name)
        files = {}
        for item in distribution.files or ():
            if str(item).endswith((".pyc", "RECORD")) or ".." in item.parts:
                continue
            path = Path(distribution.locate_file(item))
            if path.is_file():
                files[str(item)] = hashlib.sha256(path.read_bytes()).hexdigest()
        packages[name] = {"version": distribution.version, "files": files}
    return {
        "python": sys.version,
        "tokenizer_sha256": tokenizer_sha,
        "packages": packages,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
    args = parser.parse_args(argv)
    os.environ["TRACEFIX_DEEPSEEK_V41_TOKENIZER_JSON"] = str(args.tokenizer.resolve())
    request = json.load(sys.stdin)
    result = {"identity": identity(args.tokenizer)}
    if "bodies" in request:
        result["counts"] = [asdict(count_deepseek_v41_request(body)) for body in request["bodies"]]
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
