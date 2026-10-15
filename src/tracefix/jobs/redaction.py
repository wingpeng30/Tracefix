"""Bounded append-only redaction; uncertain sensitive tails never become public prefixes."""

from __future__ import annotations

import codecs
import re

_BEGIN_LABELS = tuple(f"-----BEGIN {kind}PRIVATE KEY-----"
                      for kind in ("", "RSA ", "EC ", "OPENSSH "))
_BEGIN = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE)
_END = re.compile(r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"(?im)(authorization\s*:\s*bearer\s+|bearer\s+|"
    r"(?:api[_-]?key|access[_-]?token|secret|password)\s*[=:]\s*)[^\s,;\"']*"
)
_OPEN_FIELD = re.compile(
    r"(?im)(?:authorization|bearer|api[_-]?key|access[_-]?token|secret|password)"
    r"[ \t\r\n]*(?:[=:][ \t\r\n]*(?:bearer[ \t\r\n]*)?)?\Z"
)
_AWS = re.compile(r"AKIA[0-9A-Z]{16}", re.IGNORECASE)
_MARKER = "[REDACTED]"


class StreamingRedactor:
    """Literal/fixed-pattern span union avoids masking a label before its value is found."""

    maximum_pending_chars = 16 * 1024

    def __init__(self, *, secret_values: tuple[str, ...] = ()) -> None:
        self._secrets = tuple(sorted(set(filter(None, secret_values)), key=len, reverse=True))
        if (len(self._secrets) > 128 or sum(map(len, self._secrets)) > 64 * 1024
                or any(len(value) > 4096 for value in self._secrets)):
            raise ValueError("streaming secret exceeds bounded redaction contract")
        self._literal = (re.compile("(?=(" + "|".join(re.escape(value)
                                                     for value in self._secrets) + "))")
                         if self._secrets else None)
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

    @staticmethod
    def _paint(mask: bytearray, start: int, end: int) -> None:
        mask[start:end] = b"\1" * (end - start)

    def _masks(self, text: str, *, final: bool) -> tuple[bytearray, bytearray, int | None]:
        output, safety = bytearray(len(text)), bytearray(len(text))
        unfinished = None

        def span(start: int, end: int) -> None:
            self._paint(output, start, end)
            self._paint(safety, start, end)

        if self._literal is not None:
            for match in self._literal.finditer(text):
                span(match.start(), match.start() + len(match.group(1)))
        if final:
            for value in self._secrets:
                for length in range(min(len(value) - 1, len(text)), 0, -1):
                    if text.endswith(value[:length]):
                        span(len(text) - length, len(text))
                        break
        for match in _CREDENTIAL.finditer(text):
            self._paint(output, match.end(1), match.end())
            self._paint(safety, match.start(), match.end())
            if match.end() == len(text):
                unfinished = (min(unfinished, match.start()) if unfinished is not None
                              else match.start())
        for match in _AWS.finditer(text):
            span(match.start(), match.end())
        cursor = 0
        while opening := _BEGIN.search(text, cursor):
            end = _END.search(text, opening.end())
            span(opening.start(), end.end() if end else len(text))
            if end is None:
                unfinished = (min(unfinished, opening.start()) if unfinished is not None
                              else opening.start())
                break
            cursor = end.end()
        open_field = _OPEN_FIELD.search(text)
        if open_field:
            unfinished = (min(unfinished, open_field.start()) if unfinished is not None
                          else open_field.start())
        return output, safety, unfinished

    @staticmethod
    def _safe_cut(safety: bytearray, cut: int) -> int:
        if 0 < cut < len(safety) and safety[cut - 1] and safety[cut]:
            while cut and safety[cut - 1]:
                cut -= 1
        return cut

    @staticmethod
    def _render(text: str, mask: bytearray, cut: int) -> str:
        parts, cursor = [], 0
        while cursor < cut:
            start = mask.find(b"\1", cursor, cut)
            if start < 0:
                parts.append(text[cursor:cut])
                break
            parts.extend((text[cursor:start], _MARKER))
            cursor = start + 1
            while cursor < cut and mask[cursor]:
                cursor += 1
        return "".join(parts)

    def _discard_suppressed(self) -> bool:
        if self._suppressed == "credential":
            self._pending = self._pending.lstrip()
            if not self._pending:
                return False
            if _BEGIN.match(self._pending):
                self._suppressed = "key"
            elif any(label.casefold().startswith(self._pending.casefold())
                     for label in _BEGIN_LABELS):
                return False
            else:
                self._suppressed = "line"
        if self._suppressed == "key":
            end = _END.search(self._pending)
            if end is None:
                self._pending = self._pending[-128:]
                return False
            self._pending = self._pending[end.end():]
        elif self._suppressed == "line":
            at = self._pending.find("\n")
            if at < 0:
                self._pending = ""
                return False
            self._pending = self._pending[at + 1:]
        if self._suppressed:
            self._skip = self._tail
            self._suppressed = None
        if self._skip:
            consumed = min(self._skip, len(self._pending))
            self._pending = self._pending[consumed:]
            self._skip -= consumed
        return True

    def _advance(self, *, final: bool = False) -> str:
        if not self._discard_suppressed():
            return ""
        output, safety, unfinished = self._masks(self._pending, final=final)
        cut = len(self._pending) if final else self._pending.rfind(
            "\n", 0, max(0, len(self._pending) - self._tail)) + 1
        if not final:
            if unfinished is not None:
                cut = min(cut, unfinished)
            cut = self._safe_cut(safety, cut)
            if len(self._pending) - cut > self.maximum_pending_chars:
                candidate = max(cut, len(self._pending) - max(self._tail, 128))
                if unfinished is not None:
                    candidate = min(candidate, unfinished)
                cut = max(cut, self._safe_cut(safety, candidate))
        result = self._render(self._pending, output, cut)
        self._pending = self._pending[cut:]
        if len(self._pending) > self.maximum_pending_chars:
            self.incomplete = True
            self._suppressed = ("key" if _BEGIN.search(self._pending) else "credential"
                                if _OPEN_FIELD.search(self._pending) else "line")
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
