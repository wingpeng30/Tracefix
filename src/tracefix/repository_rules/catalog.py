"""Deterministic scoped selection from a small source port; no runtime imports."""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Callable

from pydantic import ValidationError

from tracefix.repository_rules.domain import (
    MAX_CONFIG_BYTES,
    MAX_RULE_BYTES,
    MAX_SELECTION_BYTES,
    MAX_SELECTION_DOCUMENTS,
    RuleConfiguration,
    RuleDocument,
    RuleSelection,
    RuleSourceVersion,
)
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.paths import (
    RULE_BODY_DIRECTORY,
    RULE_CONFIG_PATH,
    matches_glob,
    relative_path,
)
from tracefix.source_view import SourceView


class RepositoryRules:
    """Read applicable guidance only; callers separately enforce execution permissions."""

    def __init__(self, source: SourceView, *, case_sensitive: bool = True,
                 validate_target: Callable[[str], str] = relative_path) -> None:
        if type(case_sensitive) is not bool:
            raise RuleError("rules_case_semantics_invalid")
        self.source, self.case_sensitive = source, case_sensitive
        self.validate_target = validate_target

    def _read(self, path: str) -> bytes | None:
        content = self.source.read_bytes(path)
        if content is not None:
            if not isinstance(content, bytes):
                raise RuleError("rules_source_invalid_bytes")
            limit = MAX_CONFIG_BYTES if path == RULE_CONFIG_PATH else MAX_RULE_BYTES
            if len(content) > limit:
                raise RuleError("rules_source_limit_exceeded")
        return content

    @staticmethod
    def _configuration(content: bytes | None) -> RuleConfiguration:
        if content is None:
            return RuleConfiguration(schema_version=1)
        try:
            return RuleConfiguration.model_validate(tomllib.loads(content.decode("utf-8")))
        except (UnicodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
            raise RuleError("rules_configuration_invalid") from exc

    def select(self, target: str) -> RuleSelection:
        target = relative_path(self.validate_target(target))
        documents, sources = [], {}
        total = 0

        def read(path):
            content = self._read(path)
            sha = hashlib.sha256(content).hexdigest() if content is not None else None
            if path in sources and sources[path].sha256 != sha:
                raise RuleError("rules_changed_during_selection")
            sources[path] = RuleSourceVersion(source_path=path, sha256=sha)
            return content

        def append(document):
            nonlocal total
            if len(documents) >= MAX_SELECTION_DOCUMENTS:
                raise RuleError("rules_selection_document_limit")
            total += len(document.body.encode("utf-8"))
            if total > MAX_SELECTION_BYTES:
                raise RuleError("rules_selection_body_limit")
            documents.append(document)

        parents = target.split("/")[:-1]
        for depth in range(len(parents) + 1):
            path = "/".join((*parents[:depth], "AGENTS.md"))
            content = read(path)
            if content is not None:
                append(RuleDocument.create(kind="ancestor", source_path=path, body=content))
        configuration = self._configuration(read(RULE_CONFIG_PATH))
        applicable = [(index, spec) for index, spec in enumerate(configuration.rules)
                      if any(matches_glob(pattern, target, case_sensitive=self.case_sensitive)
                             for pattern in spec.paths)]
        if len(documents) + len(applicable) > MAX_SELECTION_DOCUMENTS:
            raise RuleError("rules_selection_document_limit")
        for index, spec in applicable:
            path = RULE_BODY_DIRECTORY + "/" + spec.file
            content = read(path)
            if content is None:
                raise RuleError("rules_declared_body_missing")
            append(RuleDocument.create(kind="path", source_path=path, body=content,
                                      patterns=spec.paths, declaration_index=index))
        # Detect a changed configuration, deleted ancestor, or new formerly absent rule.
        for path, observation in sources.items():
            current = self._read(path)
            sha = hashlib.sha256(current).hexdigest() if current is not None else None
            if sha != observation.sha256:
                raise RuleError("rules_changed_during_selection")
        return RuleSelection.create(target, documents, tuple(sources.values()),
                                    case_sensitive=self.case_sensitive)
