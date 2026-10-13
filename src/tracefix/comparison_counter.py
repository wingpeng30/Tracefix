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


def identity(
    tokenizer: Path, *, verified_tokenizer_sha256: str | None = None
) -> dict:
    if metadata.version("deepseek-recipe") != V41_RECIPE_VERSION:
        raise ValueError("official recipe version mismatch")
    if verified_tokenizer_sha256 is None:
        tokenizer_sha = hashlib.sha256(tokenizer.read_bytes()).hexdigest()
        if tokenizer_sha != V41_TOKENIZER_SHA256:
            raise ValueError("official tokenizer identity mismatch")
    else:
        if verified_tokenizer_sha256 != V41_TOKENIZER_SHA256:
            raise ValueError("official tokenizer identity mismatch")
        tokenizer_sha = verified_tokenizer_sha256
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


def _runtime_files(tokenizer: Path) -> tuple[tuple[str, int, int], ...]:
    files = {str(tokenizer.resolve()): tokenizer.stat()}
    for name in ("deepseek-recipe", "pydantic", "pydantic-core"):
        distribution = metadata.distribution(name)
        for item in distribution.files or ():
            if str(item).endswith((".pyc", "RECORD")) or ".." in item.parts:
                continue
            path = Path(distribution.locate_file(item))
            if path.is_file():
                files[str(path.resolve())] = path.stat()
    return tuple(sorted((path, stat.st_size, stat.st_mtime_ns) for path, stat in files.items()))


def serve(tokenizer: Path) -> int:
    """Serve newline-delimited count requests until EOF or an identity change."""
    resolved = tokenizer.resolve()
    stat = resolved.stat()
    # Load the native tokenizer before declaring readiness, so all later requests
    # reuse the same immutable encoding instance.
    from tracefix.models.input_bounds import _cached_v41_encoding

    _cached_v41_encoding(
        str(resolved), V41_TOKENIZER_SHA256, V41_RECIPE_VERSION, stat.st_size, stat.st_mtime_ns
    )
    # The encoder loader has just verified the full tokenizer hash. Reuse that
    # fact here so startup hashes the large tokenizer file only once.
    current_identity = identity(
        resolved, verified_tokenizer_sha256=V41_TOKENIZER_SHA256
    )
    file_identity = _runtime_files(resolved)
    identity_json = json.dumps(
        current_identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    identity_sha256 = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
    print(json.dumps({
        "protocol": 1,
        "ready": True,
        "identity": current_identity,
        "identity_sha256": identity_sha256,
    }, ensure_ascii=False, separators=(",", ":")), flush=True)

    last_request_id = 0
    for line in sys.stdin:
        request_id = None
        stop_after_response = False
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("protocol") != 1:
                raise ValueError("invalid protocol frame")
            request_id = request.get("id")
            if type(request_id) is not int or request_id != last_request_id + 1:
                stop_after_response = True
                raise ValueError("invalid request id")
            last_request_id = request_id
            bodies = request.get("bodies")
            if not isinstance(bodies, list) or not bodies:
                raise ValueError("bodies must be a non-empty list")
            if _runtime_files(resolved) != file_identity:
                stop_after_response = True
                raise ValueError("counter runtime files changed")
            counts = [asdict(count_deepseek_v41_request(body)) for body in bodies]
            response = {
                "protocol": 1,
                "id": request_id,
                "identity_sha256": identity_sha256,
                "ok": True,
                "counts": counts,
            }
        except Exception as exc:
            response = {
                "protocol": 1,
                "id": request_id,
                "identity_sha256": identity_sha256,
                "ok": False,
                "error": type(exc).__name__,
            }
            stop_after_response = True
        print(json.dumps(response, ensure_ascii=False, separators=(",", ":")), flush=True)
        if stop_after_response:
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--serve", action="store_true", help="serve persistent JSONL requests")
    args = parser.parse_args(argv)
    os.environ["TRACEFIX_DEEPSEEK_V41_TOKENIZER_JSON"] = str(args.tokenizer.resolve())
    if args.serve:
        return serve(args.tokenizer)
    request = json.load(sys.stdin)
    result = {"identity": identity(args.tokenizer)}
    if "bodies" in request:
        result["counts"] = [asdict(count_deepseek_v41_request(body)) for body in request["bodies"]]
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
