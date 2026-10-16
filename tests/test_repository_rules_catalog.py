from __future__ import annotations

import pytest

from tracefix.repository_rules.catalog import RepositoryRules
from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.source import RuleSourceView


class Source:
    def __init__(self, files=()):
        self.files = dict(files)
        self.calls = []

    def read_bytes(self, path):
        self.calls.append(path)
        return self.files.get(path)


def configuration(*entries):
    text = "schema_version=1\n"
    for pattern, filename in entries:
        text += f'[[rules]]\npaths=["{pattern}"]\nfile="{filename}"\n'
    return text.encode()


def test_ancestor_chain_then_declared_rules_and_unmatched_bodies_are_not_read():
    source = Source({
        "AGENTS.md": b"root", "src/AGENTS.md": b"src", "src/pkg/AGENTS.md": b"pkg",
        ".tracefix/rules.toml": configuration(("src/**", "first.md"),
            ("tests/**", "unrelated.md"), ("src/**/*.py", "last.md")),
        ".tracefix/rules/first.md": b"first", ".tracefix/rules/last.md": b"last",
        ".tracefix/rules/unrelated.md": b"\xff",
        "AGENTS-parent.md": b"outside-guidance",
    })
    rules = RepositoryRules(source)
    result = rules.select("src/pkg/code.py")
    assert [doc.body for doc in result.documents] == ["root", "src", "pkg", "first", "last"]
    assert [doc.declaration_index for doc in result.documents[-2:]] == [0, 2]
    assert ".tracefix/rules/unrelated.md" not in source.calls
    assert "AGENTS-parent.md" not in source.calls
    assert result == rules.select("src/pkg/code.py")
    assert all(source.calls.count(entry.source_path) >= 2 for entry in result.sources)


def test_absent_rules_are_explicit_and_do_not_require_a_git_repository(tmp_path):
    view = RuleSourceView(tmp_path)
    result = RepositoryRules(view, validate_target=view.validate_target).select("new/code.py")
    assert not result.documents
    assert {ref.source_path: ref.sha256 for ref in result.sources} == {
        "AGENTS.md": None, "new/AGENTS.md": None, ".tracefix/rules.toml": None}


def test_current_checkout_changes_create_new_versions_without_mutating_old_selection():
    source = Source({"AGENTS.md": b"original"})
    rules = RepositoryRules(source)
    first = rules.select("src/code.py")
    source.files["AGENTS.md"] = b"changed"
    second = rules.select("src/code.py")
    assert first.documents[0].body == "original"
    assert first.documents[0].rule_id == second.documents[0].rule_id
    assert first.documents[0].version != second.documents[0].version
    del source.files["AGENTS.md"]
    assert not rules.select("src/code.py").documents
    assert first.documents[0].body == "original"


@pytest.mark.parametrize("content", [
    b"", b"\xff", b"schema_version=2", b"schema_version=true", b"[invalid",
    b"schema_version=1\nnetwork='enabled'",
    b"schema_version=1\n[[rules]]\npaths=['../**']\nfile='a.md'",
])
def test_malformed_or_permission_claiming_configuration_refuses_instead_of_partial_loading(content):
    source = Source({"AGENTS.md": b"root", ".tracefix/rules.toml": content})
    with pytest.raises(RuleError, match="configuration_invalid"):
        RepositoryRules(source).select("code.py")


def test_missing_declared_body_and_invalid_utf8_body_are_distinct_refusals():
    source = Source({".tracefix/rules.toml": configuration(("**", "missing.md"))})
    with pytest.raises(RuleError, match="declared_body_missing"):
        RepositoryRules(source).select("code.py")
    source.files[".tracefix/rules/missing.md"] = b"\xff"
    with pytest.raises(RuleError, match="body_not_utf8"):
        RepositoryRules(source).select("code.py")


@pytest.mark.parametrize("change", ["replace", "delete", "create_missing", "configuration"])
def test_changes_between_reads_cannot_publish_a_mixed_selection(change):
    class ChangingSource(Source):
        def read_bytes(self, path):
            value = super().read_bytes(path)
            if path == ".tracefix/rules.toml" and self.calls.count(path) == 1:
                if change == "replace":
                    self.files["AGENTS.md"] = b"different"
                elif change == "delete":
                    del self.files["AGENTS.md"]
                elif change == "create_missing":
                    self.files["src/AGENTS.md"] = b"new nested rule"
                else:
                    self.files[path] = b"schema_version=1\n#new version"
            return value

    source = ChangingSource({"AGENTS.md": b"original"})
    with pytest.raises(RuleError, match="changed_during_selection"):
        RepositoryRules(source).select("src/code.py")


def test_aliasing_ancestor_and_path_body_must_have_one_consistent_source_version():
    path = ".tracefix/rules/AGENTS.md"

    class ChangingSource(Source):
        def read_bytes(self, relative):
            data = super().read_bytes(relative)
            if relative == path:
                self.files[path] = b"new version"
            return data

    source = ChangingSource({path: b"first", ".tracefix/rules.toml":
                             configuration((".tracefix/rules/**", "AGENTS.md"))})
    with pytest.raises(RuleError, match="changed_during_selection"):
        RepositoryRules(source).select(".tracefix/rules/code.md")


def test_full_selection_limits_refuse_before_returning_any_partial_guidance():
    source = Source({"AGENTS.md": b"x" * 16384, "src/AGENTS.md": b"y" * 16384,
        ".tracefix/rules.toml": configuration(("**", "extra.md")),
        ".tracefix/rules/extra.md": b"more"})
    with pytest.raises(RuleError, match="selection_body_limit"):
        RepositoryRules(source).select("src/code.py")
    source = Source({".tracefix/rules.toml": configuration(
        *(("**", f"rule{index}.md") for index in range(17)))})
    with pytest.raises(RuleError, match="selection_document_limit"):
        RepositoryRules(source).select("code.py")
    assert not any(path.startswith(".tracefix/rules/") for path in source.calls)
    files = {"/".join([*(["nested"] * depth), "AGENTS.md"]): b"" for depth in range(17)}
    with pytest.raises(RuleError, match="selection_document_limit"):
        RepositoryRules(Source(files)).select("/".join([*(["nested"] * 16), "code.py"]))


@pytest.mark.parametrize("path,value", [("AGENTS.md", b"x" * 16385),
    (".tracefix/rules.toml", b"x" * 32769), ("AGENTS.md", "wrong adapter type")],
    ids=["oversized_rule", "oversized_config", "invalid_adapter"])
def test_even_a_source_adapter_cannot_bypass_limits_or_return_text_as_bytes(path, value):
    with pytest.raises(RuleError, match="source_limit|invalid_bytes"):
        RepositoryRules(Source({path: value})).select("code.py")


def test_invalid_or_protected_targets_are_checked_before_guidance_reads():
    source = Source({"AGENTS.md": b"must not read"})
    for target in ("../code.py", ".git/config", ".tracefix-test-tmp/code.py"):
        with pytest.raises(RuleError):
            RepositoryRules(source).select(target)
    assert source.calls == []
    with pytest.raises(RuleError, match="case_semantics"):
        RepositoryRules(source, case_sensitive=1)


def test_backend_case_semantics_and_legacy_source_port_export(tmp_path):
    from tracefix.memory_domain import SourceView as LegacyView
    from tracefix.source_view import SourceView

    assert LegacyView is SourceView
    source = Source({".tracefix/rules.toml": configuration(("SRC/*.PY", "a.md")),
                     ".tracefix/rules/a.md": b"python style"})
    assert not RepositoryRules(source).select("src/code.py").documents
    assert RepositoryRules(source, case_sensitive=False).select("src/code.py").documents
    view = RuleSourceView(tmp_path, protected_dirs=("private-evidence",))
    with pytest.raises(RuleError, match="internal"):
        RepositoryRules(view, validate_target=view.validate_target).select("private-evidence/x.py")
