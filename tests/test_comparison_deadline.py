"""B/C protocol and wall-clock request deadlines, using zero supplier calls."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from test_comparison import PATCH, messages, provider_config

from tracefix.comparison import ComparisonBudget, digest, read_json, schedule, summarize, write_json
from tracefix.comparison_campaign import FixtureClient
from tracefix.comparison_funding import freeze_parent_budget, prior_spend
from tracefix.comparison_profiles import BC_HOLDOUT_PROFILE, HOLDOUT_PROFILE, profile_config
from tracefix.comparison_transport import HardDeadlineClient, main, transport_identity
from tracefix.exceptions import LLMProviderError, PreRequestBudgetExceeded


def test_bc_schedule_balanced_and_previous_protocol_unchanged():
    selected = profile_config(BC_HOLDOUT_PROFILE)
    tasks = [f"repo{i % 4}__task-{i}" for i in range(20)]
    rows = schedule(selected, tasks)
    assert len(rows) == 120
    assert Counter(tuple(r["arm"] for r in rows[i : i + 2]) for i in range(0, 120, 2)) == {
        ("B", "C"): 30,
        ("C", "B"): 30,
    }
    assert set(Counter((r["task_id"], r["arm"]) for r in rows).values()) == {3}
    assert selected["hard_request_deadline_seconds"] == selected["limits"]["timeout_seconds"] == 180
    assert selected["limits"]["active_seconds"] == 3600
    old = profile_config(HOLDOUT_PROFILE)
    assert len(schedule(old, tasks)) == 180 and old["limits"]["timeout_seconds"] == 300
    assert "hard_request_deadline_seconds" not in old
    from tracefix.comparison_holdout import balanced_schedule

    with pytest.raises(ValueError, match="unsupported"):
        balanced_schedule(tasks, "AC")
    protocol = {
        "profile": selected,
        "mode": "offline",
        "tasks": {t: {"repository": f"repo{i % 4}"} for i, t in enumerate(tasks)},
        "schedule": rows,
    }
    summary = summarize(protocol, [], [])
    assert set(summary["arms"]) == {"B", "C"}
    assert set(summary["comparisons"]) == {"C-B"} and summary["planned"] == 120
    from tracefix.comparison_holdout import aggregate

    interrupted = aggregate(
        [
            {
                "id": "stopped",
                "finished": False,
                "status": "completed",
                "passed": True,
                "seconds": 10,
            }
        ],
        [],
    )
    assert interrupted["successes"] == 0 and not interrupted["time_complete"]


def test_only_new_live_profile_wraps_real_client(tmp_path):
    llm = ComparisonBudget(
        provider_config(),
        root=tmp_path,
        trial={"id": "001", "arm": "B"},
        protocol_sha="fixture",
        profile=profile_config(BC_HOLDOUT_PROFILE),
    )
    assert isinstance(llm.client, HardDeadlineClient) and llm.client.seconds == 180
    with pytest.raises(ValueError, match="positive"):
        HardDeadlineClient(0)
    identity = transport_identity()
    assert identity["interpreter_sha256"] and identity["pythonpath"]


@pytest.fixture
def local_provider():
    state = {"calls": 0, "keepalives": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            state["calls"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                if self.path == "/keepalive":
                    for _ in range(400):
                        self.wfile.write(b"\n")
                        self.wfile.flush()
                        state["keepalives"] += 1
                        time.sleep(0.01)
                self.wfile.write(
                    json.dumps(
                        {
                            "id": "offline",
                            "object": "chat.completion",
                            "created": 1,
                            "model": "gpt-4o",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": "offline success"},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 5,
                                "completion_tokens": 2,
                                "total_tokens": 7,
                            },
                        }
                    ).encode()
                )
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_real_worker_returns_known_response_without_supplier(local_provider):
    url, state = local_provider
    result = HardDeadlineClient(30).completion(
        model="openai/gpt-4o",
        api_base=url,
        api_key="offline-key",
        messages=[{"role": "user", "content": "test"}],
        max_tokens=8,
        timeout=3,
        num_retries=0,
    )
    assert result["choices"][0]["message"]["content"] == "offline success"
    assert result["usage"]["prompt_tokens"] == 5 and state["calls"] == 1


def test_keepalives_cannot_extend_deadline_and_worker_is_dead(
    tmp_path, monkeypatch, local_provider
):
    import tracefix.comparison_transport as transport

    url, state = local_provider
    worker = tmp_path / "keepalive_worker.py"
    worker.write_text(
        "import json, sys, urllib.request\n"
        "body=json.load(sys.stdin)\n"
        "req=urllib.request.Request(body['url'], data=b'{}')\n"
        "with urllib.request.urlopen(req, timeout=0.1) as response:\n"
        "    print(response.read().decode())\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(transport, "worker_command", lambda: [sys._base_executable, str(worker)])
    processes = []
    original = subprocess.Popen

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(transport.subprocess, "Popen", capture)
    started = time.monotonic()
    with pytest.raises(LLMProviderError, match="hard request deadline"):
        HardDeadlineClient(0.75).completion(url=url + "/keepalive")
    assert time.monotonic() - started < 2
    assert state["calls"] == 1 and state["keepalives"] > 2
    assert processes[0].poll() is not None


def test_hard_timeout_retains_pending_fee_and_cannot_replay(tmp_path, monkeypatch):
    import tracefix.comparison_transport as transport
    from tracefix.comparison_profiles import OfficialCounter
    from tracefix.models.input_bounds import InputBound

    monkeypatch.setattr(
        OfficialCounter,
        "count",
        lambda *_: InputBound(100, "estimate", "offline", "identity", "sha"),
    )
    events = []

    class HungWorker:
        returncode = None

        def communicate(self, *args, **kwargs):
            if "timeout" in kwargs:
                events.append("sent")
                raise subprocess.TimeoutExpired("offline-worker", kwargs["timeout"])
            return "", ""

        def kill(self):
            events.append("killed")
            self.returncode = -1

    monkeypatch.setattr(transport.subprocess, "Popen", lambda *a, **kw: HungWorker())
    llm = ComparisonBudget(
        provider_config(),
        root=tmp_path,
        trial={"id": "001", "arm": "B"},
        protocol_sha="offline",
        profile=profile_config(BC_HOLDOUT_PROFILE),
        counter_runtime={"command": []},
    )
    with pytest.raises(LLMProviderError, match="deadline"):
        llm.complete(messages())
    ledger = read_json(tmp_path / "requests.json")
    assert ledger["requests"][0]["status"] == "pending"
    assert ledger["requests"][0]["reserved_peak_cny"] > 0
    assert "usage" not in ledger["requests"][0]
    with pytest.raises(LLMProviderError, match="unknown prior"):
        llm.complete(messages())
    assert events == ["sent", "killed"]


@pytest.mark.parametrize(
    "code,output,message",
    [
        (2, "", "exited"),
        (0, "invalid", "invalid"),
        (0, '{"ok":false,"error_type":"OfflineError"}', "OfflineError"),
        (0, '{"ok":true}', "invalid"),
    ],
)
def test_worker_errors_are_unknown_and_not_retried(monkeypatch, code, output, message):
    import tracefix.comparison_transport as transport

    calls = []

    def spawn(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=code, communicate=lambda *a, **kw: (output, "secret"))

    monkeypatch.setattr(transport.subprocess, "Popen", spawn)
    with pytest.raises(LLMProviderError, match=message) as exc:
        HardDeadlineClient(1).completion()
    assert "secret" not in str(exc.value) and len(calls) == 1


@pytest.mark.parametrize("failure", [False, True])
def test_worker_entrypoint_preserves_response_or_only_error_type(monkeypatch, capsys, failure):
    def complete(**kwargs):
        if failure:
            raise RuntimeError("never leak this secret")
        assert kwargs == {"num_retries": 0}
        return SimpleNamespace(model_dump=lambda **kw: {"content": "known"})

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=complete))
    monkeypatch.setattr(sys, "argv", ["worker", "--worker"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"num_retries":0}'))
    assert main() == 0
    result = capsys.readouterr().out
    assert "secret" not in result and json.loads(result)["ok"] is not failure
    monkeypatch.setattr(sys, "argv", ["worker"])
    with pytest.raises(SystemExit, match="internal"):
        main()


def test_authorization_carryover_rejects_without_call_and_detects_tampering(tmp_path, monkeypatch):
    from tracefix.comparison_profiles import OfficialCounter
    from tracefix.models.input_bounds import InputBound

    monkeypatch.setattr(
        OfficialCounter,
        "count",
        lambda *_: InputBound(100, "estimate", "offline", "identity", "sha"),
    )
    parent = tmp_path / "parent"
    parent.mkdir()
    protocol = {"profile": profile_config(HOLDOUT_PROFILE), "mode": "offline"}
    write_json(parent / "protocol.json", protocol)
    write_json(parent / "protocol.sha256.json", {"sha256": digest(protocol)})
    llm = ComparisonBudget(
        provider_config(),
        root=parent,
        trial={"id": "001", "arm": "B"},
        protocol_sha=digest(protocol),
        profile=profile_config(HOLDOUT_PROFILE),
        counter_runtime={"command": []},
        client=FixtureClient(PATCH),
    )
    llm.complete(messages())
    funding = freeze_parent_budget(parent)
    assert prior_spend({"funding": funding}) > 0 and prior_spend({}) == 0
    linked = tmp_path / "linked"
    linked_protocol = {
        "profile": profile_config(BC_HOLDOUT_PROFILE),
        "funding": funding,
        "mode": "offline",
    }
    write_json(linked / "protocol.json", linked_protocol)
    write_json(linked / "protocol.sha256.json", {"sha256": digest(linked_protocol)})
    linked_llm = ComparisonBudget(
        provider_config(),
        root=linked,
        trial={"id": "001", "arm": "B"},
        protocol_sha=digest(linked_protocol),
        profile=profile_config(BC_HOLDOUT_PROFILE),
        counter_runtime={"command": []},
        client=FixtureClient(PATCH),
    )
    linked_llm.complete(messages())
    chained = freeze_parent_budget(linked)
    assert chained["prior_conservative_cny"] == pytest.approx(2 * funding["prior_conservative_cny"])
    child = tmp_path / "child"
    write_json(
        child / "protocol.json",
        {"funding": {**funding, "prior_conservative_cny": 299.999}, "mode": "offline"},
    )
    client = FixtureClient(PATCH)
    current = ComparisonBudget(
        provider_config(),
        root=child,
        trial={"id": "001", "arm": "B"},
        protocol_sha="child",
        profile=profile_config(BC_HOLDOUT_PROFILE),
        counter_runtime={"command": []},
        client=client,
    )
    with pytest.raises(PreRequestBudgetExceeded):
        current.complete(messages())
    assert client.calls == 0 and not read_json(child / "trials/001/refusal.json")["sent"]
    with pytest.raises(ValueError, match="identity"):
        prior_spend({"funding": {**funding, "authorization_cny": 301}})
    with pytest.raises(ValueError, match="protocol changed"):
        prior_spend({"funding": {**funding, "parent_protocol_sha256": "wrong"}})
    ledger = read_json(parent / "requests.json")
    ledger["requests"][0]["status"] = "pending"
    write_json(parent / "requests.json", ledger)
    with pytest.raises(ValueError, match="changed"):
        prior_spend({"funding": funding})
    with pytest.raises(ValueError, match="unknown"):
        freeze_parent_budget(parent)
    write_json(parent / "protocol.sha256.json", {"sha256": "corrupt"})
    with pytest.raises(ValueError, match="corrupted"):
        freeze_parent_budget(parent)


def test_bc_block_requires_twenty_remaining_authorized_cny(tmp_path, monkeypatch):
    import tracefix.comparison_campaign as campaign
    import tracefix.comparison_funding as funding

    selected = profile_config(BC_HOLDOUT_PROFILE)
    monkeypatch.setattr(
        campaign,
        "check_protocol",
        lambda *_: {
            "profile": selected,
            "mode": "offline",
            "schedule": [
                {"id": "001", "task_id": "fixture", "arm": "B", "repetition": 1, "block": 1},
                {"id": "002", "task_id": "fixture", "arm": "C", "repetition": 1, "block": 1},
            ],
        },
    )
    monkeypatch.setattr(funding, "prior_spend", lambda *_: 281)
    assert campaign.run(tmp_path) == []
    assert read_json(tmp_path / "budget-stop.json")["remaining_cny"] == 19
    assert not (tmp_path / "requests.json").exists()


def test_transport_identity_and_missing_funding_block_before_calls(tmp_path):
    import tracefix.comparison_campaign as campaign

    protocol = {
        "profile": profile_config(BC_HOLDOUT_PROFILE),
        "mode": "offline",
        "provider_transport": {"corrupt": True},
    }
    write_json(tmp_path / "protocol.json", protocol)
    write_json(tmp_path / "protocol.sha256.json", {"sha256": digest(protocol)})
    with pytest.raises(ValueError, match="transport identity"):
        campaign.check_protocol(tmp_path)
    protocol.update(mode="live", provider_transport=transport_identity())
    write_json(tmp_path / "protocol.json", protocol)
    write_json(tmp_path / "protocol.sha256.json", {"sha256": digest(protocol)})
    with pytest.raises(ValueError, match="funding missing"):
        campaign.check_protocol(tmp_path)
    with pytest.raises(ValueError, match="prior authorization"):
        campaign.prepare(
            tmp_path / "catalog", tmp_path / "new", tmp_path / "prices", profile=BC_HOLDOUT_PROFILE
        )


def test_parent_authorization_cannot_be_replaced_with_an_unrelated_budget(tmp_path, monkeypatch):
    import tracefix.comparison_campaign as campaign

    protocol = {"profile": profile_config(), "mode": "offline"}
    write_json(tmp_path / "protocol.json", protocol)
    write_json(tmp_path / "protocol.sha256.json", {"sha256": digest(protocol)})
    with pytest.raises(ValueError, match="same 300"):
        freeze_parent_budget(tmp_path)
    protocol["profile"] = profile_config(HOLDOUT_PROFILE)
    write_json(tmp_path / "protocol.json", protocol)
    write_json(tmp_path / "protocol.sha256.json", {"sha256": digest(protocol)})
    monkeypatch.setattr(
        campaign, "validate_requests", lambda *_: [{"status": "completed", "peak_cost_cny": 301}]
    )
    with pytest.raises(ValueError, match="invalid prior"):
        freeze_parent_budget(tmp_path)
