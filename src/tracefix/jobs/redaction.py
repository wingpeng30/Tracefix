"""Bounded streaming redaction: publish complete safe records, withhold uncertain tails."""

from __future__ import annotations

import codecs
import re

_BEGIN = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE)
_END = re.compile(r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"(?im)(authorization\s*:\s*bearer\s+|bearer\s+|"
    r"(?:api[_-]?key|access[_-]?token|secret|password)\s*[=:]\s*)[^\s,;\"']*"
)
_AWS = re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE)
_OPEN_CREDENTIAL = re.compile(
    r"(?im)(authorization\s*:\s*bearer|bearer|"
    r"(?:api[_-]?key|access[_-]?token|secret|password)[ \t]*[=:])[ \t\r\n]*\Z"
)
_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
                  r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.DOTALL | re.IGNORECASE)
_MARKER = "[REDACTED]"


class StreamingRedactor:
    """Append-only UTF-8 view; long uncertain records are hidden instead of clipped."""

    maximum_pending_chars = 16 * 1024

    def __init__(self, *, secret_values: tuple[str, ...] = ()) -> None:
        self._secrets = tuple(sorted(set(filter(None, secret_values)), key=len, reverse=True))
        if (len(self._secrets) > 128 or sum(map(len, self._secrets)) > 64 * 1024
                or any(len(value) > 4096 for value in self._secrets)):
            raise ValueError("streaming secret exceeds bounded redaction contract")
        self._tail = max((len(value) - 1 for value in self._secrets), default=0)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""
        self._suppressed: str | None = None
        self._skip = 0
        self._closed = False
        self.incomplete = False

    @property
    def pending_chars(self) -> int:
        return len(self._pending)

    def _redact(self, text: str, *, final: bool = False) -> str:
        if final:
            longest = 0
            for value in self._secrets:
                for length in range(min(len(value) - 1, len(text)), 0, -1):
                    if text.endswith(value[:length]):
                        longest = max(longest, length)
                        break
            if longest:
                text = text[:-longest] + _MARKER
        for value in self._secrets:
            text = text.replace(value, _MARKER)
        text = _KEY.sub(_MARKER, text)
        # A final unterminated key is never exposed as an ordinary line.
        opening = _BEGIN.search(text)
        if opening:
            text = text[:opening.start()] + _MARKER
        text = _CREDENTIAL.sub(lambda match: match.group(1) + _MARKER, text)
        return _AWS.sub(_MARKER, text)

    def _advance(self, *, final: bool = False) -> str:
        if self._suppressed:
            if self._suppressed == "key":
                end = _END.search(self._pending)
                if end is None:
                    self._pending = self._pending[-128:]
                    return ""
                self._pending = self._pending[end.end():]
            else:
                end_line = self._pending.find("\n")
                if end_line < 0:
                    self._pending = ""
                    return ""
                self._pending = self._pending[end_line + 1:]
                # A secret may straddle a hidden line boundary; hide its possible suffix too.
                self._skip = self._tail
            self._suppressed = None
        if self._skip:
            consumed = min(self._skip, len(self._pending))
            self._pending = self._pending[consumed:]
            self._skip -= consumed
        cut = len(self._pending) if final else self._pending.rfind(
            "\n", 0, max(0, len(self._pending) - self._tail)
        ) + 1
        if not final:
            for value in self._secrets:
                at = self._pending.find(value)
                while 0 <= at < cut:
                    if at + len(value) > cut:
                        cut = self._pending.rfind("\n", 0, at) + 1
                        break
                    at = self._pending.find(value, at + len(value))
            opening = _BEGIN.search(self._pending)
            if opening and opening.start() < cut:
                end = _END.search(self._pending, opening.end())
                if end is None or end.end() > cut:
                    cut = self._pending.rfind("\n", 0, opening.start()) + 1
            open_credential = _OPEN_CREDENTIAL.search(self._pending[:cut])
            if open_credential:
                cut = self._pending.rfind("\n", 0, open_credential.start()) + 1
        result = self._redact(self._pending[:cut], final=final)
        self._pending = self._pending[cut:]
        if len(self._pending) > self.maximum_pending_chars:
            self.incomplete = True
            self._suppressed = "key" if _BEGIN.search(self._pending) else "line"
            self._pending = self._pending[-128:] if self._suppressed == "key" else ""
            result += _MARKER
        return result

    def feed(self, data: bytes) -> str:
        if self._closed:
            raise ValueError("streaming redactor is closed")
        output = []
        for offset in range(0, len(data), 1024):
            self._pending += self._decoder.decode(data[offset:offset + 1024])
            output.append(self._advance())
        return "".join(output)

    def finish(self) -> str:
        if self._closed:
            return ""
        self._closed = True
        self._pending += self._decoder.decode(b"", final=True)
        return self._advance(final=True)
