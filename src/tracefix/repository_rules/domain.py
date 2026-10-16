"""Immutable guidance documents and selections, independent of runtime and storage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.paths import (
    RULE_BODY_DIRECTORY,
    matches_glob,
    relative_path,
    rule_source_path,
    validate_glob,
)

MAX_RULE_BYTES = 16 * 1024
MAX_SELECTION_BYTES = 32 * 1024
MAX_SELECTION_DOCUMENTS = 16
MAX_CONFIG_BYTES = 32 * 1024


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class PathRuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    paths: tuple[str, ...] = Field(min_length=1, max_length=16)
    file: str

    @field_validator("paths")
    @classmethod
    def patterns(cls, value):
        for pattern in value:
            validate_glob(pattern)
        if len(set(value)) != len(value):
            raise ValueError("duplicate rule pattern")
        return value

    @field_validator("file")
    @classmethod
    def body_file(cls, value):
        relative_path(value)
        if not value.casefold().endswith(".md"):
            raise ValueError("path rules require Markdown bodies")
        return value


class RuleConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: StrictInt = Field(ge=1, le=1)
    rules: tuple[PathRuleSpec, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def distinct_files(self):
        names = [entry.file.casefold() for entry in self.rules]
        if len(set(names)) != len(names):
            raise ValueError("declare each rule body once and combine its paths")
        return self


class RuleSourceVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    source_path: str
    sha256: str | None = Field(pattern=r"^[a-f0-9]{64}$")

    _source = field_validator("source_path")(rule_source_path)


class RuleDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: StrictInt = Field(ge=1, le=1)
    rule_id: str = Field(pattern=r"^rule_[a-f0-9]{64}$")
    version: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["ancestor", "path"]
    source_path: str
    scope_path: str
    patterns: tuple[str, ...] = Field(default=(), max_length=16)
    declaration_index: StrictInt = Field(default=0, ge=0, lt=128)
    body: str
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    _source = field_validator("source_path")(rule_source_path)

    def identity_payload(self) -> dict:
        return {key: getattr(self, key) for key in (
            "schema_version", "kind", "source_path", "scope_path", "patterns", "declaration_index")}

    @model_validator(mode="after")
    def provenance(self):
        relative_path(self.scope_path, allow_root=True)
        if self.kind == "ancestor":
            if (PurePosixPath(self.source_path).name != "AGENTS.md" or self.patterns
                    or self.declaration_index != 0
                    or str(PurePosixPath(self.source_path).parent) != self.scope_path):
                raise ValueError("ancestor guidance scope must match its AGENTS.md parent")
        elif (not self.source_path.startswith(RULE_BODY_DIRECTORY + "/")
                or not self.source_path.casefold().endswith(".md")
                or self.scope_path != "." or not self.patterns):
            raise ValueError("path guidance requires fixed rule bodies and explicit patterns")
        for pattern in self.patterns:
            validate_glob(pattern)
        data = self.body.encode("utf-8")
        if len(data) > MAX_RULE_BYTES:
            raise ValueError("rule body exceeds 16 KiB UTF-8")
        if hashlib.sha256(data).hexdigest() != self.body_sha256:
            raise ValueError("rule body digest mismatch")
        if (self.rule_id != "rule_" + digest(self.identity_payload())
                or self.version != digest({"rule_id": self.rule_id, "body": self.body_sha256})):
            raise ValueError("rule identity or version mismatch")
        return self

    @classmethod
    def create(cls, *, kind: Literal["ancestor", "path"], source_path: str, body: bytes,
               patterns: Sequence[str] = (), declaration_index: int = 0):
        if len(body) > MAX_RULE_BYTES:
            raise RuleError("rules_body_limit_exceeded")
        try:
            text = body.decode("utf-8")
        except UnicodeError as exc:
            raise RuleError("rules_body_not_utf8") from exc
        payload = {"schema_version": 1, "kind": kind, "source_path": source_path,
                   "scope_path": str(PurePosixPath(source_path).parent) if kind == "ancestor"
                   else ".", "patterns": tuple(patterns), "declaration_index": declaration_index}
        identity = "rule_" + digest(payload)
        sha256 = hashlib.sha256(body).hexdigest()
        return cls(**payload, rule_id=identity, body=text, body_sha256=sha256,
                   version=digest({"rule_id": identity, "body": sha256}))

    def applies_to(self, target: str, *, case_sensitive: bool = True) -> bool:
        target = relative_path(target)
        if self.kind == "path":
            return any(matches_glob(pattern, target, case_sensitive=case_sensitive)
                       for pattern in self.patterns)
        scope = self.scope_path
        if not case_sensitive:
            scope, target = scope.casefold(), target.casefold()
        return scope == "." or target.startswith(scope + "/")


class RuleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    schema_version: StrictInt = Field(ge=1, le=1)
    target_path: str
    case_sensitive: StrictBool = True
    documents: tuple[RuleDocument, ...] = Field(default=(), max_length=MAX_SELECTION_DOCUMENTS)
    sources: tuple[RuleSourceVersion, ...] = Field(default=(), max_length=128)
    bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    _target = field_validator("target_path")(relative_path)

    @property
    def body_bytes(self) -> int:
        return sum(len(doc.body.encode("utf-8")) for doc in self.documents)

    def selection_payload(self) -> dict:
        return {"schema_version": 1, "target_path": self.target_path,
                "case_sensitive": self.case_sensitive, "bundle_sha256": self.bundle_sha256,
                "sources": [source.model_dump(mode="json") for source in self.sources]}

    @model_validator(mode="after")
    def consistent(self):
        if self.body_bytes > MAX_SELECTION_BYTES:
            raise ValueError("complete rule selection exceeds 32 KiB UTF-8")
        source_map = {item.source_path: item.sha256 for item in self.sources}
        if len(source_map) != len(self.sources):
            raise ValueError("duplicate rule source observation")
        seen, ordering = set(), []
        for doc in self.documents:
            if doc.rule_id in seen or source_map.get(doc.source_path) != doc.body_sha256:
                raise ValueError("rule selection source or identity mismatch")
            if not doc.applies_to(self.target_path, case_sensitive=self.case_sensitive):
                raise ValueError("rule selection scope does not apply")
            seen.add(doc.rule_id)
            ordering.append((0, len(PurePosixPath(doc.scope_path).parts))
                            if doc.kind == "ancestor" else (1, doc.declaration_index))
        if ordering != sorted(set(ordering)):
            raise ValueError("rule selection precedence is invalid")
        refs = [(doc.rule_id, doc.version) for doc in self.documents]
        if (self.bundle_sha256 != digest(refs)
                or self.sha256 != digest(self.selection_payload())):
            raise ValueError("rule selection digest mismatch")
        return self

    @classmethod
    def create(cls, target_path: str, documents: Sequence[RuleDocument],
               sources: Sequence[RuleSourceVersion], *, case_sensitive: bool = True):
        bundle = digest([(doc.rule_id, doc.version) for doc in documents])
        payload = {"schema_version": 1, "target_path": target_path,
                   "case_sensitive": case_sensitive, "bundle_sha256": bundle,
                   "sources": [source.model_dump(mode="json") for source in sources]}
        return cls(**payload, documents=tuple(documents), sha256=digest(payload))
