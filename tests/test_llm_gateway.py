from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tracefix.comparison_counter import serve
from tracefix.comparison_profiles import OfficialCounter
from tracefix.exceptions import LLMProviderError
from tracefix.messages import Message, MessageRole
from tracefix.models.base import LLMConfig
from tracefix.models.gateway import LLMGateway
from tracefix.models.input_bounds import InputBound
from tracefix.models.litellm_adapter import LiteLLMAdapter


class FakeClient:
    def __init__(self):
        self.calls = []

    def token_counter(self, **kwargs):
        return 23

    def completion(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "model": "provider/model",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        }


def test_input_measurement_is_reused_when_only_output_limit_changes(monkeypatch):
    adapter = LiteLLMAdapter(
        LLMConfig(model_name="provider/model", max_output_tokens=100), client=FakeClient()
    )
    counts = {"official": 0, "bound": 0}

    def official(kwargs):
        counts["official"] += 1
        return InputBound(17, "verified_upper_bound", "fixture", "fixture")

    def conservative(kwargs):
        counts["bound"] += 1
        return 99

    monkeypatch.setattr(adapter, "_count_input_bound_for_kwargs", official)
    monkeypatch.setattr(adapter, "_count_input_tokens_for_kwargs", conservative)
    messages = [Message(role=MessageRole.USER, content="same request")]

    assert adapter.count_input_bound(messages).tokens == 17
    assert adapter.count_input_tokens(messages) == 99
    adapter.config.max_output_tokens = 50
    assert adapter.count_input_bound(messages).tokens == 17
    prepared = adapter.prepare_request(messages)
    adapter.complete_prepared(prepared)

    assert counts == {"official": 1, "bound": 1}
    assert adapter.client.calls[0]["max_tokens"] == 50
    assert prepared.request_sha256 == hashlib.sha256(prepared.payload).hexdigest()
    assert prepared.measurement.conservative_input_tokens == 99


def test_gateway_recounts_changed_body_and_refuses_other_request_changes(monkeypatch):
    adapter = LiteLLMAdapter(
        LLMConfig(model_name="provider/model", max_output_tokens=100), client=FakeClient()
    )
    count = 0

    def official(kwargs):
        nonlocal count
        count += 1
        return InputBound(17, "verified_upper_bound", "fixture", "fixture")

    monkeypatch.setattr(adapter, "_count_input_bound_for_kwargs", official)
    monkeypatch.setattr(adapter, "_count_input_tokens_for_kwargs", lambda _: 99)
    first = [Message(role=MessageRole.USER, content="first")]
    adapter.count_input_bound(first)
    adapter.config.max_retries = 1
    with pytest.raises(LLMProviderError, match="changed after input counting"):
        adapter.prepare_request(first)

    changed = [Message(role=MessageRole.USER, content="second")]
    adapter.count_input_bound(changed)
    assert count == 2


def test_prepare_recounts_changed_body_and_rejects_counter_identity_drift(monkeypatch):
    client = FakeClient()
    adapter = LiteLLMAdapter(LLMConfig(model_name="provider/model"), client=client)
    calls = 0

    def official(kwargs):
        nonlocal calls
        calls += 1
        return InputBound(17, "verified_upper_bound", "fixture", "fixture")

    monkeypatch.setattr(adapter, "_count_input_bound_for_kwargs", official)
    monkeypatch.setattr(adapter, "_count_input_tokens_for_kwargs", lambda _: 99)
    original = [Message(role=MessageRole.USER, content="counted before send")]
    changed = [Message(role=MessageRole.USER, content="changed before send")]

    adapter.count_input_bound(original)
    adapter.complete(changed)
    assert calls == 2
    assert len(client.calls) == 1
    assert client.calls[0]["messages"][0]["content"] == "changed before send"

    adapter.config.model_name = "provider/other-model"
    with pytest.raises(LLMProviderError, match="counter identity changed"):
        adapter.complete(changed)
    assert len(client.calls) == 1


def test_frozen_send_rejects_payload_changed_after_request_preparation():
    client = FakeClient()
    adapter = LiteLLMAdapter(LLMConfig(model_name="provider/model"), client=client)
    messages = [Message(role=MessageRole.USER, content="original")]
    prepared = adapter.prepare_request(messages)
    kwargs = prepared.kwargs()
    kwargs["messages"][0]["content"] = "tampered"
    payload = json.dumps(
        kwargs, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    tampered = replace(
        prepared,
        payload=payload,
        request_sha256=hashlib.sha256(payload).hexdigest(),
    )

    with pytest.raises(LLMProviderError, match="input identity changed"):
        adapter.complete_prepared(tampered)
    assert client.calls == []


def test_gateway_recounts_tool_and_thinking_changes(monkeypatch):
    adapter = LiteLLMAdapter(
        LLMConfig(
            model_name="deepseek/deepseek-flash",
            extra_kwargs={
                "api_base": "https://api.deepseek.com",
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        ),
        client=FakeClient(),
    )
    calls = 0

    def official(_kwargs):
        nonlocal calls
        calls += 1
        return InputBound(17, "estimate", "fixture", "fixture")

    monkeypatch.setattr(adapter, "_count_input_bound_for_kwargs", official)
    monkeypatch.setattr(adapter, "_count_input_tokens_for_kwargs", lambda _: 99)
    message = [Message(role=MessageRole.USER, content="same body")]
    from tracefix.tools.base import ToolSpec

    tool = ToolSpec(
        name="read_file",
        description="Read source",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    adapter.count_input_bound(message)
    adapter.count_input_bound(message, [tool])
    adapter.config.extra_kwargs["extra_body"]["thinking"] = {"type": "enabled"}
    adapter.count_input_bound(message, [tool])

    assert calls == 3


def test_gateway_rejects_supplier_endpoint_identity_drift():
    adapter = LiteLLMAdapter(
        LLMConfig(
            model_name="deepseek/deepseek-flash",
            extra_kwargs={
                "api_base": "https://api.deepseek.com",
                "extra_body": {"thinking": {"type": "disabled"}},
            },
        ),
        client=FakeClient(),
    )
    messages = [Message(role=MessageRole.USER, content="identity check")]
    adapter.count_input_bound(messages)
    adapter.config.extra_kwargs["api_base"] = "https://proxy.invalid"

    with pytest.raises(LLMProviderError, match="counter identity changed"):
        adapter.count_input_bound(messages)


def test_gateway_cache_is_bounded_and_close_is_terminal():
    gateway = LLMGateway(max_entries=2)
    calls = 0

    def compute():
        nonlocal calls
        calls += 1
        return InputBound(1, "estimate", "test", "test"), 10

    for content in ("a", "b", "c"):
        gateway.measure({"model": "m", "messages": [{"content": content}]},
                        counter_identity="counter", compute=compute)
    assert calls == 3
    assert len(gateway._measurements) == 2
    gateway.close()
    gateway.close()
    with pytest.raises(LLMProviderError, match="closed"):
        gateway.freeze({"model": "m"})


def _fake_worker(tmp_path: Path, mode: str = "ok") -> tuple[OfficialCounter, Path]:
    starts = tmp_path / "starts.txt"
    worker = tmp_path / "fake_counter.py"
    worker.write_text(
        "import hashlib,json,sys,time\n"
        "mode,starts=sys.argv[1:3]\n"
        "open(starts,'a').write('start\\n')\n"
        "identity={'worker':'different' if mode=='startup-drift' else 'fixture-v1'}\n"
        "digest=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()\n"
        "print(json.dumps({'protocol':1,'ready':True,'identity':identity,'identity_sha256':digest},separators=(',',':')),flush=True)\n"
        "for line in sys.stdin:\n"
        " request=json.loads(line)\n"
        " if mode=='stderr': sys.stderr.write('x'*100000); sys.stderr.flush()\n"
        " if mode=='delay': time.sleep(2)\n"
        " if mode=='eof': break\n"
        " if mode=='malformed': print('not-json',flush=True); break\n"
        " rid=request['id']+(1 if mode=='wrong-id' else 0)\n"
        " counts=[{'tokens':len(body.get('messages',[])),'status':'estimate',"
        "'method':'fixture','identity':'fixture','request_sha256':None} "
        "for body in request['bodies']]\n"
        " response={'protocol':1,'id':rid,'identity_sha256':digest,'ok':True,'counts':counts}\n"
        " if mode=='identity-drift': response['identity_sha256']='0'*64\n"
        " print(json.dumps(response,separators=(',',':')),flush=True)\n",
        encoding="utf-8",
    )
    return (
        OfficialCounter({
            "command": [sys.executable, str(worker), mode, str(starts)],
            "mode": "jsonl-v1",
            "identity": {"worker": "fixture-v1"},
        }, timeout_seconds=0.1 if mode == "delay" else 5),
        starts,
    )


def _count_request() -> dict:
    return {"model": "deepseek-flash", "messages": [{"role": "user", "content": "x"}]}


@pytest.mark.parametrize("persistent", [False, True])
def test_counter_client_preserves_unicode_under_non_utf8_environment(
    tmp_path, monkeypatch, persistent
):
    monkeypatch.setenv("PYTHONIOENCODING", "gbk")
    monkeypatch.setenv("PYTHONUTF8", "0")
    content = "修复中文请求，保留原文与工具参数。 café 🐍"
    identity = {"worker": "中文计数器"}
    worker = tmp_path / "unicode_counter.py"
    worker.write_text(
        "import hashlib,json,sys\n"
        "identity=" + ascii(identity) + "\n"
        "assert sys.stdin.encoding.lower() == 'utf-8'\n"
        "assert sys.stdout.encoding.lower() == 'utf-8'\n"
        "digest=hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False,"
        "separators=(',',':')).encode('utf-8')).hexdigest()\n"
        "def counts(request):\n"
        " return [{'tokens':len(body['messages'][0]['content']),'status':'estimate',"
        "'method':body['messages'][0]['content'],'identity':'fixture',"
        "'request_sha256':None} for body in request['bodies']]\n"
        "if '--serve' in sys.argv:\n"
        " print(json.dumps({'protocol':1,'ready':True,'identity':identity,"
        "'identity_sha256':digest},ensure_ascii=False),flush=True)\n"
        " for line in sys.stdin:\n"
        "  request=json.loads(line)\n"
        "  print(json.dumps({'protocol':1,'id':request['id'],'ok':True,"
        "'identity_sha256':digest,'counts':counts(request)},ensure_ascii=False),flush=True)\n"
        "else:\n"
        " print(json.dumps({'identity':identity,'counts':counts(json.load(sys.stdin))},"
        "ensure_ascii=False))\n",
        encoding="utf-8",
    )
    runtime = {"command": [sys.executable, str(worker)], "identity": identity}
    if persistent:
        runtime["mode"] = "jsonl-v1"
    counter = OfficialCounter(runtime)
    request = {"model": "deepseek-flash", "messages": [{"content": content}]}
    try:
        for _ in range(2):
            measured = counter.count(request)
            assert measured.tokens == len(content)
            assert measured.method == content
    finally:
        counter.close()
    assert os.environ["PYTHONIOENCODING"] == "gbk"


@pytest.mark.parametrize("persistent", [False, True])
def test_real_counter_entrypoint_overrides_gbk_stdio(tmp_path, persistent):
    content = "你是 TraceFix，修复中文 JSON。 café 🐍"
    worker = tmp_path / "entrypoint.py"
    worker.write_text(
        "import sys\n"
        "from tracefix import comparison_counter as worker\n"
        "from tracefix.models.input_bounds import InputBound\n"
        "import tracefix.models.input_bounds as bounds\n"
        "for stream in (sys.stdin,sys.stdout,sys.stderr): stream.reconfigure(encoding='gbk')\n"
        "worker.identity=lambda *args,**kwargs: {'worker':'fixture'}\n"
        "worker._runtime_files=lambda *args: ()\n"
        "bounds._cached_v41_encoding=lambda *args: object()\n"
        "worker.count_deepseek_v41_request=lambda body: InputBound("
        "len(body['messages'][0]['content']),'estimate',body['messages'][0]['content'],"
        "'fixture')\n"
        "raise SystemExit(worker.main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text("explicit offline fixture", encoding="utf-8")
    bodies = [{"messages": [{"content": content}]}]
    command = [sys.executable, str(worker), "--tokenizer", str(tokenizer)]
    if persistent:
        command.append("--serve")
        payload = "".join(json.dumps(
            {"protocol": 1, "id": rid, "bodies": bodies}, ensure_ascii=False
        ) + "\n" for rid in (1, 2))
    else:
        payload = json.dumps({"bodies": bodies}, ensure_ascii=False)
    result = subprocess.run(
        command, input=payload.encode("utf-8"), capture_output=True, timeout=15,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
             "PYTHONIOENCODING": "gbk", "PYTHONUTF8": "0"},
    )
    assert result.returncode == 0, result.stderr.decode("utf-8")
    frames = [json.loads(line) for line in result.stdout.decode("utf-8").splitlines()]
    responses = frames[1:] if persistent else frames
    assert len(responses) == (2 if persistent else 1)
    for response in responses:
        assert response["counts"][0]["tokens"] == len(content)
        assert response["counts"][0]["method"] == content


def test_official_counter_reuses_one_process_and_drains_stderr(tmp_path, monkeypatch):
    counter, starts = _fake_worker(tmp_path, "stderr")
    timeouts = []
    original_invoke = counter._persistent_invoke

    def capture_deadline(bodies, timeout):
        timeouts.append(timeout)
        return original_invoke(bodies, timeout)

    monkeypatch.setattr(counter, "_persistent_invoke", capture_deadline)
    try:
        assert counter.count(_count_request()).tokens == 1
        assert counter.count(_count_request()).tokens == 1
        assert [item.tokens for item in counter.count_many(
            [_count_request(), _count_request()]
        )] == [1, 1]
        assert starts.read_text(encoding="utf-8").splitlines() == ["start"]
        assert timeouts == [5, 5, 1800.0]
    finally:
        counter.close()
        counter.close()
    assert counter._process is None


@pytest.mark.parametrize(
    "mode",
    ["wrong-id", "identity-drift", "startup-drift", "malformed", "eof", "delay"],
)
def test_official_counter_fails_closed_on_worker_protocol_errors(tmp_path, mode):
    counter, starts = _fake_worker(tmp_path, mode)
    try:
        with pytest.raises((ValueError, TimeoutError)):
            counter.count(_count_request())
        with pytest.raises(ValueError, match="closed or failed"):
            counter.count(_count_request())
    finally:
        counter.close()
    assert starts.read_text(encoding="utf-8").splitlines() == ["start"]
    assert counter._process is None


def test_counter_worker_serves_multiple_frames_and_preserves_one_shot_module(
    monkeypatch, tmp_path, capsys
):
    import io

    import tracefix.comparison_counter as worker

    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text("fixture", encoding="utf-8")
    identity = {"worker": "fixture-v1"}
    identity_calls = []
    encoding_loads = []
    monkeypatch.setattr(
        worker, "identity", lambda _, **__: identity_calls.append(1) or identity
    )
    monkeypatch.setattr(worker, "_runtime_files", lambda _: ((str(tokenizer), 7, 1),))
    monkeypatch.setattr(
        "tracefix.models.input_bounds._cached_v41_encoding",
        lambda *args: encoding_loads.append(args) or object(),
    )
    monkeypatch.setattr(worker, "count_deepseek_v41_request", lambda body: InputBound(
        len(body["messages"]), "estimate", "fixture", "fixture"
    ))
    frames = [
        {"protocol": 1, "id": 1, "bodies": [_count_request()]},
        {"protocol": 1, "id": 2, "bodies": [_count_request()]},
    ]
    monkeypatch.setattr(
        worker.sys, "stdin", io.StringIO("".join(json.dumps(frame) + "\n" for frame in frames))
    )

    assert serve(tokenizer) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]["ready"] is True
    assert [frame["id"] for frame in output[1:]] == [1, 2]
    assert [frame["counts"][0]["tokens"] for frame in output[1:]] == [1, 1]
    assert identity_calls == [1]
    assert len(encoding_loads) == 1


def test_counter_worker_stops_when_runtime_file_identity_drifts(
    monkeypatch, tmp_path, capsys
):
    import io

    import tracefix.comparison_counter as worker

    tokenizer = tmp_path / "tokenizer.json"
    tokenizer.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(
        worker, "identity", lambda _, **__: {"worker": "fixture-v1"}
    )
    fingerprints = iter([
        ((str(tokenizer), 7, 1),),
        ((str(tokenizer), 8, 2),),
    ])
    monkeypatch.setattr(worker, "_runtime_files", lambda _: next(fingerprints))
    monkeypatch.setattr(
        "tracefix.models.input_bounds._cached_v41_encoding", lambda *args: object()
    )
    monkeypatch.setattr(
        worker, "count_deepseek_v41_request",
        lambda _body: pytest.fail("must reject changed files before counting"),
    )
    frame = {"protocol": 1, "id": 1, "bodies": [_count_request()]}
    monkeypatch.setattr(worker.sys, "stdin", io.StringIO(json.dumps(frame) + "\n"))

    assert worker.serve(tokenizer) == 1
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]["ready"] is True
    assert output[1]["ok"] is False
    assert output[1]["error"] == "ValueError"
