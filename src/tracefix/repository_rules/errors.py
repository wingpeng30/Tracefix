"""Stable refusal codes shared by rule models and their runtime adapters."""

from __future__ import annotations


class RuleError(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
