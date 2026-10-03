"""Procedural memory publication and real, cross-process repository replay."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from examples.replay_ordinary import ReplayClient, _git
from tracefix.checkpoint import CheckpointError, ProcessLock
from tracefix.memory import (
    ExperienceProposal,
    ExperienceStore,
    MemoryError,
    atomic_json,
    digest,
    validate_proposal,
)
from tracefix.messages import Message, MessageRole
from tracefix.models.base import BaseLLM, LLMResponse, TokenUsage
from tracefix.models.litellm_adapter import LiteLLMAdapter
from tracefix.runtime import RunConfig, TraceFixRunner, config_identity_sha256


def proposal(**updates):
    values = {
        "key": "pagination", "summary": "Validate pagination changes against the current tests",
        "keywords": ["pagination", "分页"], "applicability_paths": ["widget.py"],
        "steps": [{"instruction": "Read the current implementation before editing.",
                   "call_id": "read"}],
        "test_call_id": "test", "generalized": False,
    }
    return ExperienceProposal.model_validate({**values, **updates})


def evidence():
    return {
        "source_sha256": "source", "latest_test": {
            "valid": True, "call_id": "test", "source_sha256": "source",
        },
        "tool_results": {
            "read": {"tool_name": "read_file", "success": True, "output": {"path": "widget.py"}},
            "test": {"tool_name": "run_tests", "success": True, "output": {
                "source_sha256_before": "source", "source_sha256_after": "source",
            }},
        },
    }


@pytest.fixture
def store(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "widget.py").write_text("VALUE = 1\n", encoding="utf-8")
    return ExperienceStore(tmp_path / "memory", repo)


def test_atomic_versions_duplicates_lineage_disable_and_rollback(store):
    first = store.publish("first", proposal(), evidence(), [])
    assert first["status"] == "verified"
    assert store.publish("first", proposal(summary="ignored"), evidence(), []) == first
    assert store.publish("same", proposal(), evidence(), [])["status"] == "duplicate"
    previous = store.show("pagination")["versions"][0]["sha256"]
    changed = proposal(summary="Read and test pagination before finishing", supersedes=previous)
    assert store.publish("second", changed, evidence(), [])["version"] == 2
    store.rollback("pagination", 1)
    assert store.show("pagination")["active"] == 1
    store.disable("pagination")
    assert not store.select("pagination")
    store.rollback("pagination", 2)
    assert store.select("pagination")[0][1] == 2
    conflict = store.publish("conflict", proposal(summary="Different unlinked guidance"),
                             evidence(), [])
    assert conflict["status"] == "candidate"
    assert not store.select("pagination")
    with pytest.raises(MemoryError, match="unverified"):
        store.rollback("pagination", 3)
    with pytest.raises(MemoryError, match="unknown"):
        store.rollback("pagination", 99)
    with pytest.raises(MemoryError, match="unknown"):
        store.disable("missing")
    with pytest.raises(MemoryError, match="unknown"):
        store.show("missing")


def test_candidate_never_selected_and_repository_namespace(store, tmp_path):
    store.publish("candidate", proposal(), evidence(), ["not verified"])
    assert store.select("pagination") == []
    other = tmp_path / "other"
    other.mkdir()
    assert ExperienceStore(store.root, other).list() == {}
    with pytest.raises(MemoryError, match="outside"):
        ExperienceStore(store.repo / "memory", store.repo)


def test_candidate_can_gain_verified_evidence_without_overwriting_history(store):
    store.publish("uncertain", proposal(), evidence(), ["unverified"])
    assert store.publish("qualified", proposal(), evidence(), [])["version"] == 2
    record = store.show("pagination")
    assert record["active"] == 2
    assert record["versions"][0]["status"] == "candidate"
    assert store.publish("repeated", proposal(), evidence(), [])["status"] == "duplicate"


def test_automatic_revision_cannot_override_explicit_disable(store):
    store.publish("first", proposal(), evidence(), [])
    previous = store.show("pagination")["versions"][0]["sha256"]
    store.disable("pagination")
    update = store.publish("update", proposal(summary="Revised description", supersedes=previous),
                           evidence(), [])
    assert update["status"] == "candidate"
    assert not store.select("pagination")
    assert store.show("pagination")["disabled"] is True
    store.rollback("pagination", 1)
    assert store.select("pagination")


@pytest.mark.parametrize("mutation", [
    "schema", "repo", "shape", "key", "versions", "active", "status", "content", "evidence",
    "unverified_active",
])
def test_checksummed_invalid_store_is_rejected(store, mutation):
    store.publish("first", proposal(), evidence(), [])
    document = json.loads(store.path.read_text(encoding="utf-8"))
    data = document["data"]
    record = data["records"]["pagination"]
    if mutation == "schema":
        document["schema_version"] = 99
    elif mutation == "repo":
        data["repo_id"] = "other"
    elif mutation == "shape":
        data["records"] = []
    elif mutation == "key":
        data["records"]["../escape"] = data["records"].pop("pagination")
    elif mutation == "versions":
        record["versions"] = []
    elif mutation == "active":
        record["active"] = 20
    elif mutation == "status":
        record["versions"][0]["status"] = "trusted"
    elif mutation == "content":
        record["versions"][0]["sha256"] = "wrong"
    elif mutation == "evidence":
        record["versions"][0]["evidence_sha256"] = "wrong"
    else:
        record["versions"][0]["status"] = "candidate"
    document["sha256"] = digest(data)
    atomic_json(store.path, document)
    with pytest.raises(MemoryError, match="verify"):
        store.list()


def test_deterministic_max_three_and_observed_steps(store):
    for key in ["delta", "beta", "alpha", "gamma"]:
        store.publish(key, proposal(key=key), evidence(), [])
    assert [item[0] for item in store.select("pagination")] == ["alpha", "beta", "delta"]
    assert not store.select("unrelated")
    facts = evidence()
    facts["call_order"] = ["read", "test"]
    reversed_steps = proposal(steps=[
        {"instruction": "Run the related tests after changing the implementation.",
         "call_id": "test"},
        {"instruction": "Read the current implementation before editing.", "call_id": "read"},
    ])
    assert "experience steps do not follow the observed order" in validate_proposal(
        reversed_steps, facts, store.repo)


@pytest.mark.parametrize("path", ["C:\\outside", "a/../b", "..\\x", ".git/config", "a\nfile"])
def test_unsafe_paths_never_become_active(store, path):
    assert validate_proposal(proposal(applicability_paths=[path]), evidence(), store.repo)


def test_independent_process_load_and_skill_snapshot_is_immutable(store, tmp_path):
    store.publish("first", proposal(), evidence(), [])
    env = __import__("os").environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    code = ("from pathlib import Path; from tracefix.memory import ExperienceStore; "
            "import sys,json; print(json.dumps(ExperienceStore(Path(sys.argv[1]),"
            "Path(sys.argv[2])).select('pagination')))")
    read = subprocess.run([sys.executable, "-c", code, str(store.root), str(store.repo)],
                          capture_output=True, text=True, check=True, env=env)
    assert json.loads(read.stdout)[0][0] == "pagination"
    snapshot = tmp_path / "skills"
    assert store.snapshot("pagination", snapshot)[0]["version"] == 1
    original = (snapshot / "experience-pagination" / "SKILL.md").read_bytes()
    store.disable("pagination")
    assert (snapshot / "experience-pagination" / "SKILL.md").read_bytes() == original
    (store.repo / "widget.py").unlink()
    store.rollback("pagination", 1)
    assert store.select("pagination") == []
    assert store.show("pagination")["active"] is None


def test_store_corruption_concurrency_and_failed_publication(store, monkeypatch):
    store.publish("first", proposal(), evidence(), [])
    original = store.path.read_bytes()
    with ProcessLock(store.directory), pytest.raises(CheckpointError, match="lock"):
        store.disable("pagination")
    import tracefix.memory as memory

    def fail(*args):
        raise OSError("injected replacement failure")

    with monkeypatch.context() as context:
        context.setattr(memory.os, "replace", fail)
        with pytest.raises(OSError, match="injected"):
            store.disable("pagination")
    assert store.path.read_bytes() == original
    assert not list(store.directory.glob(".memory-*"))
    store.path.write_text("{broken", encoding="utf-8")
    with pytest.raises(MemoryError, match="verify"):
        store.list()
    store.path.write_bytes(original)
    data = json.loads(original)
    data["data"]["records"]["pagination"]["versions"][0]["content"]["summary"] = "changed"
    store.path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(MemoryError, match="verify"):
        store.list()


def test_capacity_rejection_preserves_readable_previous_versions(store, monkeypatch):
    import tracefix.memory as memory

    store.publish("first", proposal(), evidence(), [])
    original = store.path.read_bytes()
    previous = store.show("pagination")["versions"][0]["sha256"]
    monkeypatch.setattr(memory, "_MAX_INDEX_BYTES", len(original))
    with pytest.raises(MemoryError, match="capacity"):
        store.publish("larger", proposal(summary="Additional guidance", supersedes=previous),
                      evidence(), [])
    assert store.path.read_bytes() == original
    assert store.select("pagination")[0][1] == 1


@pytest.mark.parametrize("update", [
    {"generalized": True}, {"test_call_id": "missing"},
    {"applicability_paths": ["../outside"]}, {"applicability_paths": ["missing.py"]},
    {"steps": [{"instruction": "unsupported", "call_id": "missing"}]},
])
def test_proposal_rejects_unverified_generalization_paths_and_calls(store, update):
    assert validate_proposal(proposal(**update), evidence(), store.repo)


def source_repo(root):
    root.mkdir()
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (root / "widget.py").write_text("def next_page(page):\n    return page\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests/test_widget.py").write_text(
        "from widget import next_page\n\ndef test_next_page():\n    assert next_page(1) == 2\n",
        encoding="utf-8",
    )
    _git(root, "init", "-q")
    _git(root, "add", "--all")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "fixture")


class MemoryClient(ReplayClient):
    def __init__(self, *, recall=False, extraction="valid"):
        super().__init__()
        self.recalled = False
        self.extraction = extraction
        if recall:
            self.sequence.insert(0, ("load_skill", {"name": "experience-pagination"}))

    def completion(self, **kwargs):
        if not kwargs.get("tools"):
            self.calls += 1
            if self.extraction == "unknown":
                raise TimeoutError("injected unknown extraction outcome")
            data = json.loads(kwargs["messages"][-1]["content"])
            results = data["evidence"]["tool_results"]
            read_id = next(key for key, item in results.items() if item["tool_name"] == "read_file")
            test_id = data["evidence"]["latest_test"]["call_id"]
            learned = proposal(steps=[{
                "instruction": "Read the current implementation before editing.",
                "call_id": read_id,
            }], test_call_id=test_id)
            content = "not json" if self.extraction == "invalid" else learned.model_dump_json()
            return {"model": "offline/replay", "usage": {
                "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
            }, "choices": [{"message": {"role": "assistant", "content": content},
                            "finish_reason": "stop"}]}
        if any("<tracefix_skill_instructions>" in str(item) and "Read the current implementation" in
               str(item) for item in kwargs["messages"]):
            self.recalled = True
        return super().completion(**kwargs)


def run_memory(root, client, **updates):
    repo = root / "source"
    if not repo.exists():
        source_repo(repo)
    runner = TraceFixRunner(llm_factory=lambda config: LiteLLMAdapter(config, client=client))
    config = RunConfig(repo=repo, task="Fix pagination", model_name="offline/replay",
                       output_dir=root / "runs", env_file=None, memory_enabled=True,
                       test_python_executable=Path(sys.executable),
                       test_target="tests/test_widget.py", source_import="widget")
    for key, value in updates.items():
        setattr(config.agent_config, key, value)
    return runner.run(config)


def test_real_repository_extract_recall_tests_and_accounting(tmp_path):
    first_client = MemoryClient()
    first = run_memory(tmp_path, first_client)
    assert first.status == "completed", first.error
    assert first.memory_status["status"] == "verified", first.memory_status
    assert first.step_count == first_client.calls == 7
    assert first.input_tokens == 700
    assert first.output_tokens == 70
    memory = ExperienceStore(tmp_path / "runs/memory", tmp_path / "source")
    assert memory.select("pagination")
    second_client = MemoryClient(recall=True)
    second = run_memory(tmp_path, second_client)
    assert second.status == "completed", second.error
    assert second.agent_validation_status == "verified"
    assert second_client.recalled
    assert "return page + 1" in Path(second.diff_path).read_text(encoding="utf-8")
    from tracefix.onboarding import verify_patch

    assert verify_patch(Path(second.result_path).parent)["passed"] is True
    assert (tmp_path / "source/widget.py").read_text().endswith("return page\n")
    assert json.loads((Path(second.result_path).parent / "memory-selection.json").read_text())[0][
        "key"
    ] == "pagination"


@pytest.mark.parametrize("kind,status", [("invalid", "rejected"), ("unknown", "outcome_unknown")])
def test_extraction_failure_preserves_repair_and_unknown_accounting(tmp_path, kind, status):
    client = MemoryClient(extraction=kind)
    result = run_memory(tmp_path, client)
    assert result.status == "completed"
    assert result.memory_status["status"] == status
    if kind == "unknown":
        assert not result.cost_complete and not result.usage_complete
    assert not ExperienceStore(tmp_path / "runs/memory", tmp_path / "source").list()


def test_extraction_budget_skip_and_legacy_config_identity(tmp_path):
    result = run_memory(tmp_path, MemoryClient(), max_steps=6)
    assert result.status == "completed"
    assert result.step_count == 6
    assert result.memory_status["status"] == "skipped"
    config = RunConfig(repo=tmp_path, task="task")
    old = config.model_dump(mode="json", exclude={"memory_enabled", "memory_dir"})
    import hashlib

    old_hash = hashlib.sha256(json.dumps(old, separators=(",", ":"), ensure_ascii=False,
                                       default=str).encode()).hexdigest()
    assert config_identity_sha256(config, old) == old_hash


def test_memory_cli_and_toml_paths(store, tmp_path, capsys):
    from tracefix.cli import _ordinary_settings, build_parser, main

    store.publish("first", proposal(), evidence(), [])
    common = ["--repo", str(store.repo), "--memory-dir", str(store.root)]
    assert main(["memory", "list", *common]) == 0
    assert "pagination" in capsys.readouterr().out
    assert main(["memory", "show", *common, "--key", "pagination"]) == 0
    assert main(["memory", "disable", *common, "--key", "pagination"]) == 0
    assert main(["memory", "rollback", *common, "--key", "pagination", "--version", "1"]) == 0
    assert main(["memory", "show", *common]) != 0
    assert main(["memory", "rollback", *common, "--key", "pagination"]) != 0
    config = tmp_path / "config.toml"
    config.write_text('[run]\nrepo="repo"\nmemory=true\nmemory_dir="memory"\n', encoding="utf-8")
    args = build_parser().parse_args(["run", "--config", str(config)])
    settings = _ordinary_settings(args)
    assert settings["memory_enabled"] is True
    assert settings["memory_dir"] == tmp_path / "memory"


def test_no_verified_patch_skips_without_extraction(tmp_path):
    source_repo(tmp_path / "source")

    class FinalOnly(BaseLLM):
        def complete(self, messages, tools=()):
            return LLMResponse(message=Message(role=MessageRole.ASSISTANT, content="No change"),
                               usage=TokenUsage(cost_usd=0), model_name="offline")

    result = TraceFixRunner(llm_factory=FinalOnly).run(RunConfig(
        repo=tmp_path / "source", task="pagination", output_dir=tmp_path / "runs",
        model_name="offline", env_file=None, memory_enabled=True,
    ))
    assert result.step_count == 1
    assert result.memory_status["status"] == "skipped"


def test_atomic_json_rejects_nonfinite_without_losing_previous(tmp_path):
    path = tmp_path / "record.json"
    atomic_json(path, {"old": True})
    original = path.read_bytes()
    with pytest.raises(ValueError):
        atomic_json(path, {"bad": float("nan")})
    assert path.read_bytes() == original
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_installed_package_shape_replay_separate_processes(tmp_path):
    from tracefix.memory_replay import run_memory_replay

    result = run_memory_replay(tmp_path / "replay")
    assert result["accepted"] is True
    assert result["provider_calls"] == 0
    assert result["learn"]["pid"] != result["recall"]["pid"]
    assert result["recall"]["recalled"] is True


def test_recorded_worker_uses_production_adapter_and_independent_verifier(tmp_path):
    from tracefix.memory_replay import worker

    output = tmp_path / "worker"
    output.mkdir()
    first = worker(output, "learn")
    second = worker(output, "recall")
    assert first["recorded_requests"] == 7
    assert second["recorded_requests"] == 8
    assert first["independent_passed"] and second["independent_passed"]
    assert second["recalled"]
    assert first["memory"]["status"] == "verified"
    assert second["memory"]["status"] == "duplicate"


@pytest.mark.parametrize("mode,expected", [
    ("input", "skipped"), ("output", "skipped"), ("preflight", "skipped"),
    ("tools", "rejected"), ("excess", "rejected"), ("time", "rejected"),
    ("format", "outcome_unknown"), ("interrupt", "outcome_unknown"),
])
def test_extraction_faults_budget_and_durable_no_retry(store, tmp_path, mode, expected):
    from types import SimpleNamespace

    from tracefix.exceptions import (
        LLMResponseFormatError,
        PreRequestBudgetExceeded,
        TimeLimitExceeded,
    )
    from tracefix.memory import reflect_experience
    from tracefix.messages import ToolCall

    run = tmp_path / "run"
    run.mkdir()
    facts = evidence()
    (run / "trajectory.jsonl").write_text("".join(json.dumps({
        "event_type": "tool_returned", "payload": {"result": {"call_id": key, **value}},
    }) + "\n" for key, value in facts["tool_results"].items()), encoding="utf-8")

    class Agent:
        def __init__(self):
            self.calls = 0
            self.state = SimpleNamespace(validation_status="verified", task="pagination",
                                         step_count=0, input_tokens=0, output_tokens=0,
                                         model_request_seconds=0, cost_complete=True,
                                         usage_complete=True)
            self.config = SimpleNamespace(max_input_tokens=10000, max_output_tokens=10000)
            self.context_manager = SimpleNamespace(estimate_tokens=lambda messages:
                                                   20000 if mode == "input" else 10)
            self.llm = SimpleNamespace(config=SimpleNamespace(
                max_output_tokens=20000 if mode == "output" else 100), complete=self.complete)
            self.history = SimpleNamespace(snapshot=lambda: ())
            self._last_test_evidence = facts["latest_test"]

        def complete(self, messages):
            self.calls += 1
            if mode == "preflight":
                raise PreRequestBudgetExceeded("no cost budget")
            if mode == "format":
                raise LLMResponseFormatError("invalid", context={"usage": {"input_tokens": 12}})
            if mode == "interrupt":
                raise KeyboardInterrupt
            return LLMResponse(message=Message(role=MessageRole.ASSISTANT,
                content=proposal().model_dump_json(), tool_calls=(
                    (ToolCall(id="bad", name="read_file", arguments={}),) if mode == "tools" else ()
                )), usage=TokenUsage(cost_usd=0), model_name="offline")

        def verify_test_source(self, source):
            pass

        def _check_pre_request_budgets(self):
            pass

        def _emit(self, *args):
            pass

        def _emit_model_response(self, *args):
            pass

        def _add_usage(self, usage):
            pass

        def _add_usage_dict(self, usage):
            self.state.input_tokens += usage["input_tokens"]

        def _check_time_budget(self):
            if mode == "time":
                raise TimeLimitExceeded("time budget")

        def _post_response_budget_error(self):
            return "exceeded" if mode == "excess" else None

    agent = Agent()
    if mode == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            reflect_experience(agent, store, run, store.repo, "source")
        assert not agent.state.cost_complete and not agent.state.usage_complete
    else:
        receipt = reflect_experience(agent, store, run, store.repo, "source")
        assert receipt["status"] == expected
    previous_calls = agent.calls
    assert reflect_experience(agent, store, run, store.repo, "source")["status"] == expected
    assert agent.calls == previous_calls
    assert store.list() == {}
