# TraceFix

TraceFix 是一个面向真实 GitHub Issue 的 Coding Agent 项目。它计划通过仓库结构检索、动态上下文和测试驱动的补丁验证，在固定模型与 Token 预算下提高 Bug 修复成功率与成本效率。

当前版本为 **V0.1 最小可运行闭环**。它已经能够让模型读取和搜索代码、应用补丁、运行 pytest、检查 Git diff，并根据工具反馈继续推理。

## 当前包含

- Pydantic 强类型消息、工具调用与消息历史
- Agent 配置、运行状态、抽象基类和最小控制循环
- 统一 LLM 接口与可选 LiteLLM 适配器
- 五个面向 Python/Git 仓库的本地工具
- Agent、模型、工具、消息和轨迹异常体系
- Trace 事件与输出端协议

## 架构

```text
BaseAgent ── MinimalAgent
├── BaseLLM ── LiteLLMAdapter
├── MessageHistory ── Message / ToolCall
├── ToolRegistry ── BaseTool / ToolSpec / ToolResult
├── AgentConfig / AgentState
└── TraceSink ── TraceEvent（可选）
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

## 最小 Agent 示例

```python
from tracefix import (
    LLMConfig,
    LiteLLMAdapter,
    MinimalAgent,
    create_default_tool_registry,
)

llm = LiteLLMAdapter(LLMConfig(model_name="openai/gpt-5"))
tools = create_default_tool_registry("D:/path/to/target-repository")
agent = MinimalAgent(llm, tools)

state = agent.run("修复空 token 导致 Parser 崩溃的问题，并运行相关测试。")
print(state.status, state.final_output)
print(state.input_tokens, state.output_tokens, state.cost_usd)
```

调用真实模型前，需要按 LiteLLM 对应供应商的要求配置 API Key。目标目录必须是已有初始提交的 Git 仓库；Agent 会直接修改该目录，因此评测时应为每次运行准备独立副本。

## Agent Loop

`MinimalAgent` 使用原生 tool calling，单步流程如下：

```text
检查预算 → 请求模型 → 记录消息与 Token/成本
                         ├── 无工具调用 → 完成任务
                         └── 有工具调用 → 顺序执行并写回结果 → 下一步
```

普通工具错误会作为结构化反馈返回模型，使其有机会调整方案。步骤、Token、运行时间或测试次数耗尽时，Agent 会以 `INTERRUPTED` 状态停止；不可恢复的模型或协议错误会标记为 `FAILED`。

## 五个基础工具

- `search_code`：在仓库文本文件中按关键词搜索代码位置。
- `read_file`：按行号读取文件，单次最多返回 400 行。
- `apply_patch`：先通过 `git apply --check` 验证，再应用 unified diff。
- `run_tests`：以 `shell=False` 执行 `pytest` 或 `python -m pytest`。
- `get_git_diff`：读取 tracked 修改和未跟踪新文件的 unified diff。

所有文件路径都必须位于传入的仓库根目录内，并拒绝绝对路径、`..`、`.git` 和符号链接逃逸。输出默认限制为 20,000 字符，长测试日志与 diff 会保留首尾并标记截断。

> 安全提示：V0.1 的路径工具具备仓库边界检查，但 pytest 仍然是本机进程。这不是操作系统级沙箱，不应直接用于运行不可信仓库；Docker 隔离将在后续版本加入。

## V0.1 预算模型

`AgentConfig` 预留以下默认限制，后续 Agent Loop 将统一消费这些字段：

- 最大步骤：30
- 最大输入 Token：80,000
- 最大输出 Token：20,000
- 最大运行时间：1,200 秒
- 最大测试次数：8

模型用量分别记录输入 Token、输出 Token 和 LiteLLM 返回的美元成本 `cost_usd`。

## 本阶段非目标

- Repository Indexer、动态上下文、Hypothesis Manager 和 Patch Verifier
- JSONL 文件写入、CLI、Dockerfile 与 Docker 沙箱

## 测试

测试使用脚本化 LLM 和临时 Git 仓库，不会访问真实模型 API：

```bash
pytest --cov=tracefix --cov-report=term-missing
ruff check .
```

## 后续接入点

下一阶段可以在当前闭环外加入 JSONL 轨迹写入器和统一评测入口，先建立 mini-swe-agent Baseline；随后再逐步接入 Repository Indexer、动态上下文和 Patch Verifier，并通过消融实验验证各模块贡献。
