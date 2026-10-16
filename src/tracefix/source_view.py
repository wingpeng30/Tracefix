"""Read-only source port shared by memory, guidance and runtime adapters."""

from __future__ import annotations

from typing import Protocol


class SourceView(Protocol):
    """Read one approved regular file, or report absence without inventing content."""

    def read_bytes(self, relative_path: str) -> bytes | None: ...
