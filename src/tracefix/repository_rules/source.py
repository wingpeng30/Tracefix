"""Guidance-only source port over the shared bounded file reader."""

from __future__ import annotations

from tracefix.repository_rules.domain import MAX_CONFIG_BYTES, MAX_RULE_BYTES
from tracefix.repository_rules.file_io import BoundedWorkspaceReader
from tracefix.repository_rules.paths import RULE_CONFIG_PATH, rule_source_path


class RuleSourceView(BoundedWorkspaceReader):
    """Expose only Markdown guidance and its fixed TOML catalog."""

    def read_bytes(self, relative_path: str) -> bytes | None:
        relative = rule_source_path(relative_path)
        maximum = MAX_CONFIG_BYTES if relative == RULE_CONFIG_PATH else MAX_RULE_BYTES
        return super().read_bytes(relative, maximum=maximum)
