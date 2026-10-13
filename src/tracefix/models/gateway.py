"""请求身份、输入计数复用和发送前冻结。"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from tracefix.exceptions import LLMProviderError
from tracefix.models.input_bounds import InputBound


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class RequestMeasurement:
    """Only count metadata is retained; request bodies remain in the caller."""

    input_sha256: str
    counter_identity: str
    input_bound: InputBound
    conservative_input_tokens: int | None


@dataclass(frozen=True)
class PreparedRequest:
    """An immutable serialized request and the identities used to audit it."""

    payload: bytes
    request_sha256: str
    input_sha256: str
    measurement: RequestMeasurement | None

    def kwargs(self) -> dict:
        return json.loads(self.payload.decode("utf-8"))


class LLMGateway:
    """Share input counts while freezing the exact final provider request."""

    _NON_INPUT_SETTINGS = frozenset({"max_tokens", "timeout", "num_retries", "temperature"})

    def __init__(self, *, max_entries: int = 128) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._measurements: OrderedDict[str, RequestMeasurement] = OrderedDict()
        self._request_guards: dict[str, str] = {}
        self._counter_identity: str | None = None
        self._closed = False

    @staticmethod
    def _input_payload(kwargs: dict, counter_identity: str) -> dict:
        # Retain every request field that can affect rendering/counting. The
        # output ceiling and transport controls do not affect input tokenization.
        return {
            "counter_identity": counter_identity,
            "request": {
                key: value for key, value in kwargs.items()
                if key not in LLMGateway._NON_INPUT_SETTINGS
            },
        }

    def measure(
        self,
        kwargs: dict,
        *,
        counter_identity: str,
        compute: Callable[[], tuple[InputBound, int | None]],
    ) -> RequestMeasurement:
        if self._closed:
            raise LLMProviderError("LLM gateway is closed")
        if self._counter_identity is None:
            self._counter_identity = counter_identity
        elif self._counter_identity != counter_identity:
            raise LLMProviderError("LLM counter identity changed within a gateway session")

        input_payload = self._input_payload(kwargs, counter_identity)
        input_sha = sha256_bytes(canonical_json(input_payload))
        request_guard = sha256_bytes(canonical_json({
            key: value for key, value in kwargs.items() if key != "max_tokens"
        }))
        cached = self._measurements.get(input_sha)
        if cached is not None:
            if self._request_guards.get(input_sha) != request_guard:
                raise LLMProviderError("serialized request changed after input counting")
            self._measurements.move_to_end(input_sha)
            return cached

        bound, conservative = compute()
        if conservative is not None and conservative <= 0:
            raise ValueError("conservative input bound must be positive")
        measurement = RequestMeasurement(
            input_sha256=input_sha,
            counter_identity=counter_identity,
            input_bound=bound,
            conservative_input_tokens=conservative,
        )
        # Unavailable is still a result: retain its status and the independent
        # conservative estimate so repeated window/fee checks share one pass.
        # Callers that require a qualified count continue to reject this status.
        self._measurements[input_sha] = measurement
        self._request_guards[input_sha] = request_guard
        self._measurements.move_to_end(input_sha)
        while len(self._measurements) > self._max_entries:
            evicted, _ = self._measurements.popitem(last=False)
            self._request_guards.pop(evicted, None)
        return measurement

    def freeze(
        self, kwargs: dict, *, counter_identity: str | None = None
    ) -> PreparedRequest:
        if self._closed:
            raise LLMProviderError("LLM gateway is closed")
        if counter_identity is not None:
            if self._counter_identity is None:
                self._counter_identity = counter_identity
            elif self._counter_identity != counter_identity:
                raise LLMProviderError("LLM counter identity changed within a gateway session")
        payload = canonical_json(kwargs)
        request_sha = sha256_bytes(payload)
        measurement = None
        input_sha = ""
        if self._counter_identity is not None:
            input_sha = sha256_bytes(canonical_json(
                self._input_payload(kwargs, self._counter_identity)
            ))
            guard = self._request_guards.get(input_sha)
            if guard is not None and guard != sha256_bytes(canonical_json({
                key: value for key, value in kwargs.items() if key != "max_tokens"
            })):
                raise LLMProviderError("serialized request changed after input counting")
            measurement = self._measurements.get(input_sha)
            if measurement is not None and measurement.counter_identity != self._counter_identity:
                raise LLMProviderError("prepared request tokenizer identity changed")
        return PreparedRequest(payload, request_sha, input_sha, measurement)

    def validate(self, request: PreparedRequest) -> dict:
        if self._closed:
            raise LLMProviderError("LLM gateway is closed")
        if sha256_bytes(request.payload) != request.request_sha256:
            raise LLMProviderError("prepared request payload changed")
        if self._counter_identity is None or request.measurement is None:
            raise LLMProviderError("prepared request has no gateway input measurement")
        if request.measurement.counter_identity != self._counter_identity:
            raise LLMProviderError("prepared request counter identity changed")
        kwargs = request.kwargs()
        input_sha = sha256_bytes(canonical_json(
            self._input_payload(kwargs, self._counter_identity)
        ))
        if (
            input_sha != request.input_sha256
            or input_sha != request.measurement.input_sha256
        ):
            raise LLMProviderError("prepared request input identity changed")
        return kwargs

    def close(self) -> None:
        self._measurements.clear()
        self._request_guards.clear()
        self._closed = True
