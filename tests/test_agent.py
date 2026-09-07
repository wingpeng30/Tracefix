from datetime import datetime

import pytest
from pydantic import ValidationError

from tracefix import AgentConfig, AgentState, AgentStatus, BaseAgent, BaseLLM, LLMConfig


class StubLLM(BaseLLM):
    def complete(self, messages, tools=()):  # pragma: no cover - not used in interface tests
        raise NotImplementedError


class StubAgent(BaseAgent):
    def run(self, task: str) -> AgentState:
        self.state.task = task
        return self.state

    def step(self) -> None:
        return None


def test_agent_config_defaults() -> None:
    config = AgentConfig()
    assert config.max_steps == 30
    assert config.max_input_tokens == 80_000
    assert config.max_output_tokens == 20_000
    assert config.wall_time_seconds == 1_200
    assert config.max_test_runs == 8


def test_base_agent_is_abstract() -> None:
    llm = StubLLM(LLMConfig(model_name="test"))
    try:
        BaseAgent(llm)  # type: ignore[abstract]
    except TypeError as error:
        assert "abstract" in str(error)
    else:  # pragma: no cover
        raise AssertionError("BaseAgent must not be directly instantiable")


def test_subclass_receives_dependencies_and_resets_state() -> None:
    llm = StubLLM(LLMConfig(model_name="test"))
    agent = StubAgent(llm)

    state = agent.run("fix parser")
    state.status = AgentStatus.RUNNING
    agent.reset()

    assert agent.llm is llm
    assert agent.state.status is AgentStatus.CREATED
    assert agent.state.task is None
    assert len(agent.history) == 0


def test_agents_have_isolated_state() -> None:
    llm = StubLLM(LLMConfig(model_name="test"))
    first = StubAgent(llm)
    second = StubAgent(llm)
    first.state.step_count = 4
    assert second.state.step_count == 0


def test_agent_state_requires_consistent_aware_timestamps() -> None:
    with pytest.raises(ValidationError):
        AgentState(started_at=datetime(2026, 1, 1))

