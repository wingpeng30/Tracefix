"""Compare two frozen run directory trees by path, size, and SHA-256."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def inventory(root: Path) -> tuple[dict[str, int], set[str]]:
    files: dict[str, int] = {}
    directories: set[str] = set()
    for base, names, filenames in os.walk(root, followlinks=False):
        parent = Path(base)
        for name in names:
            path = parent / name
            if path.is_symlink():
                raise ValueError(f"linked directory: {path}")
            directories.add(path.relative_to(root).as_posix())
        for name in filenames:
            path = parent / name
            if path.is_symlink():
                raise ValueError(f"linked file: {path}")
            files[path.relative_to(root).as_posix()] = path.stat().st_size
    return files, directories


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def verify_pair(source: Path, target: Path, relative: str) -> tuple[str, str, bool]:
    left = sha256(source / relative)
    return relative, left, left == sha256(target / relative)


def verify(source: Path, target: Path, output: Path, workers: int = 12) -> dict:
    started = time.monotonic()
    source_files, source_dirs = inventory(source)
    target_files, target_dirs = inventory(target)
    if source_dirs != target_dirs or source_files != target_files:
        shared_files = source_files.keys() & target_files.keys()
        changed_sizes = sum(source_files[k] != target_files[k] for k in shared_files)
        raise ValueError(
            "path or size mismatch: "
            f"missing_files={len(source_files.keys() - target_files.keys())}, "
            f"extra_files={len(target_files.keys() - source_files.keys())}, "
            f"changed_sizes={changed_sizes}, "
            f"missing_dirs={len(source_dirs - target_dirs)}, "
            f"extra_dirs={len(target_dirs - source_dirs)}"
        )
    paths = sorted(source_files)
    tree = hashlib.sha256()
    mismatches: list[str] = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for offset in range(0, len(paths), 2000):
            batch = paths[offset:offset + 2000]
            for relative, digest, same in pool.map(
                lambda name: verify_pair(source, target, name), batch
            ):
                if not same:
                    mismatches.append(relative)
                tree.update(relative.encode("utf-8"))
                tree.update(b"\0")
                tree.update(digest.encode("ascii"))
                tree.update(b"\n")
            progress = {
                "source": str(source), "target": str(target),
                "total_files": len(paths), "checked_files": min(offset + len(batch), len(paths)),
                "mismatches": mismatches[:20], "mismatch_count": len(mismatches),
                "elapsed_seconds": round(time.monotonic() - started, 1),
            }
            output.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{source.name}: {progress['checked_files']}/{len(paths)}", flush=True)
    progress.update({
        "directory_count": len(source_dirs), "bytes": sum(source_files.values()),
        "tree_sha256": tree.hexdigest(), "complete": not mismatches,
    })
    output.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    return progress


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()
    result = verify(args.source, args.target, args.output, args.workers)
    raise SystemExit(0 if result["complete"] else 1)
