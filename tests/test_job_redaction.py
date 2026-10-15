from __future__ import annotations

import pytest

from tracefix.jobs.redaction import StreamingRedactor


def test_utf8_and_secret_split_never_publish_a_partial_secret():
    redactor = StreamingRedactor(secret_values=("secret-12345",))
    payload = "中文🙂 start\npassword=secret-12345\nfinish\n".encode()
    output = []
    for value in payload:
        output.append(redactor.feed(bytes([value])))
        assert "secret-12345" not in "".join(output)
    output.append(redactor.finish())
    result = "".join(output)
    assert "中文🙂 start" in result and "finish" in result
    assert "[REDACTED]" in result and "secret-12345" not in result
    assert "�" not in result


def test_multiline_configured_secret_is_hidden_across_record_boundaries():
    redactor = StreamingRedactor(secret_values=("first\nsecond",))
    result = redactor.feed(b"prefix\nfirst\n")
    assert "first" not in result
    result += redactor.feed(b"second\nsuffix\n") + redactor.finish()
    assert "first" not in result and "second" not in result
    assert "prefix" in result and "suffix" in result


def test_open_private_key_and_long_sensitive_fragment_are_never_delivered():
    redactor = StreamingRedactor()
    result = redactor.feed(b"safe\n-----BEGIN PRIVATE KEY-----\n")
    result += redactor.feed(b"sensitive-key-material\n" * 3000)
    result += redactor.feed(b"still-secret\n-----END PRIVATE KEY-----\npublic\n")
    result += redactor.finish()
    assert "safe" in result and "public" in result
    assert "sensitive-key-material" not in result and "still-secret" not in result
    assert "[REDACTED]" in result


def test_long_unterminated_line_is_bounded_and_does_not_poison_following_output():
    redactor = StreamingRedactor()
    result = ""
    for _ in range(20):
        result += redactor.feed(b"password=" + b"x" * 4000)
        assert redactor.pending_chars <= redactor.maximum_pending_chars
    result += redactor.feed(b"\nnext-safe-line\n") + redactor.finish()
    assert "xxxx" not in result and "next-safe-line" in result
    assert "[REDACTED]" in result


def test_final_short_credential_invalid_bytes_and_single_finish():
    redactor = StreamingRedactor()
    result = redactor.feed(b"ok\xff\npassword=abc") + redactor.finish()
    assert "ok�" in result and "abc" not in result
    assert redactor.finish() == ""
    with pytest.raises(ValueError, match="closed"):
        redactor.feed(b"later")


def test_credential_value_on_following_record_waits_for_safe_redaction():
    redactor = StreamingRedactor()
    result = redactor.feed(b"public\npassword=\n")
    assert result == "public\n"
    result += redactor.feed(b"short\nend\n") + redactor.finish()
    assert "short" not in result and "end" in result


def test_quota_cut_in_configured_secret_cannot_expose_its_unfinished_prefix():
    redactor = StreamingRedactor(secret_values=("value-that-is-private",))
    result = redactor.feed(b"message\nvalue-that-") + redactor.finish()
    assert "message" in result and "value-that-" not in result
