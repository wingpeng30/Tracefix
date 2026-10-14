"""Contract tests for typed, source-scoped knowledge memory."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tracefix.memory import ExperienceStore, atomic_json, digest
from tracefix.memory_domain import (
    ApplicabilityStatus,
    EvidenceLevel,
    KnowledgeKind,
    KnowledgeVersion,
    PublicationState,
    RepositoryIdentity,
    SourceApplicability,
    SourceSnapshot,
    issue_user_input_receipt,
    validate_user_input_receipt,
)
from tracefix.memory_recall import (
    RECALL_MAX_BODY_BYTES,
    ApplicabilityEvaluator,
    DeterministicRecallMaterializer,
    EvidenceVerifier,
)


def identity(*, checkout: str = "a") -> RepositoryIdentity:
    return RepositoryIdentity(
        repository_id=hashlib.sha256(b"repo").hexdigest(),
        checkout_id=hashlib.sha256(checkout.encode()).hexdigest(),
        git_backed=True,
    )


def source(*, checkout: str = "a", value: str = "source") -> SourceSnapshot:
    return SourceSnapshot(identity=identity(checkout=checkout), files={"src/widget.py": value})


def knowledge(
    key: str,
    *,
    evidence=EvidenceLevel.OBSERVED,
    publication=PublicationState.ACTIVE,
    value="source",
    kind=KnowledgeKind.PROCEDURE,
) -> dict:
    version = KnowledgeVersion(
        record_id=f"{identity().repository_id}:{key}:v1",
        kind=kind,
        publication=publication,
        evidence=evidence,
        applicability=SourceApplicability(
            repository_id=identity().repository_id,
            files={"src/widget.py": value},
        ),
    )
    return {
        "knowledge": version.model_dump(mode="json"),
        "content": {
            "summary": "分页 widget.py repair",
            "keywords": ["分页"],
            "applicability_paths": ["src/widget.py"],
            "steps": ["Read the current implementation before editing."],
        },
    }


def test_state_evidence_and_applicability_are_independent_recall_axes():
    records = [
        ("observed", 1, knowledge("observed")),
        (
            "user",
            1,
            knowledge("user", evidence=EvidenceLevel.USER_STATED, kind=KnowledgeKind.DECISION),
        ),
        ("cited", 1, knowledge("cited", evidence=EvidenceLevel.SOURCE_CITED)),
        (
            "unsubstantiated",
            1,
            knowledge("unsubstantiated", evidence=EvidenceLevel.UNSUBSTANTIATED),
        ),
        ("disabled", 1, knowledge("disabled", publication=PublicationState.DISABLED)),
        ("stale", 1, knowledge("stale", value="old source")),
        ("missing", 1, knowledge("missing", value="source")),
    ]
    current = SourceSnapshot(
        identity=identity(),
        files={
            "src/widget.py": "source",
            "missing.py": None,
        },
    )
    # The missing case has its own applicability path so it remains distinguishable.
    missing = KnowledgeVersion(
        record_id=f"{identity().repository_id}:missing:v1",
        kind=KnowledgeKind.PROCEDURE,
        publication=PublicationState.ACTIVE,
        evidence=EvidenceLevel.OBSERVED,
        applicability=SourceApplicability(
            repository_id=identity().repository_id,
            files={"missing.py": "expected"},
        ),
    )
    records[-1] = (
        "missing",
        1,
        {
            "knowledge": missing.model_dump(mode="json"),
            "content": {"summary": "分页 widget.py repair", "keywords": ["分页"]},
        },
    )
    bundle = DeterministicRecallMaterializer().materialize(
        task="修复 src/widget.py 分页",
        records=records,
        current=current,
    )
    assert [item.key for item in bundle.items] == ["observed", "user"]
    assert {item.evidence for item in bundle.items} == {
        EvidenceLevel.OBSERVED,
        EvidenceLevel.USER_STATED,
    }
    assert all(item.applicability.status is ApplicabilityStatus.MATCHED for item in bundle.items)
    statuses = ApplicabilityEvaluator()
    stale_binding = SourceApplicability.model_validate(records[5][2]["knowledge"]["applicability"])
    assert statuses.evaluate(stale_binding, current).status is ApplicabilityStatus.STALE
    assert statuses.evaluate(missing.applicability, current).status is ApplicabilityStatus.MISSING


def test_evidence_citations_never_upgrade_free_text_and_user_receipts_are_bound():
    verifier = EvidenceVerifier()
    assert (
        verifier.verify(
            KnowledgeKind.PROJECT_FACT,
            {
                "source_citations": [{"path": "src/widget.py", "result_sha256": "abc"}],
            },
        )
        is EvidenceLevel.SOURCE_CITED
    )
    assert (
        verifier.verify(
            KnowledgeKind.PROJECT_FACT,
            {
                "machine_observation": {"verified": True, "value_sha256": "abc"},
                "source_citations": [{"path": "src/widget.py"}],
            },
        )
        is EvidenceLevel.OBSERVED
    )
    assert (
        verifier.verify(
            KnowledgeKind.PROCEDURE,
            {
                "source_citations": [{"path": "src/widget.py"}],
            },
        )
        is EvidenceLevel.SOURCE_CITED
    )

    content = "请保留页码为零时的哨兵语义"
    receipt = issue_user_input_receipt("message-1", 2, content)
    evidence = {
        "trusted_user_receipt": receipt.model_dump(mode="json"),
        "user_statement": "保留页码为零时的哨兵语义",
        "user_message_id": "message-1",
        "user_turn": 2,
        "user_content": content,
    }
    assert verifier.verify(KnowledgeKind.DECISION, evidence) is EvidenceLevel.USER_STATED
    assert validate_user_input_receipt(receipt, message_id="message-1", turn=2, content=content)
    evidence["user_content"] = "altered"
    assert verifier.verify(KnowledgeKind.DECISION, evidence) is EvidenceLevel.UNSUBSTANTIATED
    # A USER role or a direct statement without a host receipt is not trusted provenance.
    assert (
        verifier.verify(
            KnowledgeKind.DECISION,
            {
                "user_statement": "保留页码为零时的哨兵语义",
                "user_content": content,
            },
        )
        is EvidenceLevel.UNSUBSTANTIATED
    )


def test_recall_uses_chinese_bigrams_fixed_weights_and_stable_ties():
    records = []
    for key in ("zeta", "beta", "alpha"):
        item = knowledge(key)
        item["content"]["applicability_paths"] = []
        records.append((key, 1, item))
    records.extend(
        [
            (
                "path",
                1,
                {
                    **knowledge("path"),
                    "content": {
                        "summary": "分页",
                        "keywords": [],
                        "applicability_paths": ["src/widget.py"],
                    },
                },
            ),
            (
                "error",
                1,
                {
                    **knowledge("error"),
                    "content": {
                        "summary": "TimeoutError 分页",
                        "keywords": [],
                        "applicability_paths": [],
                    },
                },
            ),
        ]
    )
    bundle = DeterministicRecallMaterializer().materialize(
        task="修复 src/widget.py TimeoutError 分页",
        records=records,
        current=source(),
    )
    assert bundle.items[0].key == "path"  # path(8) + Chinese bigrams outranks error(4)
    assert bundle.items[0].score == 12
    assert bundle.items[1].key == "error"
    assert bundle.items[1].score == 6
    ties = DeterministicRecallMaterializer().materialize(
        task="分页",
        records=records[:3],
        current=source(),
    )
    assert [item.key for item in ties.items] == ["alpha", "beta", "zeta"]


def test_recall_caps_item_count_and_rendered_utf8_bytes():
    records = []
    for key in ("a", "b", "c", "d"):
        item = knowledge(key)
        item["content"]["summary"] = "分页" + ("x" * 3000)
        records.append((key, 1, item))
    bundle = DeterministicRecallMaterializer().materialize(
        task="分页",
        records=records,
        current=source(),
    )
    assert len(bundle.items) == 2
    assert bundle.body_utf8_bytes <= RECALL_MAX_BODY_BYTES
    assert [item.key for item in bundle.items] == ["a", "b"]


def test_recall_bundle_nested_content_and_source_bindings_are_immutable():
    bundle = DeterministicRecallMaterializer().materialize(
        task="分页",
        records=[("a", 1, knowledge("a"))],
        current=source(),
    )
    item = bundle.items[0]
    with pytest.raises(TypeError, match="immutable"):
        item.content["summary"] = "rewritten"
    with pytest.raises(TypeError, match="immutable"):
        item.source_applicability.files["src/widget.py"] = "rewritten"
    with pytest.raises(TypeError):
        item.content["keywords"][0] = "rewritten"
    assert (
        bundle.model_dump(mode="json")["items"][0]["content"]["summary"] == "分页 widget.py repair"
    )


def test_recall_rejects_unknown_and_unproven_candidates_and_renders_failure_scope():
    from tracefix.memory_recall import render_recall_bundle

    current = SourceSnapshot(
        identity=identity(),
        files={"src/widget.py": "source"},
        dependency_sha256="deps-current",
        environment_sha256="env-current",
    )
    base = knowledge("failure")
    base["knowledge"]["applicability"]["dependency_sha256"] = "deps-current"
    base["knowledge"]["applicability"]["environment_sha256"] = "env-current"
    base["content"] = {
        "summary": "unrelated summary",
        "keywords": [],
        "applicability_paths": [],
        "steps": [],
        "failure_condition": "TimeoutError",
    }
    malformed = {"content": {"summary": "TimeoutError"}}
    no_match = knowledge("no-match")
    no_match["content"] = {"summary": "unrelated words", "keywords": []}
    source_cited = knowledge("cited", evidence=EvidenceLevel.SOURCE_CITED)
    unknown_repo = knowledge("foreign")
    unknown_repo["knowledge"]["applicability"]["repository_id"] = "f" * 64
    bundle = DeterministicRecallMaterializer().materialize(
        task="TimeoutError",
        records=[
            ("malformed", 1, malformed),
            ("no-match", 1, no_match),
            ("source-cited", 1, source_cited),
            ("foreign", 1, unknown_repo),
            ("failure", 1, base),
        ],
        current=current,
    )
    assert [item.key for item in bundle.items] == ["failure"]
    assert "失败条件：TimeoutError" in render_recall_bundle(bundle)
    assert ApplicabilityEvaluator().evaluate(None, current).status is ApplicabilityStatus.UNKNOWN
    foreign_binding = SourceApplicability(repository_id="f" * 64, files={})
    assert (
        ApplicabilityEvaluator().evaluate(foreign_binding, current).status
        is ApplicabilityStatus.UNKNOWN
    )


@pytest.mark.parametrize(
    ("binding", "current_files", "dependencies", "environment", "expected"),
    [
        (
            SourceApplicability(
                repository_id=identity().repository_id, files={}, dependency_sha256="deps"
            ),
            {},
            None,
            None,
            ApplicabilityStatus.MISSING,
        ),
        (
            SourceApplicability(
                repository_id=identity().repository_id, files={}, dependency_sha256="deps"
            ),
            {},
            "other-deps",
            None,
            ApplicabilityStatus.STALE,
        ),
        (
            SourceApplicability(
                repository_id=identity().repository_id, files={}, environment_sha256="env"
            ),
            {},
            None,
            None,
            ApplicabilityStatus.MISSING,
        ),
        (
            SourceApplicability(
                repository_id=identity().repository_id, files={}, environment_sha256="env"
            ),
            {},
            None,
            "other-env",
            ApplicabilityStatus.STALE,
        ),
    ],
)
def test_applicability_checks_dependency_and_environment_identity(
    binding, current_files, dependencies, environment, expected
):
    current = SourceSnapshot(
        identity=identity(),
        files=current_files,
        dependency_sha256=dependencies,
        environment_sha256=environment,
    )
    assert ApplicabilityEvaluator().evaluate(binding, current).status is expected


def test_domain_deep_freeze_handles_existing_mappings_sequences_sets_and_copy():
    from tracefix.memory_domain import FrozenDict

    frozen = FrozenDict({"nested": {"items": [1, {"x"}], "stable": FrozenDict({"v": 2})}})
    assert copy.deepcopy(frozen) is frozen
    assert frozen["nested"]["items"] == (1, frozenset({"x"}))
    assert frozen["nested"]["stable"]["v"] == 2
    with pytest.raises(TypeError, match="immutable"):
        frozen["nested"]["items"] += (3,)


def test_local_source_view_rejects_unsafe_paths_and_caps_files(tmp_path):
    from tracefix.memory_sources import LocalSourceSnapshotProvider, LocalSourceView

    root = tmp_path / "repo"
    root.mkdir()
    (root / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    view = LocalSourceView(root)
    for unsafe in ("../outside", r"C:\\outside", ".git/config", "a\nfile"):
        with pytest.raises(ValueError, match="unsafe"):
            view.read_bytes(unsafe)
    assert view.read_bytes("missing.py") is None
    (root / "folder").mkdir()
    assert view.read_bytes("folder") is None
    oversized = root / "oversized.bin"
    with oversized.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="size limit"):
        view.read_bytes("oversized.bin")
    with pytest.raises(ValueError, match="directory"):
        LocalSourceView(root / "widget.py")

    provider = LocalSourceSnapshotProvider()
    before = provider.snapshot(root, ["widget.py"], patch_sha256="a" * 64)
    changed = provider.snapshot(
        root,
        ["widget.py"],
        content_root=root,
        patch_sha256="b" * 64,
        environment={"profile": "isolated"},
    )
    assert (
        before.files["widget.py"] == hashlib.sha256((root / "widget.py").read_bytes()).hexdigest()
    )
    assert changed.files["widget.py"] == before.files["widget.py"]
    assert before.patch_sha256 != changed.patch_sha256
    assert before.environment_sha256 != changed.environment_sha256
    assert not before.identity.git_backed
    with pytest.raises(ValueError, match="content root"):
        provider.snapshot(root, [], content_root=root / "widget.py")


def test_proposal_kind_contracts_and_verified_decision_and_negative_result(tmp_path):
    from test_memory import evidence

    from tracefix.memory import ExperienceProposal, validate_proposal

    with pytest.raises(ValueError, match="procedures require"):
        ExperienceProposal(key="bad", summary="x", keywords=["x"])
    with pytest.raises(ValueError, match="project facts require"):
        ExperienceProposal(
            key="bad-fact",
            summary="x",
            keywords=["x"],
            kind=KnowledgeKind.PROJECT_FACT,
            applicability_paths=["widget.py"],
            steps=[{"instruction": "step", "call_id": "call"}],
        )
    with pytest.raises(ValueError, match="decisions require"):
        ExperienceProposal(
            key="bad-decision", summary="x", keywords=["x"], kind=KnowledgeKind.DECISION
        )
    with pytest.raises(ValueError, match="negative results require"):
        ExperienceProposal(
            key="bad-negative",
            summary="x",
            keywords=["x"],
            kind=KnowledgeKind.NEGATIVE_RESULT,
            applicability_paths=["widget.py"],
        )

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = ExperienceStore(tmp_path / "memory", repo)
    decision_text = "保留零页码语义"
    decision_receipt = issue_user_input_receipt("direct-user-1", 1, decision_text)
    decision = ExperienceProposal(
        key="pagination-decision",
        summary=decision_text,
        keywords=["页码"],
        kind=KnowledgeKind.DECISION,
        user_receipt_id=decision_receipt.message_id,
        generalized=False,
    )
    decision_evidence = {
        **evidence(),
        "trusted_user_inputs": [
            {"receipt": decision_receipt.model_dump(mode="json"), "content": decision_text}
        ],
    }
    assert validate_proposal(decision, decision_evidence, repo) == []
    assert (
        store.publish("decision-episode", decision, decision_evidence, [])["status"] == "verified"
    )
    selected_decision = store.select("页码")
    assert [item[0] for item in selected_decision] == ["pagination-decision"]
    assert selected_decision[0][2]["knowledge"]["evidence"] == "user_stated"

    failed = {
        **evidence(),
        "tool_results": {
            **evidence()["tool_results"],
            "failed-patch": {
                "tool_name": "apply_patch",
                "success": False,
                "error": "patch failed with SyntaxError: malformed hunk",
            },
        },
        "call_order": ["read", "failed-patch", "test"],
    }
    negative = ExperienceProposal(
        key="patch-negative-result",
        summary="patch failed",
        keywords=["SyntaxError"],
        kind=KnowledgeKind.NEGATIVE_RESULT,
        applicability_paths=["widget.py"],
        failure_call_id="failed-patch",
        failure_condition="SyntaxError",
        generalized=False,
    )
    assert validate_proposal(negative, failed, repo) == []
    assert store.publish("negative-episode", negative, failed, [])["status"] == "verified"
    selected_negative = store.select("SyntaxError")
    assert [item[0] for item in selected_negative] == ["patch-negative-result"]
    assert selected_negative[0][2]["knowledge"]["evidence"] == "observed"

    failed["latest_test"] = {**failed["latest_test"], "source_sha256": "other"}
    assert "negative result is not an observed failed call in a verified task" in validate_proposal(
        negative, failed, repo
    )


def test_migration_rejects_changed_source_and_conflicting_preservation_copy(tmp_path):
    store, original = _v1_store(tmp_path)
    normalized = store._read()
    store.legacy_path.write_bytes(original + b" ")
    with pytest.raises(Exception, match="changed before atomic migration"):
        store._write(normalized, operation="disable")
    assert store.legacy_path.read_bytes() == original + b" "
    assert not store.path.exists()
    assert not store.legacy_backup_path.exists()

    other_root = tmp_path / "other"
    other_root.mkdir()
    other, before = _v1_store(other_root)
    other.legacy_backup_path.parent.mkdir(parents=True, exist_ok=True)
    other.legacy_backup_path.write_bytes(b"unrelated preservation copy")
    with pytest.raises(Exception, match="preservation copy conflicts"):
        other.migrate()
    assert other.legacy_path.read_bytes() == before
    assert other.legacy_backup_path.read_bytes() == b"unrelated preservation copy"
    assert not other.path.exists()


def test_migration_preview_reports_corrupt_legacy_without_writing(tmp_path):
    store, _ = _v1_store(tmp_path)
    store.migrate()
    store.legacy_path.write_text("{not-json", encoding="utf-8")
    before = store.path.read_bytes()
    report = store.migration_preview()
    assert report["status"] == "current"
    assert report["conflicts"][0]["kind"] == "invalid_legacy_store"
    assert store.path.read_bytes() == before


def test_store_rejects_in_repo_memory_root_and_missing_source_snapshot(tmp_path):
    from test_memory import evidence, proposal

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(Exception, match="outside the source repository"):
        ExperienceStore(repo / ".memory", repo)

    store = ExperienceStore(tmp_path / "memory", repo)
    missing = proposal(applicability_paths=["absent.py"])
    with pytest.raises(Exception, match="missing an applicability file"):
        store.publish("missing-source", missing, evidence(), [])
    assert store.list() == {}


def test_memory_evidence_snapshot_streams_and_caps_tool_output(tmp_path):
    from tracefix.memory import _MAX_EVIDENCE_BYTES, canonical, extract_evidence
    from tracefix.tracing import TraceEventType

    trace = tmp_path / "trajectory.jsonl"
    events = []
    for number in range(80):
        events.append(
            {
                "event_type": TraceEventType.TOOL_RETURNED.value,
                "payload": {
                    "result": {
                        "call_id": f"call-{number}",
                        "tool_name": "read_file",
                        "success": True,
                        "output": {"path": "src/widget.py", "content": "x" * 16_000},
                    }
                },
            }
        )
    trace.write_bytes(
        b"".join(json.dumps(item, ensure_ascii=False).encode("utf-8") + b"\n" for item in events)
    )
    agent = SimpleNamespace(
        history=SimpleNamespace(snapshot=lambda: []),
        _last_test_evidence={"valid": True, "source_sha256": "source"},
    )
    evidence = extract_evidence(agent, trace, "source")
    assert len(canonical(evidence)) <= _MAX_EVIDENCE_BYTES
    assert len(evidence["tool_results"]) <= 64
    assert evidence["truncated_tool_results"] > 0
    assert all(len(item["output"]["content"]) < 5000 for item in evidence["tool_results"].values())


def test_evidence_snapshot_skips_oversized_rows_and_bounds_receipts(tmp_path):
    from tracefix.memory import _MAX_EVIDENCE_TRACE_RECORD_BYTES, extract_evidence
    from tracefix.messages import Message, MessageRole, ToolCall
    from tracefix.tracing import TraceEventType

    trace = tmp_path / "bounded.jsonl"
    event = {
        "event_type": TraceEventType.TOOL_RETURNED.value,
        "payload": {
            "result": {
                "call_id": "result-0",
                "tool_name": "read_file",
                "success": True,
                "output": {"path": "src/widget.py", "content": "VALUE = 1"},
            }
        },
    }
    second_event = {
        "event_type": TraceEventType.TOOL_RETURNED.value,
        "payload": {"result": {"call_id": "result-1", "tool_name": "search_code"}},
    }
    raw_rows = [
        json.dumps(event).encode("utf-8") + b"\n",
        b"x" * (_MAX_EVIDENCE_TRACE_RECORD_BYTES + 1) + b"\n",
        json.dumps({"event_type": "noise", "payload": {}}).encode("utf-8") + b"\n",
        json.dumps({"event_type": TraceEventType.TOOL_RETURNED.value, "payload": {}}).encode(
            "utf-8"
        )
        + b"\n",
        json.dumps(second_event).encode("utf-8") + b"\n",
    ]
    trace.write_bytes(b"".join(raw_rows))

    history = []
    for turn in range(1, 3):
        text = f"direct choice {turn}"
        receipt = issue_user_input_receipt(f"user-{turn}", turn, text)
        history.append(
            Message(
                id=f"user-{turn}",
                role=MessageRole.USER,
                content=text,
                metadata={"turn": turn, "trusted_user_input_receipt": receipt.model_dump()},
            )
        )
    huge_text = "y" * (16 * 1024 + 1)
    huge_receipt = issue_user_input_receipt("user-huge", 18, huge_text)
    history.append(
        Message(
            id="user-huge",
            role=MessageRole.USER,
            content=huge_text,
            metadata={
                "turn": 18,
                "trusted_user_input_receipt": huge_receipt.model_dump(mode="json"),
            },
        )
    )
    history.append(
        Message(
            id="assistant-forged",
            role=MessageRole.ASSISTANT,
            content="not direct user input",
            metadata={"turn": 19, "trusted_user_input_receipt": huge_receipt.model_dump()},
        )
    )
    history.append(
        Message(
            id="user-malformed",
            role=MessageRole.USER,
            content="malformed receipt",
            metadata={"turn": 20, "trusted_user_input_receipt": {"issuer": "forged"}},
        )
    )
    history.append(
        Message(
            id="assistant-call",
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=(
                ToolCall(id="result-0", name="read_file", arguments={"path": "src/widget.py"}),
                ToolCall(id="unused", name="run_tests", arguments={"command": "pytest"}),
            ),
        )
    )
    agent = SimpleNamespace(
        history=SimpleNamespace(snapshot=lambda: history),
        _last_test_evidence=None,
    )
    result = extract_evidence(agent, trace, "source")
    assert set(result["tool_results"]) == {"result-0", "result-1"}
    assert result["truncated_tool_results"] == 1
    assert result["calls"]["result-0"]["tool_name"] == "read_file"
    assert result["source_citations"] == [
        {
            "call_id": "result-0",
            "path": "src/widget.py",
            "result_sha256": result["tool_results"]["result-0"]["result_sha256"],
        }
    ]
    assert len(result["trusted_user_inputs"]) == 2
    assert result["truncated_user_inputs"] == 1


def test_evidence_snapshot_reduces_user_receipts_to_the_total_byte_cap(tmp_path):
    from tracefix.memory import _MAX_EVIDENCE_BYTES, canonical, extract_evidence
    from tracefix.messages import Message, MessageRole

    history = []
    for turn in range(1, 18):
        content = "x" * 16_000
        receipt = issue_user_input_receipt(f"user-{turn}", turn, content)
        history.append(
            Message(
                id=f"user-{turn}",
                role=MessageRole.USER,
                content=content,
                metadata={"turn": turn, "trusted_user_input_receipt": receipt.model_dump()},
            )
        )
    trace = tmp_path / "empty.jsonl"
    trace.write_bytes(b"")
    agent = SimpleNamespace(
        history=SimpleNamespace(snapshot=lambda: history),
        _last_test_evidence=None,
    )
    result = extract_evidence(agent, trace, "source")
    assert len(canonical(result)) <= _MAX_EVIDENCE_BYTES
    assert len(result["trusted_user_inputs"]) < 16
    assert result["truncated_user_inputs"] > 1


def test_bounded_evidence_projection_limits_depth_and_container_width():
    from tracefix.memory import _bounded_evidence_value

    nested = {"leaf": "kept"}
    for _ in range(9):
        nested = {"next": nested}
    projected = _bounded_evidence_value(
        {"nested": nested, "wide": list(range(70)), "opaque": object()}
    )
    cursor = projected["nested"]
    while "next" in cursor:
        cursor = cursor["next"]
    assert cursor == {"omitted": "maximum evidence depth reached"}
    assert len(projected["wide"]) == 64
    assert isinstance(projected["opaque"], str)


def test_evidence_extract_accepts_only_host_receipted_user_messages(tmp_path):
    from tracefix.memory import extract_evidence
    from tracefix.messages import Message, MessageRole

    text = "保留零页码语义"
    receipt = issue_user_input_receipt("trusted-1", 1, text)
    trusted = Message(
        id="trusted-1",
        role=MessageRole.USER,
        content=text,
        metadata={"turn": 1, "trusted_user_input_receipt": receipt.model_dump(mode="json")},
    )
    plain = Message(id="plain", role=MessageRole.USER, content="普通用户消息，无收据")
    forged = Message(
        id="forged",
        role=MessageRole.USER,
        content="篡改后的内容",
        metadata={"turn": 1, "trusted_user_input_receipt": receipt.model_dump(mode="json")},
    )
    trace = tmp_path / "empty.jsonl"
    trace.write_bytes(b"")
    agent = SimpleNamespace(
        history=SimpleNamespace(snapshot=lambda: [plain, trusted, forged]),
        _last_test_evidence=None,
    )
    evidence = extract_evidence(agent, trace, "source")
    assert [item["receipt"]["message_id"] for item in evidence["trusted_user_inputs"]] == [
        "trusted-1"
    ]


def test_recovery_validates_pinned_bundle_and_user_receipt_before_resume(tmp_path):
    from test_memory import evidence, proposal

    from tracefix.memory import atomic_json, canonical
    from tracefix.memory_recall import render_recall_bundle
    from tracefix.messages import Message, MessageRole
    from tracefix.runtime import TraceFixRunner

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = ExperienceStore(tmp_path / "memory", repo)
    store.publish("first", proposal(), evidence(), [])
    bundle = store.recall_bundle("分页 widget.py")
    task = "请修复 widget.py 的分页"
    receipt = issue_user_input_receipt("user-1", 1, task)
    user_message = Message(
        id="user-1",
        role=MessageRole.USER,
        content=task,
        metadata={"turn": 1, "trusted_user_input_receipt": receipt.model_dump(mode="json")},
    )
    recall_message = Message(
        role=MessageRole.USER,
        content=render_recall_bundle(bundle),
        metadata={
            "kind": "memory_recall_bundle",
            "trust_level": "untrusted_reference_data",
            "bundle_sha256": hashlib.sha256(canonical(bundle.model_dump(mode="json"))).hexdigest(),
        },
    )
    root = tmp_path / "run"
    root.mkdir()
    atomic_json(root / "trusted-user-inputs.json", [receipt.model_dump(mode="json")])
    atomic_json(root / "memory-recall.json", bundle.model_dump(mode="json"))
    agent_payload = {
        "history": [user_message.model_dump(mode="json"), recall_message.model_dump(mode="json")]
    }
    config = SimpleNamespace(memory_enabled=True)
    TraceFixRunner._verify_recovery_memory(root, config, agent_payload)

    tampered = dict(agent_payload)
    tampered["history"] = [
        user_message.model_dump(mode="json"),
        recall_message.model_copy(
            update={
                "content": "different recall",
            }
        ).model_dump(mode="json"),
    ]
    with pytest.raises(Exception, match="fixed memory recall differs"):
        TraceFixRunner._verify_recovery_memory(root, config, tampered)

    bad_receipt = receipt.model_copy(update={"content_sha256": "0" * 64})
    atomic_json(root / "trusted-user-inputs.json", [bad_receipt.model_dump(mode="json")])
    with pytest.raises(Exception, match="trusted user input provenance"):
        TraceFixRunner._verify_recovery_memory(root, config, agent_payload)


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True)


def _v1_store(tmp_path: Path) -> tuple[ExperienceStore, bytes]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "widget.py")
    _git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    store = ExperienceStore(tmp_path / "memory", repo)
    # Seed through the public API, then translate its valid content into the prior v1 shape.
    from test_memory import evidence, proposal

    store.publish("first", proposal(), evidence(), [])
    current = json.loads(store.path.read_text(encoding="utf-8"))
    data = current["data"]
    data["repo_id"] = store.legacy_repo_id
    data["legacy_extension"] = {"kept": True}
    for record in data["records"].values():
        record.pop("disabled_version", None)
        for version in record["versions"]:
            version.pop("knowledge", None)
            proposal_data = version["proposal"]
            for name in ("kind", "user_receipt_id", "failure_call_id", "failure_condition"):
                proposal_data.pop(name, None)
    v1 = {"schema_version": 1, "sha256": digest(data), "data": data}
    store.legacy_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(store.legacy_path, v1)
    store.path.unlink()
    return store, store.legacy_path.read_bytes()


def test_v1_read_and_dry_run_are_read_only_and_first_mutation_is_one_v2_publish(
    tmp_path,
    monkeypatch,
):
    from test_memory import evidence, proposal

    store, before = _v1_store(tmp_path)
    preview = store.migrate(dry_run=True)
    assert preview["status"] == "migration_required"
    assert preview["record_count"] == 1
    assert preview["unknown_top_level_fields_preserved"] == ["legacy_extension"]
    assert store.legacy_path.read_bytes() == before
    assert not store.path.exists() and not store.legacy_backup_path.exists()
    legacy = store.show("pagination")["versions"][0]["knowledge"]
    assert legacy["legacy"] is True
    assert legacy["applicability"] is None
    assert legacy["evidence"] == "source_cited"
    assert store.select("pagination") == []
    assert store.legacy_path.read_bytes() == before

    result = store.publish("second", proposal(key="pagination-v2"), evidence(), [])
    assert result["status"] == "verified"
    assert store.legacy_path.read_bytes() == before
    assert store.legacy_backup_path.read_bytes() == before
    envelope = json.loads(store.path.read_text(encoding="utf-8"))
    assert envelope["schema_version"] == 2
    assert envelope["data"]["migration_receipt"] == {
        "from_schema": 1,
        "to_schema": 2,
        "source_sha256": hashlib.sha256(before).hexdigest(),
        "record_count": 2,
        "operation": "publish",
    }
    assert envelope["data"]["legacy_extension"] == {"kept": True}


@pytest.mark.parametrize("operation", ["migrate", "disable", "rollback"])
def test_v1_first_operation_records_migration_in_the_same_v2_envelope(tmp_path, operation):
    store, before = _v1_store(tmp_path)
    if operation == "migrate":
        store.migrate()
    elif operation == "disable":
        store.disable("pagination")
    else:
        store.rollback("pagination", 1)
    envelope = json.loads(store.path.read_text(encoding="utf-8"))
    assert envelope["data"]["migration_receipt"]["operation"] == operation
    assert store.legacy_path.read_bytes() == before
    assert store.legacy_backup_path.read_bytes() == before
    assert len(envelope["data"]["records"]["pagination"]["versions"]) == 1


def test_failed_v1_migration_preserves_original_and_corrupt_v2_never_falls_back(
    tmp_path,
    monkeypatch,
):
    import tracefix.memory as memory

    store, before = _v1_store(tmp_path)
    real_replace = memory.os.replace

    def fail_v2(source_path, destination):
        if Path(destination) == store.path:
            raise OSError("injected v2 replacement failure")
        return real_replace(source_path, destination)

    monkeypatch.setattr(memory.os, "replace", fail_v2)
    with pytest.raises(OSError, match="injected"):
        store.disable("pagination")
    assert store.legacy_path.read_bytes() == before
    assert store.legacy_backup_path.read_bytes() == before
    assert not store.path.exists()
    assert store.show("pagination")["active"] == 1

    monkeypatch.setattr(memory.os, "replace", real_replace)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{corrupt v2", encoding="utf-8")
    with pytest.raises(Exception, match="cannot verify memory store"):
        store.list()
    assert store.legacy_path.read_bytes() == before


def test_migration_preview_reports_unrelated_v1_and_v2_conflict(tmp_path):
    store, _ = _v1_store(tmp_path)
    store.migrate()
    legacy = json.loads(store.legacy_path.read_text(encoding="utf-8"))
    legacy["data"]["legacy_extension"] = {"edited": True}
    legacy["sha256"] = digest(legacy["data"])
    atomic_json(store.legacy_path, legacy)
    report = store.migration_preview()
    assert report["status"] == "current"
    assert report["conflicts"] == [
        {
            "kind": "unrelated_v1_and_v2_stores",
            "legacy_sha256": hashlib.sha256(store.legacy_path.read_bytes()).hexdigest(),
        }
    ]


def test_worktree_identity_is_shared_but_checkout_applicability_is_local(tmp_path):
    from test_memory import evidence, proposal

    from tracefix.memory_sources import GitRepositoryIdentityResolver

    primary = tmp_path / "primary"
    primary.mkdir()
    (primary / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(primary, "init", "-q")
    _git(primary, "add", "widget.py")
    _git(
        primary,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    worktree = tmp_path / "worktree"
    _git(primary, "worktree", "add", "--detach", str(worktree), "HEAD")
    resolver = GitRepositoryIdentityResolver()
    first = resolver.resolve(primary)
    second = resolver.resolve(worktree)
    assert first.repository_id == second.repository_id
    assert first.checkout_id != second.checkout_id
    provider = ExperienceStore(tmp_path / "memory", primary).source_provider
    applicable = provider.snapshot(primary, ["widget.py"])
    store_a = ExperienceStore(tmp_path / "memory", primary)
    store_a.publish("episode", proposal(), evidence(), [])
    store_b = ExperienceStore(tmp_path / "memory", worktree)
    assert store_a.select("pagination")
    assert store_b.select("pagination")
    (worktree / "widget.py").write_text("VALUE = 2\n", encoding="utf-8")
    stale = provider.snapshot(worktree, ["widget.py"])
    binding = SourceApplicability(
        repository_id=first.repository_id, files={"widget.py": applicable.files["widget.py"]}
    )
    evaluator = ApplicabilityEvaluator()
    assert evaluator.evaluate(binding, applicable).status is ApplicabilityStatus.MATCHED
    assert evaluator.evaluate(binding, stale).status is ApplicabilityStatus.STALE
    assert store_b.select("pagination") == []
    assert store_b.show("pagination")["active"] == 1
    (worktree / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert store_b.select("pagination")
    store_b.disable("pagination")
    assert store_a.select("pagination") == []
    store_b.rollback("pagination", 1)
    assert store_a.select("pagination")
    clone = tmp_path / "independent-clone"
    _git(tmp_path, "clone", "--no-hardlinks", str(primary), str(clone))
    assert resolver.resolve(clone).repository_id != first.repository_id


@pytest.mark.parametrize(
    "field,value",
    [
        ("record_id", "forged"),
        ("kind", "decision"),
    ],
)
def test_v2_knowledge_identity_and_type_are_checked_even_with_recomputed_envelope(
    tmp_path,
    field,
    value,
):
    from test_memory import evidence, proposal

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = ExperienceStore(tmp_path / "memory", repo)
    store.publish("episode", proposal(), evidence(), [])
    envelope = json.loads(store.path.read_text(encoding="utf-8"))
    envelope["data"]["records"]["pagination"]["versions"][0]["knowledge"][field] = value
    envelope["sha256"] = digest(envelope["data"])
    atomic_json(store.path, envelope)
    with pytest.raises(Exception, match="cannot verify memory store"):
        store.list()


def test_postimage_fact_is_stale_until_patch_is_applied_but_preimage_procedure_remains(
    tmp_path,
):
    from test_memory import evidence, proposal

    from tracefix.memory import ExperienceProposal

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "widget.py").write_text("VALUE = 2\n", encoding="utf-8")
    store = ExperienceStore(tmp_path / "memory", repo)
    before = store.source_provider.snapshot(repo, ["widget.py"])
    after = store.source_provider.snapshot(
        repo,
        ["widget.py"],
        content_root=workspace,
        patch_sha256=hashlib.sha256(b"patch").hexdigest(),
    )
    procedure_evidence = evidence()
    procedure_evidence.update(
        preimage_snapshot=before.model_dump(mode="json"),
        postimage_snapshot=after.model_dump(mode="json"),
        patch_sha256=hashlib.sha256(b"patch").hexdigest(),
    )
    store.publish("procedure", proposal(), procedure_evidence, [])
    fact_evidence = {
        **procedure_evidence,
        "tool_results": {
            "read-fact": {
                "tool_name": "read_file",
                "success": True,
                "output": {"path": "widget.py", "content": "VALUE = 2\n"},
            },
        },
    }
    fact = ExperienceProposal(
        key="postimage-fact",
        summary="VALUE = 2",
        keywords=["VALUE"],
        kind=KnowledgeKind.PROJECT_FACT,
        applicability_paths=["widget.py"],
        generalized=False,
    )
    store.publish("fact", fact, fact_evidence, [])
    assert store.select("VALUE") == []  # procedure does not match; fact postimage is stale
    assert store.show("postimage-fact")["active"] == 1

    (repo / "widget.py").write_text("VALUE = 2\n", encoding="utf-8")
    selected = store.select("VALUE")
    assert [item[0] for item in selected] == ["postimage-fact"]
    assert selected[0][2]["knowledge"]["evidence"] == "observed"
