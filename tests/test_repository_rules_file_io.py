import pytest

from tracefix.repository_rules.errors import RuleError
from tracefix.repository_rules.file_io import BoundedWorkspaceReader
from tracefix.repository_rules.source import RuleSourceView


def test_generic_source_reader_keeps_guidance_port_restricted(tmp_path):
    (tmp_path / "a.py").write_bytes(b"raw\xff\n")
    reader = BoundedWorkspaceReader(tmp_path)
    assert reader.read_bytes("a.py", maximum=100) == b"raw\xff\n"
    with pytest.raises(RuleError, match="unsupported_source_path"):
        RuleSourceView(tmp_path).read_bytes("a.py")


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, 1024 * 1024 + 1])
def test_invalid_read_limits_fail_before_open(tmp_path, monkeypatch, limit):
    import os

    reader = BoundedWorkspaceReader(tmp_path)
    monkeypatch.setattr(os, "open", lambda *args: pytest.fail("invalid limit opened a file"))
    with pytest.raises(RuleError, match="read_limit_invalid"):
        reader.read_bytes("a.py", maximum=limit)


def test_caller_limit_is_enforced_before_read_and_missing_is_explicit(tmp_path, monkeypatch):
    import os

    (tmp_path / "a.py").write_bytes(b"too much")
    reader = BoundedWorkspaceReader(tmp_path)
    monkeypatch.setattr(os, "open", lambda *args: pytest.fail("oversize source was opened"))
    with pytest.raises(RuleError, match="limit_exceeded"):
        reader.read_bytes("a.py", maximum=2)
    assert reader.read_bytes("missing.py", maximum=2) is None
