import pytest

from tracefix import AgentStatus, BaseLLM, LLMConfig, LLMProviderError, MinimalAgent
from tracefix.exceptions import PreRequestBudgetExceeded


@pytest.mark.parametrize("prior_complete", [True, False])
def test_known_not_sent_preserves_previous_accounting(prior_complete):
    class RejectedLLM(BaseLLM):
        def complete(self, messages, tools=()):
            raise PreRequestBudgetExceeded("local reservation rejected")

    agent = MinimalAgent(RejectedLLM(LLMConfig(model_name="offline")))
    agent.run("task")
    agent.state.status = AgentStatus.RUNNING
    agent.state.usage_complete = prior_complete
    agent.state.cost_complete = prior_complete
    agent.state.input_tokens = 20
    with pytest.raises(PreRequestBudgetExceeded):
        agent.step()
    assert agent.state.usage_complete == prior_complete
    assert agent.state.cost_complete == prior_complete
    assert agent.state.input_tokens == 20


def test_provider_error_still_marks_usage_unknown():
    class UnknownLLM(BaseLLM):
        def complete(self, messages, tools=()):
            raise LLMProviderError("transport result unknown")

    agent = MinimalAgent(UnknownLLM(LLMConfig(model_name="offline")))
    with pytest.raises(LLMProviderError):
        agent.run("task")
    assert not agent.state.usage_complete
    assert not agent.state.cost_complete
