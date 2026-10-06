"""Failure injection around campaign identity and post-model persistence."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_comparison import PATCH, fixture_catalog

from tracefix import TraceFixRunner
from tracefix.checkpoint import CheckpointError, ProcessLock
from tracefix.comparison import TASK_IDS, read_json, schedule, simple_loop, write_json
from tracefix.comparison_campaign import (
    check_protocol,
    finalize_trial,
    main,
    prepare,
    qualify_task,
    run,
    validate_record,
    verify,
)
from tracefix.messages import Message, MessageRole, ToolCall
from tracefix.models.base import LLMResponse, TokenUsage
from tracefix.tools.base import ToolResult


@pytest.mark.parametrize("budget_stop", [False, True])
def test_simple_trial_excludes_real_pytest_products_from_patch(tmp_path, monkeypatch, budget_stop):
    import tracefix.comparison_campaign as campaign
    from tracefix.exceptions import PreRequestBudgetExceeded

    catalog, _ = fixture_catalog(tmp_path)
    spec = read_json(catalog)[0]
    source = tmp_path / "source"
    with (source / "test_sample.py").open("a", encoding="utf-8") as stream:
        stream.write(
            "def test_binary_product(tmp_path):\n"
            "    (tmp_path / 'generated.bin').write_bytes(bytes(range(256)))\n"
        )
    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=f@example.invalid",
         "commit", "-m", "real temporary products"],
        cwd=source, check=True, capture_output=True,
    )
    spec = qualify_task(spec, tmp_path / "qualification")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Fix add", encoding="utf-8")
    spec["prompt_path"] = str(prompt)
    protocol = {"mode": "offline", "tasks": {TASK_IDS[0]: spec}}
    original = campaign.simple_loop

    def stop_after_tools(*args, **kwargs):
        result = original(*args, **kwargs)
        if budget_stop:
            raise PreRequestBudgetExceeded("offline fee refusal")
        return result

    monkeypatch.setattr(campaign, "simple_loop", stop_after_tools)
    root = tmp_path / "campaign"
    row = schedule()[1]
    result = campaign.execute_trial(root, protocol, row, None)
    directory = root / "trials" / row["id"]
    assert list((directory / "workspace" / ".tracefix-test-tmp").rglob("generated.bin"))
    assert ".tracefix-test-tmp" not in (directory / "patch.diff").read_text(encoding="utf-8")
    assert result["passed"]
    assert result["status"] == ("budget_exhausted" if budget_stop else "completed")


def test_cli_dispatch_preserves_explicit_campaign_limits(tmp_path, monkeypatch, capsys):
    import tracefix.comparison_campaign as campaign

    calls = []
    monkeypatch.setattr(campaign, "prepare", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(campaign, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(campaign, "report", lambda root: {"complete": False})
    root = tmp_path / "campaign"
    assert (
        main(
            [
                "prepare",
                "--campaign-dir",
                str(root),
                "--catalog",
                "catalog.json",
                "--prices",
                "prices.json",
                "--mode",
                "offline",
            ]
        )
        == 0
    )
    assert calls[-1][1] == {"mode": "offline"}
    assert main(["run", "--campaign-dir", str(root), "--max-trials", "3"]) == 0
    assert calls[-1][1] == {"max_trials": 3}
    assert main(["report", "--campaign-dir", str(root)]) == 0
    assert '"complete": false' in capsys.readouterr().out


def test_unapproved_provider_configuration_rejected_before_transport(tmp_path):
    from test_comparison import provider_config

    from tracefix.comparison import ComparisonBudget

    config = provider_config()
    config.max_retries = 1
    with pytest.raises(ValueError, match="no retries"):
        ComparisonBudget(
            config, root=tmp_path, trial={"id": "001", "arm": "A"}, protocol_sha="test"
        )
    assert not (tmp_path / "requests.json").exists()


def test_bare_model_illegal_patch_is_model_failure_not_infrastructure(tmp_path):
    import tracefix.comparison_campaign as campaign
    from tracefix.comparison import digest

    catalog, _ = fixture_catalog(tmp_path)
    spec = read_json(catalog)[0]
    spec["fixture_patch"] = PATCH.replace("sample.py", "../outside.py")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Fix add", encoding="utf-8")
    spec["prompt_path"] = str(prompt)
    protocol = {"mode": "offline", "tasks": {TASK_IDS[0]: spec}}
    root = tmp_path / "campaign"
    result = campaign.execute_trial(root, protocol, schedule()[0], None)
    assert result["status"] == "invalid_patch"
    assert result["passed"] is False
    assert not result.get("infrastructure_failure")
    assert read_json(root / "requests.json")["protocol_sha256"] == digest(protocol)


def test_malformed_response_preserves_raw_evidence_and_blocks_replay(tmp_path):
    from test_comparison import make_budget, messages

    from tracefix.comparison_campaign import FixtureClient
    from tracefix.exceptions import LLMProviderError, LLMResponseFormatError

    class MalformedClient(FixtureClient):
        def completion(self, **kwargs):
            response = super().completion(**kwargs)
            response["choices"][0]["message"]["tool_calls"] = [
                {"id": "broken", "function": {"name": "read_file", "arguments": "{"}}
            ]
            return response

    client = MalformedClient(PATCH)
    budget = make_budget(tmp_path, client=client)
    with pytest.raises(LLMResponseFormatError):
        budget.complete(messages())
    error = read_json(tmp_path / "provider/001-01-response-error.json")
    assert error["context"]["raw_response"]["usage"]["prompt_tokens"] == 100
    assert read_json(tmp_path / "requests.json")["requests"][0]["status"] == "pending"
    with pytest.raises(LLMProviderError, match="no automatic replay"):
        budget.complete(messages())
    assert client.calls == 1


def test_catalog_modes_and_price_identity_fail_without_calls(tmp_path):
    catalog, prices = fixture_catalog(tmp_path)
    with pytest.raises(ValueError, match="mode"):
        prepare(catalog, tmp_path / "live", prices)
    write_json(catalog, [])
    with pytest.raises(ValueError, match="ten-task"):
        prepare(catalog, tmp_path / "empty", prices, mode="offline")
    with pytest.raises(SystemExit):
        main(["prepare", "--campaign-dir", str(tmp_path / "missing")])


def test_simple_loop_has_no_added_guidance_and_enforces_test_cap(tmp_path):
    class Client:
        count = 0

        def complete(self, history, _specs):
            self.count += 1
            if self.count == 1:
                return LLMResponse(
                    model_name="offline",
                    usage=TokenUsage(),
                    message=Message(
                        role=MessageRole.ASSISTANT,
                        tool_calls=tuple(
                            ToolCall(id=str(i), name="run_tests", arguments={"command": "pytest"})
                            for i in range(17)
                        ),
                    ),
                )
            return LLMResponse(
                model_name="offline",
                usage=TokenUsage(),
                message=Message(role=MessageRole.ASSISTANT, content="done"),
            )

    calls = []

    class Tools:
        def specs(self):
            return ()

        def get(self, name):
            return self

        def execute(self, call):
            calls.append(call.id)
            return ToolResult(call_id=call.id, tool_name=call.name, success=True, output="ok")

    trace = tmp_path / "trace.json"
    result = simple_loop(Client(), Tools(), "Fix it", trace)
    assert result["status"] == "completed" and len(calls) == 16
    history = read_json(trace)
    assert len([r for r in history if r["role"] == "system"]) == 1
    assert "test limit" in history[-2]["content"]


def test_simple_loop_tool_errors_are_plain_results(tmp_path):
    class Client:
        count = 0

        def complete(self, history, _specs):
            self.count += 1
            return LLMResponse(
                model_name="offline",
                message=Message(
                    role=MessageRole.ASSISTANT,
                    tool_calls=(ToolCall(id=str(self.count), name="unknown", arguments={}),),
                ),
            )

    class Tools:
        def specs(self):
            return ()

        def get(self, name):
            raise ValueError("unknown tool")

    result = simple_loop(Client(), Tools(), "Fix it", tmp_path / "trace.json")
    assert result["status"] == "budget_exhausted"
    assert len(read_json(tmp_path / "trace.json")) == 42


def fake_qualified(tmp_path, monkeypatch):
    import tracefix.comparison_campaign as campaign

    catalog, prices = fixture_catalog(tmp_path)
    specs = read_json(catalog)
    for spec in specs:
        spec.update(
            source_identity={"commit": "fake", "files": {"sample.py": "fake"}},
            environment_identity="fake",
            public_failure="public initial failure",
            qualification={"qualified": True},
        )
    monkeypatch.setattr(
        campaign,
        "qualify_task",
        lambda spec, root: next(s for s in specs if s["task_id"] == spec["task_id"]),
    )
    root = tmp_path / "campaign"
    protocol = prepare(catalog, root, prices, mode="offline")
    monkeypatch.setattr(
        campaign,
        "source_identity",
        lambda source: {"commit": "fake", "files": {"sample.py": "fake"}},
    )
    monkeypatch.setattr(
        campaign,
        "inspect_test_environment",
        lambda *a, **k: SimpleNamespace(fingerprint_sha256="fake"),
    )
    return root, protocol


@pytest.mark.parametrize(
    "failure", ["protocol", "implementation", "artifact", "environment", "source", "ci"]
)
def test_frozen_identity_refuses_before_transport(tmp_path, monkeypatch, failure):
    import tracefix.comparison_campaign as campaign

    root, protocol = fake_qualified(tmp_path, monkeypatch)
    if failure == "protocol":
        (root / "protocol.json").write_text("{}")
    elif failure == "implementation":
        monkeypatch.setattr(TraceFixRunner, "_implementation_sha256", lambda: "different")
    elif failure == "artifact":
        (root / "prices.json").write_text("{}")
    elif failure == "environment":
        monkeypatch.setattr(
            campaign,
            "inspect_test_environment",
            lambda *a, **k: SimpleNamespace(fingerprint_sha256="different"),
        )
    elif failure == "source":
        monkeypatch.setattr(campaign, "source_identity", lambda source: {})
    else:
        protocol["mode"] = "live"
        from tracefix.comparison import digest

        write_json(root / "protocol.json", protocol)
        write_json(root / "protocol.sha256.json", {"sha256": digest(protocol)})
        write_json(
            root / "ci-evidence.json", {"head_sha": "wrong", "conclusion": "success", "jobs": []}
        )
        original_git = campaign.git
        monkeypatch.setattr(
            campaign,
            "git",
            lambda source, *args: "" if args[0] == "status" else original_git(source, *args),
        )
    with pytest.raises((ValueError, KeyError)):
        check_protocol(root)
    assert not (root / "requests.json").exists()


def test_live_ci_is_exact_and_offline_protocol_cannot_be_overwritten(tmp_path, monkeypatch):
    import tracefix.comparison_campaign as campaign

    root, protocol = fake_qualified(tmp_path, monkeypatch)
    catalog = tmp_path / "catalog.json"
    with pytest.raises(ValueError, match="already"):
        prepare(catalog, root, tmp_path / "prices.json", mode="offline")
    protocol["mode"] = "live"
    from tracefix.comparison import digest

    write_json(root / "protocol.json", protocol)
    write_json(root / "protocol.sha256.json", {"sha256": digest(protocol)})
    write_json(
        root / "ci-evidence.json",
        {
            "head_sha": protocol["implementation_commit"],
            "conclusion": "success",
            "jobs": [{"conclusion": "success"}] * 5,
        },
    )
    original_git = campaign.git
    monkeypatch.setattr(
        campaign,
        "git",
        lambda source, *args: "" if args[0] == "status" else original_git(source, *args),
    )
    assert check_protocol(root)["mode"] == "live"


def test_concurrent_run_and_incomplete_trial_cannot_send(tmp_path, monkeypatch):
    import tracefix.comparison_campaign as campaign

    root, protocol = fake_qualified(tmp_path, monkeypatch)
    monkeypatch.setattr(campaign, "check_protocol", lambda root: protocol)
    with ProcessLock(root), pytest.raises(CheckpointError):
        run(root)
    write_json(root / "trials" / "001" / "record.json", {**schedule()[0], "finished": False})
    with pytest.raises(ValueError, match="incomplete"):
        run(root)
    assert not (root / "requests.json").exists()


def test_record_hash_and_escape_are_rejected(tmp_path):
    from tracefix.comparison import digest

    row = schedule()[0]
    record = {**row, "finished": True, "artifacts": {}}
    record["record_sha256"] = digest(record)
    validate_record(record, tmp_path, row)
    with pytest.raises(ValueError, match="identity"):
        validate_record(record, tmp_path, {**row, "arm": "C"})
    record["finished"] = False
    with pytest.raises(ValueError, match="corrupted"):
        validate_record(record, tmp_path, row)


def test_post_model_verification_crash_reentry_never_calls_model(tmp_path, monkeypatch):
    import tracefix.comparison_campaign as campaign

    root, protocol = fake_qualified(tmp_path, monkeypatch)
    monkeypatch.setattr(campaign, "check_protocol", lambda root: protocol)
    monkeypatch.setattr(campaign, "verify", lambda *a: {"passed": True, "offline": True})
    directory = root / "trials" / "001"
    directory.mkdir(parents=True)
    (directory / "patch.diff").write_text(PATCH)
    from tracefix.comparison import file_sha

    row = {
        **schedule()[0],
        "finished": False,
        "phase": "agent_completed",
        "patch_sha256": file_sha(directory / "patch.diff"),
    }
    write_json(directory / "record.json", row)
    assert run(root, max_trials=1)[0]["passed"]
    assert not (root / "requests.json").exists()
    (directory / "patch.diff").write_text("tampered")
    with pytest.raises(ValueError, match="patch changed"):
        finalize_trial(directory, row, protocol["tasks"][TASK_IDS[0]])


def test_empty_patch_and_unsupported_task_kind(tmp_path):
    assert not verify({}, tmp_path / "absent", tmp_path / "verify")["passed"]
    with pytest.raises(ValueError, match="unsupported"):
        qualify_task({"source": str(tmp_path), "task_id": TASK_IDS[0], "kind": "unknown"}, tmp_path)


def test_reference_behavior_must_be_qualified_and_regressions_pass(tmp_path, monkeypatch):
    import tracefix.comparison_campaign as campaign

    task = SimpleNamespace(
        id=TASK_IDS[3],
        test_pythonpath_paths=(),
        problem_statement="public problem",
        task_dir=tmp_path,
        hashes=SimpleNamespace(model_dump=lambda **k: {"gold": "hash"}),
    )
    (tmp_path / "task.json").write_text("{}")
    monkeypatch.setattr(campaign, "load_real_issue_tasks", lambda *a, **k: [task])
    behavior = SimpleNamespace(
        eligible_for_llm_prescreen=False,
        qualification_type="wrong",
        eligibility_reason="failed",
        model_dump=lambda **k: {"qualified": False},
        gold_evidence=SimpleNamespace(executed_node_ids=("test.py::test_case",)),
    )
    monkeypatch.setattr(campaign, "validate_real_task_behavior", lambda *a, **k: behavior)
    spec = {
        "source": str(tmp_path),
        "task_id": TASK_IDS[3],
        "kind": "real",
        "task_root": str(tmp_path),
        "python": sys.executable,
        "recipe": {"task_id": TASK_IDS[3]},
        "regressions": ["test.py"],
    }
    with pytest.raises(ValueError, match="unqualified"):
        qualify_task(spec, tmp_path / "first")
    behavior.eligible_for_llm_prescreen = True
    behavior.qualification_type = "assertion_failure"
    monkeypatch.setattr(campaign, "checked_test", lambda *a: {"passed": False})
    with pytest.raises(ValueError, match="regressions"):
        qualify_task(spec, tmp_path / "second")
    monkeypatch.setattr(
        campaign,
        "checked_test",
        lambda *a: {
            "passed": True,
            "test": {"output": {"test_counts": {"node_ids": ["test.py::test_case"]}}},
        },
    )
    monkeypatch.setattr(campaign, "source_identity", lambda *a: {"fake": True})
    monkeypatch.setattr(
        campaign,
        "inspect_test_environment",
        lambda *a, **k: SimpleNamespace(fingerprint_sha256="fake"),
    )
    result = qualify_task(spec, tmp_path / "third")
    assert "hidden" in result["public_failure"] or "隐藏" in result["public_failure"]
    assert result["issue"] == "public problem"


def test_public_qualification_keeps_corrected_reference_and_public_evidence(tmp_path, monkeypatch):
    import tracefix.comparison_campaign as campaign

    package = tmp_path / "benchmarks" / "public_tasks" / TASK_IDS[0]
    package.mkdir(parents=True)
    (package / "task.md").write_text("public issue")

    def qualified(source, python, output, task):
        prepared = output / "prepared-source"
        prepared.mkdir(parents=True)
        write_json(
            output / "base-1" / "record.json",
            {
                "targets": [
                    {
                        "test": {
                            "output": {
                                "stdout": "public failure",
                                "test_counts": {"node_ids": ["test.py::test_fix"]},
                            }
                        }
                    }
                ]
            },
        )
        return {"qualified": True, "corrected_reference": "saved provenance"}

    fake = SimpleNamespace(
        qualify=qualified,
        load_manifest=lambda *a: {
            "task_target": "test.py",
            "regression_targets": ["reg.py"],
            "source_import": "sample",
        },
    )
    monkeypatch.setattr(
        campaign.importlib.util,
        "spec_from_file_location",
        lambda *a: SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None)),
    )
    monkeypatch.setattr(campaign.importlib.util, "module_from_spec", lambda *a: fake)
    monkeypatch.setattr(campaign, "source_identity", lambda *a: {"fake": True})
    monkeypatch.setattr(
        campaign,
        "inspect_test_environment",
        lambda *a, **k: SimpleNamespace(fingerprint_sha256="fake"),
    )
    result = qualify_task(
        {
            "source": str(tmp_path),
            "python": sys.executable,
            "task_id": TASK_IDS[0],
            "package": str(package),
            "kind": "public",
        },
        tmp_path / "out",
    )
    assert result["public_failure"] == "public failure"
    assert result["qualification"]["corrected_reference"] == "saved provenance"
    assert result["verification_nodes"] == ["test.py::test_fix"]


@pytest.mark.parametrize(
    "eligible,changed,passed", [(True, False, True), (True, True, False), (False, False, False)]
)
def test_real_independent_verification_checks_regression_identity(
    tmp_path, monkeypatch, eligible, changed, passed
):
    import tracefix.comparison_campaign as campaign

    task = SimpleNamespace(
        id=TASK_IDS[3],
        task_dir=tmp_path,
        hashes=SimpleNamespace(model_dump=lambda **k: {"gold": "hash"}),
    )
    (tmp_path / "task.json").write_text("{}")
    patch = tmp_path / "patch.diff"
    patch.write_text(PATCH)
    from tracefix.comparison import file_sha

    spec = {
        "kind": "real",
        "task_root": str(tmp_path),
        "task_id": TASK_IDS[3],
        "task_metadata_sha256": file_sha(tmp_path / "task.json"),
        "artifact_hashes": {"gold": "hash"},
        "source": str(tmp_path),
        "python": sys.executable,
        "recipe": {"task_id": TASK_IDS[3]},
        "qualification_nodes": ["hidden.py::test_fix"],
        "regressions": ["public.py"],
        "regression_nodes": ["public.py::test_regression"],
        "environment_identity": "fake",
    }
    monkeypatch.setattr(campaign, "load_real_issue_tasks", lambda *a, **k: [task])
    monkeypatch.setattr(
        campaign,
        "validate_agent_patch_strict",
        lambda *a, **k: SimpleNamespace(
            eligible=eligible, model_dump=lambda **k: {"passed": eligible}
        ),
    )
    monkeypatch.setattr(
        campaign,
        "checked_test",
        lambda *a: {
            "passed": True,
            "test": {
                "output": {
                    "test_counts": {
                        "node_ids": ["changed" if changed else "public.py::test_regression"]
                    }
                }
            },
        },
    )
    monkeypatch.setattr(
        campaign,
        "inspect_test_environment",
        lambda *a, **k: SimpleNamespace(fingerprint_sha256="fake"),
    )
    assert verify(spec, patch, tmp_path / "verify")["passed"] is passed
    spec["task_metadata_sha256"] = "corrupt"
    with pytest.raises(ValueError, match="identity"):
        verify(spec, patch, tmp_path / "wrong")


def test_verifier_rejects_illegal_and_test_patches_without_running_pytest(tmp_path):
    catalog, _ = fixture_catalog(tmp_path)
    spec = read_json(catalog)[0]
    patch = tmp_path / "bad.patch"
    patch.write_text(PATCH.replace("sample.py", "../outside.py"))
    assert not verify(spec, patch, tmp_path / "escape")["passed"]
    patch.write_text(
        "*** Begin Patch\n*** Update File: test_sample.py\n@@\n-"
        "from sample import add\n+from sample import add as bypass\n*** End Patch\n"
    )
    assert verify(spec, patch, tmp_path / "tests")["reason"] == "modified tests/configuration"


def test_static_excerpt_uses_keywords_inside_long_files(tmp_path):
    from tracefix.comparison import static_material

    (tmp_path / "module.py").write_text("# filler\n" * 600 + "def unique_bug_target(): return 0\n")
    text = static_material(tmp_path, "unique_bug_target", ["module.py"])
    assert "def unique_bug_target" in text
