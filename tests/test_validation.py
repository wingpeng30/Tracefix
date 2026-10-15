"""Validation identities bind only the explicit backend inputs."""

import hashlib
import json
from contextlib import contextmanager

import pytest

from tracefix.validation import docker_validation_environment_sha256


def _dispatcher(profile="workspace_write"):
    from tracefix.capabilities import (
        CapabilityCatalog,
        CapabilityPolicy,
        ExecutionProfile,
        ToolDispatcher,
    )

    return ToolDispatcher(CapabilityPolicy(
        CapabilityCatalog.for_tool_names(("run_tests",)), ExecutionProfile(profile)
    ))


class _TestTool:
    from tracefix.tools.base import ToolSpec

    spec = ToolSpec(name="run_tests", description="validation fixture")

    def execute(self, call):
        from tracefix.tools.base import ToolResult

        return ToolResult(call_id=call.id, tool_name=call.name, success=True,
                          output={"test_status": "passed"})


def test_local_validation_acquires_new_checkout_and_releases_every_call():
    from tracefix.messages import ToolCall
    from tracefix.validation import LocalValidationBackend

    events = []

    @contextmanager
    def acquire():
        tool = _TestTool()
        events.append(("acquire", tool))
        try:
            yield tool
        finally:
            events.append(("release", tool))

    backend = LocalValidationBackend(acquire, _dispatcher())
    assert backend.execute(ToolCall(id="one", name="run_tests"))["success"]
    assert backend.execute(ToolCall(id="two", name="run_tests"))["success"]
    assert events[0][1] is events[1][1]
    assert events[2][1] is events[3][1]
    assert events[0][1] is not events[2][1]


@pytest.mark.parametrize("failure", [None, "prepare", "execute", "export"])
def test_docker_validation_cleans_disposable_container_even_after_unknown_result(failure):
    from tracefix.messages import ToolCall
    from tracefix.validation import DockerValidationBackend

    events = []

    class Backend:
        def export_evidence(self):
            events.append("export")
            if failure == "export":
                raise OSError("export unknown")

        def close(self, *, remove):
            assert remove
            events.append("close")

    class Tool(_TestTool):
        def execute(self, call):
            events.append("execute")
            if failure == "execute":
                raise OSError("execute unknown")
            return super().execute(call)

    def create():
        events.append("create")
        return Backend()

    def prepare(backend):
        from tracefix.tools.base import ToolRegistry

        events.append("prepare")
        if failure == "prepare":
            raise OSError("prepare unknown")
        return ToolRegistry((Tool(),))

    backend = DockerValidationBackend(create, prepare, _dispatcher())
    call = ToolCall(id="one", name="run_tests")
    if failure:
        with pytest.raises(OSError, match="unknown"):
            backend.execute(call)
    else:
        assert backend.execute(call)["success"]
    assert events[-1] == "close"
    assert events.count("create") == 1


@pytest.mark.parametrize("kind", ["local", "docker"])
def test_validation_policy_denial_never_acquires_checkout_or_container(kind):
    from tracefix.exceptions import RunConfigurationError
    from tracefix.messages import ToolCall
    from tracefix.validation import DockerValidationBackend, LocalValidationBackend

    calls = []
    def acquire():
        calls.append("acquire")
    backend = (LocalValidationBackend(acquire, _dispatcher("inspect")) if kind == "local"
               else DockerValidationBackend(acquire, lambda _: None, _dispatcher("inspect")))
    with pytest.raises(RunConfigurationError) as caught:
        backend.execute(ToolCall(id="one", name="run_tests"))
    assert caught.value.context["reason_code"] == "profile_denied"
    assert calls == []


@pytest.mark.parametrize(
    ("image_id", "environment"),
    [
        (None, {}),
        ("sha256:" + "a" * 64, {"LANG": "zh_CN.UTF-8", "MODE": "test"}),
    ],
)
def test_docker_validation_identity_is_canonical(image_id, environment):
    expected = hashlib.sha256(
        json.dumps(
            {
                "backend": "docker",
                "image_id": image_id,
                "environment_variables": environment,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert docker_validation_environment_sha256(image_id, environment) == expected


def test_docker_validation_identity_changes_with_each_input():
    base = docker_validation_environment_sha256("sha256:one", {"A": "1"})

    assert docker_validation_environment_sha256("sha256:two", {"A": "1"}) != base
    assert docker_validation_environment_sha256("sha256:one", {"A": "2"}) != base
