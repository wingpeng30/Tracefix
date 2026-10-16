from __future__ import annotations

import pytest

from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.paths import (
    matches_glob,
    relative_path,
    rule_source_path,
    validate_glob,
)


@pytest.mark.parametrize("pattern,target,expected", [
    ("**", "code.py", True), ("**/*.py", "code.py", True),
    ("src/**/*.py", "src/code.py", True), ("src/**/*.py", "src/a/b/code.py", True),
    ("src/*.py", "src/a/code.py", False), ("src/?ode.py", "src/code.py", True),
    ("src/?ode.py", "src/decode.py", False), ("文档/**/*.md", "文档/说明.md", True),
    ("文档/?.md", "文档/😀.md", True), ("src/**/a/**/b.py", "src/a/b.py", True),
    ("src/**/a/**/b.py", "src/x/a/y/z/b.py", True),
    ("src/**/a/**/b.py", "src/x/b.py", False),
    ("src/**", "src2/a.py", False), ("src/(a|b).py", "src/a.py", False),
    ("a.py", "a.py/child", False), ("a.py/child", "a.py", False),
])
def test_globs_match_components_and_do_not_execute_regex(pattern, target, expected):
    assert matches_glob(pattern, target) is expected


def test_case_semantics_belong_to_trusted_backend_not_rule_text():
    assert not matches_glob("src/**/*.PY", "SRC/code.py")
    assert matches_glob("src/**/*.PY", "SRC/code.py", case_sensitive=False)


@pytest.mark.parametrize("path", [
    "", ".", "../AGENTS.md", "a/../AGENTS.md", "/tmp/AGENTS.md", "C:/AGENTS.md",
    "C:AGENTS.md", "\\\\server\\share\\AGENTS.md", "a\\AGENTS.md", "a//AGENTS.md",
    "a/./AGENTS.md", "a/AGENTS.md/", "a/AGENTS.md ", "a./AGENTS.md", "a\n/AGENTS.md",
    ".git/AGENTS.md", ".AWS/AGENTS.md", ".env/AGENTS.md", ".env.secret/AGENTS.md",
    ".config/gh/AGENTS.md", "credentials/AGENTS.md", ".tracefix-test-tmp/AGENTS.md",
    "AUX.md", "a/CON/AGENTS.md", "a/COM1.md", "a" * 1025,
    "/".join(["x"] * 65), "x\x7f.md",
])
def test_unsafe_ambiguous_and_internal_paths_are_refused_without_io(path):
    with pytest.raises(RuleError):
        relative_path(path)


@pytest.mark.parametrize("pattern", ["a/**b.py", "a/***/b.py", "a/[ab].py", "a/{b,c}",
                                     "!src/**", "a" * 513])
def test_unsupported_glob_syntax_is_explicitly_rejected(pattern):
    with pytest.raises(RuleError):
        validate_glob(pattern)


def test_only_fixed_rule_sources_and_portable_paths_are_accepted():
    for path in ("AGENTS.md", "a/b/AGENTS.md", ".tracefix/rules.toml",
                 ".tracefix/rules/python/style.md"):
        assert rule_source_path(path) == path
    assert relative_path(".", allow_root=True) == "."
    assert relative_path(".env.example") == ".env.example"
    with pytest.raises(RuleError, match="unsupported_source"):
        rule_source_path("README.md")
    with pytest.raises(RuleError):
        matches_glob("**", "../outside.py")
