# TraceFix

TraceFix 是一个面向真实 GitHub Issue 的 Coding Agent 项目。它计划通过仓库结构检索、动态上下文和测试驱动的补丁验证，在固定模型与 Token 预算下提高 Bug 修复成功率与成本效率。

当前版本为 **V0 核心接口骨架**。这一阶段只建立稳定、可测试的数据边界，不包含 Agent 循环、代码修改工具或沙箱执行。

## 当前包含

- Pydantic 强类型消息、工具调用与消息历史
- Agent 配置、运行状态和抽象基类
- 统一 LLM 接口与可选 LiteLLM 适配器
- Tool 描述、执行结果、抽象接口和注册表
- Agent、模型、工具、消息和轨迹异常体系
- Trace 事件与输出端协议

## 架构

```text
BaseAgent
├── BaseLLM ── LiteLLMAdapter
├── MessageHistory ── Message / ToolCall
├── ToolRegistry ── BaseTool / ToolSpec / ToolResult
├── AgentConfig / AgentState
└── TraceSink ── TraceEvent
```

接口之间只交换 TraceFix 自己的数据模型。供应商响应在模型适配层被规范化，后续 Agent Loop、评测器和轨迹查看器不需要依赖 LiteLLM 的对象结构。

## 安装

仅安装核心接口：

```bash
pip install -e .
```

安装 LiteLLM 适配器依赖：

```bash
pip install -e ".[llm]"
```

安装开发依赖：

```bash
pip install -e ".[dev,llm]"
```

## 最小示例

```python
from tracefix import LLMConfig, LiteLLMAdapter, Message, MessageHistory, MessageRole

history = MessageHistory(
    [
        Message(role=MessageRole.SYSTEM, content="You are a coding agent."),
        Message(role=MessageRole.USER, content="Explain the failing test."),
    ]
)

llm = LiteLLMAdapter(LLMConfig(model_name="openai/gpt-5"))
response = llm.complete(history.snapshot())
print(response.message.content)
```

调用真实模型前，需要按 LiteLLM 对应供应商的要求配置 API Key。

## V0 预算模型

`AgentConfig` 预留以下默认限制，后续 Agent Loop 将统一消费这些字段：

- 最大步骤：30
- 最大输入 Token：80,000
- 最大输出 Token：20,000
- 最大运行时间：1,200 秒
- 最大测试次数：8

## 本阶段非目标

- Agent Loop 和自动终止逻辑
- `search_code`、`read_file`、`apply_patch`、`run_tests`、`get_git_diff` 的具体实现
- Repository Indexer、动态上下文、Hypothesis Manager 和 Patch Verifier
- JSONL 文件写入、CLI、Dockerfile 与 Docker 沙箱

## 测试

测试不会访问真实模型 API：

```bash
pytest --cov=tracefix --cov-report=term-missing
ruff check .
```

## 后续接入点

下一阶段可以在 `BaseAgent` 上实现最小循环，并为五个保留工具名分别提供确定性实现。循环只需要消费 `LLMResponse.message.tool_calls`、通过 `ToolRegistry` 找到工具，并把 `ToolResult` 转换为 `tool` 消息写回历史。


