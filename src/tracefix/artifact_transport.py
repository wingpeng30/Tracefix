"""Internal bounded streaming bridge for ordinary Docker artifact archives."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tracefix.artifacts import MAX_ARTIFACT_ARCHIVE_BYTES, ArtifactError, ArtifactStore
from tracefix.security import validate_project_environment


class _BoundedReader:
    def __init__(self, source, maximum: int) -> None:
        self.source = source
        self.maximum = maximum
        self.read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        if self.read_bytes >= self.maximum:
            extra = self.source.read(1)
            if extra:
                raise ArtifactError("artifact archive exceeds its transfer limit")
            return b""
        allowed = self.maximum - self.read_bytes
        requested = allowed + 1 if size < 0 else min(size, allowed + 1)
        chunk = self.source.read(requested)
        self.read_bytes += len(chunk)
        if self.read_bytes > self.maximum:
            raise ArtifactError("artifact archive exceeds its transfer limit")
        return chunk


class _BoundedWriter:
    def __init__(self, destination, maximum: int) -> None:
        self.destination = destination
        self.maximum = maximum
        self.written = 0

    def write(self, data: bytes) -> int:
        if self.written + len(data) > self.maximum:
            raise ArtifactError("artifact archive exceeds its transfer limit")
        count = self.destination.write(data)
        if count != len(data):
            raise ArtifactError("artifact archive transfer was incomplete")
        self.written += count
        return count

    def flush(self) -> None:
        self.destination.flush()


def _store(args: argparse.Namespace) -> ArtifactStore:
    environment = json.loads(args.environment_json.read_text(encoding="utf-8"))
    environment = validate_project_environment(environment)
    return ArtifactStore(
        args.root,
        run_id=args.run_id,
        workspace=args.workspace,
        secret_values=tuple(environment.values()),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("export", "import"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--environment-json", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    store = _store(args)
    if args.operation == "export":
        writer = _BoundedWriter(sys.stdout.buffer, MAX_ARTIFACT_ARCHIVE_BYTES)
        receipt = store.export_archive(writer)
        sys.stdout.buffer.flush()
        sys.stderr.write(json.dumps(receipt, sort_keys=True) + "\n")
        sys.stderr.flush()
    else:
        receipt = store.import_archive(_BoundedReader(sys.stdin.buffer, MAX_ARTIFACT_ARCHIVE_BYTES))
        sys.stdout.write(json.dumps(receipt, sort_keys=True) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
