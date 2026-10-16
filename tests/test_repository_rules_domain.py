from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from tracefix.repository_rules.domain import (
    PathRuleSpec,
    RuleConfiguration,
    RuleDocument,
    RuleSelection,
    RuleSourceVersion,
)
from tracefix.repository_rules.errors import RuleError


def ancestor(path="AGENTS.md", body=b"project guidance"):
    return RuleDocument.create(kind="ancestor", source_path=path, body=body)


def path_rule(index=0, body=b"path guidance", pattern="src/**"):
    return RuleDocument.create(kind="path", source_path=f".tracefix/rules/style{index}.md",
        body=body, patterns=(pattern,), declaration_index=index)


def selection(docs, target="src/pkg/code.py", **kwargs):
    sources = tuple(RuleSourceVersion(source_path=doc.source_path, sha256=doc.body_sha256)
                    for doc in docs)
    return RuleSelection.create(target, docs, sources, **kwargs)


def test_document_versions_separate_identity_from_body_and_keep_exact_utf8():
    body = "先读取完整实现。\r\n😀\n".encode()
    first = ancestor(body=body)
    changed = ancestor(body=body + b"more")
    assert first.body.encode() == body
    assert first.body_sha256 == hashlib.sha256(body).hexdigest()
    assert first.rule_id == changed.rule_id and first.version != changed.version
    assert first == RuleDocument.model_validate_json(first.model_dump_json())
    with pytest.raises(ValidationError):
        first.body = "mutated"


def test_precedence_scope_and_selection_identity_are_explicit():
    docs = (ancestor(), ancestor("src/AGENTS.md"), ancestor("src/pkg/AGENTS.md"),
            path_rule(0), path_rule(2))
    selected = selection(docs)
    assert selected.documents == docs
    assert selected.body_bytes == sum(len(doc.body.encode()) for doc in docs)
    same_guidance = selection(docs, target="src/pkg/other.py")
    assert selected.bundle_sha256 == same_guidance.bundle_sha256
    assert selected.sha256 != same_guidance.sha256
    assert selected == RuleSelection.model_validate_json(selected.model_dump_json())
    assert not docs[1].applies_to("src2/code.py")
    assert not docs[-1].applies_to("tests/code.py")
    assert docs[1].applies_to("SRC/code.py", case_sensitive=False)
    assert not docs[1].applies_to("SRC/code.py")


@pytest.mark.parametrize("bad", [
    {"network": "on"}, {"schema_version": True}, {"schema_version": 2},
    {"rules": [{"paths": ["src/**"], "file": "../escape.md"}]},
    {"rules": [{"paths": ["src/**"], "file": "python.sh"}]},
    {"rules": [{"paths": ["src/**", "src/**"], "file": "python.md"}]},
    {"rules": [{"paths": [], "file": "python.md"}]},
    {"rules": [{"paths": ["src/**"], "file": "python.md", "allowed_tools": ["shell"]}]},
    {"rules": [{"paths": ["src/**"], "file": "a.md"},
               {"paths": ["tests/**"], "file": "A.md"}]},
])
def test_configuration_is_versioned_bounded_and_cannot_declare_permissions(bad):
    with pytest.raises(ValueError):
        RuleConfiguration.model_validate({"schema_version": 1, **bad})


def test_configuration_detaches_mutable_lists_and_combines_paths_in_one_declaration():
    paths = ["src/**/*.py", "tests/**"]
    config = RuleConfiguration(schema_version=1, rules=[{"file": "python.md", "paths": paths}])
    paths.append("other/**")
    assert config.rules[0].paths == ("src/**/*.py", "tests/**")
    assert PathRuleSpec(paths=["**"], file="nested/style.MD").file == "nested/style.MD"
    with pytest.raises(ValidationError):
        RuleConfiguration(schema_version=1, rules=[
            {"file": f"rule{index}.md", "paths": ["**"]} for index in range(129)])


def test_utf8_size_limits_refuse_complete_body_or_set_instead_of_truncating():
    maximum = ancestor(body=b"x" * 16384)
    assert len(maximum.body) == 16384
    assert selection([maximum, path_rule(body=b"y" * 16384)]).body_bytes == 32768
    with pytest.raises(RuleError, match="body_limit"):
        ancestor(body=("汉" * 6000).encode())
    with pytest.raises(RuleError, match="not_utf8"):
        ancestor(body=b"\xff")
    with pytest.raises(ValidationError, match="32 KiB"):
        selection([maximum, ancestor("src/AGENTS.md", b"y" * 16384), path_rule()])
    with pytest.raises(ValidationError):
        selection([path_rule(index) for index in range(17)])


@pytest.mark.parametrize("field,value", [
    ("body", "forged body"), ("body_sha256", "f" * 64), ("rule_id", "rule_" + "f" * 64),
    ("version", "f" * 64), ("scope_path", "other"), ("declaration_index", True),
    ("patterns", ["**"]), ("source_path", "README.md"), ("origin", "system"),
])
def test_document_tampering_and_forged_origin_are_rejected(field, value):
    original = ancestor()
    with pytest.raises(ValueError):
        RuleDocument.model_validate({**original.model_dump(), field: value})


@pytest.mark.parametrize("kind,source,patterns,index", [
    ("path", "AGENTS.md", ("**",), 0),
    ("path", ".tracefix/rules/style.md", (), 0),
    ("ancestor", "AGENTS.md", (), 1),
    ("path", ".tracefix/rules/style.md", ("**a",), 0),
])
def test_invalid_scope_contract_cannot_be_constructed(kind, source, patterns, index):
    with pytest.raises(ValueError):
        RuleDocument.create(kind=kind, source_path=source, body=b"text",
                            patterns=patterns, declaration_index=index)


@pytest.mark.parametrize("docs", [
    lambda: [ancestor("src/AGENTS.md"), ancestor()],
    lambda: [path_rule(1), path_rule(0)],
    lambda: [path_rule(), ancestor()],
    lambda: [ancestor(), ancestor()],
    lambda: [ancestor("other/AGENTS.md")],
    lambda: [path_rule(pattern="tests/**")],
])
def test_selection_requires_applicable_unique_documents_in_exact_precedence(docs):
    with pytest.raises(ValueError):
        selection(docs())


def test_missing_sources_are_observations_not_invented_empty_files_or_facts():
    sources = [RuleSourceVersion(source_path="AGENTS.md", sha256=None),
               RuleSourceVersion(source_path=".tracefix/rules.toml", sha256=None)]
    selected = RuleSelection.create("code.py", (), sources)
    assert not selected.documents and selected.body_bytes == 0
    assert selected.sources[0].sha256 is None
    with pytest.raises(ValueError, match="source observation"):
        RuleSelection.create("code.py", (), [*sources, sources[0]])
    with pytest.raises(ValueError, match="source or identity"):
        RuleSelection.create("code.py", [ancestor()], sources)


def test_copied_models_and_saved_digest_drift_cannot_bypass_revalidation():
    doc = ancestor()
    with pytest.raises(ValueError, match="body digest"):
        selection([doc.model_copy(update={"body": "copied forgery"})])
    selected = selection([doc])
    for field in ("sha256", "bundle_sha256"):
        with pytest.raises(ValueError, match="digest mismatch"):
            RuleSelection.model_validate({**selected.model_dump(), field: "f" * 64})
    with pytest.raises(ValidationError):
        RuleSelection.model_validate({**selected.model_dump(), "case_sensitive": 1})


def test_document_and_selection_are_bounded_when_loaded_from_saved_json():
    doc = ancestor()
    with pytest.raises(ValueError, match="16 KiB"):
        RuleDocument.model_validate({**doc.model_dump(), "body": "x" * 16385})
    with pytest.raises(ValueError, match="fixed rule bodies"):
        raw = path_rule().model_dump()
        raw["scope_path"] = "src"
        RuleDocument.model_validate(raw)
