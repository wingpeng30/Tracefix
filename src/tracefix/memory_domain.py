"""Typed, storage-independent knowledge memory contracts.

The domain layer deliberately knows nothing about a runner, model client, or execution backend.
Adapters own repository inspection and persistence; callers provide already trusted evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeKind(StrEnum):
    PROJECT_FACT = "project_fact"
    DECISION = "decision"
    PROCEDURE = "procedure"
    NEGATIVE_RESULT = "negative_result"


class PublicationState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DISABLED = "disabled"
    SUPERSEDED = "superseded"


class EvidenceLevel(StrEnum):
    OBSERVED = "observed"
    USER_STATED = "user_stated"
    SOURCE_CITED = "source_cited"
    UNSUBSTANTIATED = "unsubstantiated"


class ApplicabilityStatus(StrEnum):
    MATCHED = "matched"
    STALE = "stale"
    MISSING = "missing"
    UNKNOWN = "unknown"


class RepositoryIdentity(BaseModel):
    """A shared logical repository and a separately tracked checkout identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkout_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    git_backed: bool


class SourceSnapshot(BaseModel):
    """Immutable hashes of explicitly inspected source and environment inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: RepositoryIdentity
    files: dict[str, str | None] = Field(default_factory=dict)
    dependency_sha256: str | None = None
    environment_sha256: str | None = None
    patch_sha256: str | None = None


class SourceApplicability(BaseModel):
    """Evidence preconditions for one knowledge version; absent hashes mean unbound."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: dict[str, str] = Field(default_factory=dict)
    dependency_sha256: str | None = None
    environment_sha256: str | None = None
    patch_sha256: str | None = None


class ApplicabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ApplicabilityStatus
    checkout_id: str
    reasons: tuple[str, ...] = ()


class KnowledgeVersion(BaseModel):
    """A typed memory item, independent of the store's version-chain envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str = Field(min_length=1, max_length=160)
    kind: KnowledgeKind
    publication: PublicationState
    evidence: EvidenceLevel
    applicability: SourceApplicability | None = None
    legacy: bool = False
    user_receipt_id: str | None = None


class TrustedUserInputReceipt(BaseModel):
    """Host-issued evidence that a specific direct user message was received."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str = "tracefix-host-v1"
    message_id: str = Field(min_length=1, max_length=200)
    turn: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecallItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    key: str
    version: int = Field(ge=1)
    content: dict[str, object]
    evidence: EvidenceLevel
    applicability: ApplicabilityResult
    score: int = Field(ge=0)
    reasons: tuple[str, ...] = ()


class RecallBundle(BaseModel):
    """A fixed selection attached to one request/recovery history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    repository_id: str
    checkout_id: str
    strategy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: tuple[RecallItem, ...] = ()
    body_utf8_bytes: int = Field(ge=0, le=8192)


class RepositoryIdentityResolver(Protocol):
    def resolve(self, repository: object) -> RepositoryIdentity: ...


class SourceSnapshotProvider(Protocol):
    def snapshot(
        self,
        repository: object,
        paths: Sequence[str],
        *,
        content_root: object | None = None,
        patch_sha256: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> SourceSnapshot: ...


class EvidenceVerifier(Protocol):
    def verify(self, kind: KnowledgeKind, evidence: Mapping[str, object]) -> EvidenceLevel: ...


class ApplicabilityEvaluator(Protocol):
    def evaluate(
        self, applicability: SourceApplicability | None, current: SourceSnapshot
    ) -> ApplicabilityResult: ...


class MemoryStore(Protocol):
    def list(self) -> dict[str, object]: ...


class RecallMaterializer(Protocol):
    def materialize(self, items: Sequence[RecallItem], *, repository_id: str,
                    checkout_id: str) -> RecallBundle: ...


def issue_user_input_receipt(message_id: str, turn: int, content: str) -> TrustedUserInputReceipt:
    """Mint a receipt at a host API boundary, never from a message role or model payload."""
    import hashlib

    return TrustedUserInputReceipt(
        message_id=message_id,
        turn=turn,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def validate_user_input_receipt(receipt: TrustedUserInputReceipt, *, message_id: str,
                                turn: int, content: str) -> bool:
    """Check receipt binding; a USER role alone never establishes user provenance."""
    expected = issue_user_input_receipt(message_id, turn, content)
    return receipt == expected
