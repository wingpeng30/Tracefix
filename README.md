# TraceFix

TraceFix 是一个面向真实 GitHub Issue 的单 Agent Coding 系统。项目计划在固定模型和
Token 预算下，通过仓库结构检索、动态上下文和测试驱动的补丁验证，提高 Bug 修复成功率
与成本效率。

当前版本为 **0.3.0**。它已经具备一条可真实运行的最小闭环，并新增确定性上下文管理：
完整轨迹始终保留，模型请求视图会按压力裁剪超长工具输出并折叠较早轮次。

## 当前能力

- Pydantic 强类型消息、模型响应、工具调用、运行配置与评测结果。
- 可运行的 `MinimalAgent` 单 Agent Loop。
- LiteLLM 模型适配层，默认接入 `deepseek/deepseek-v4-flash`。
- `search_code`、`read_file`、`apply_patch`、`run_tests`、`get_git_diff` 五个工具。
- 独立 Git 克隆、干净源仓库校验和源 commit 记录。
- UTF-8 JSONL 轨迹、最终 `patch.diff` 和 `result.json`。
- 输入/输出 Token、美元原始费用和人民币估算费用。
- 10 个可复现的合成 Python Bug 与串行 baseline 评测入口。
- 不调用额外模型的工具结果裁剪、历史折叠和压缩指标。

## 架构

```text
tracefix CLI
    │
    ├── TraceFixRunner ── 干净源仓库 → 独立 Git 克隆
    │       ├── LiteLLMAdapter → DeepSeek API
    │       ├── MinimalAgent → MessageHistory（完整历史）
    │       │                    └── ContextManager（模型请求视图）
    │       ├── ToolRegistry → 五个基础工具
    │       └── JSONLTraceSink
    │
    └── BenchmarkRunner
            ├── 合成任务准备
            ├── TraceFixRunner
            └── Agent 外独立 pytest 判定
```

Agent 内部的测试调用用于获得修复反馈；评测器最后执行的测试只负责判断 `resolved`，不会
反馈给 Agent，也不会占用 Agent 的测试次数预算。

## 安装

Python 版本要求为 3.11 或更高。

建议使用独立虚拟环境，避免 LiteLLM 的供应商依赖影响机器上已有的 Python 工具：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[llm]"
```

开发和测试环境：

```powershell
python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -e ".[dev,llm]"
```

核心数据模型仍然只依赖 Pydantic；LiteLLM 和 `.env` 加载器位于 `llm` 可选依赖中。
项目固定使用已验证的 `litellm==1.75.5.post2`，保证本阶段的适配行为可复现。

## 配置 DeepSeek

复制示例文件：

```powershell
Copy-Item .env.example .env
```

然后只在本机 `.env` 中填写：

```dotenv
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_API_BASE=https://api.deepseek.com
TRACEFIX_MODEL=deepseek/deepseek-v4-flash
TRACEFIX_USD_CNY_RATE=7.20
TRACEFIX_CONTEXT_ENABLED=true
TRACEFIX_CONTEXT_WINDOW_TOKENS=1000000
TRACEFIX_CONTEXT_TRIGGER_TOKENS=32000
TRACEFIX_CONTEXT_RETAIN_RATIO=0.375
```

`.env` 已被 Git 忽略。TraceFix 不提供 `--api-key` 参数，避免密钥进入 Shell 历史；密钥也
不会写入 Agent 消息、JSONL 轨迹或结果文件。真实 `.env` 文件不能被代码读取工具访问，
pytest 子进程会移除 API Key、Token、Password 等敏感环境变量。

## 运行一个任务

源仓库必须已经有初始提交并保持干净。TraceFix 不直接修改源仓库，而是在 `runs/` 下保留
独立克隆：

```powershell
tracefix run `
  --repo D:\repos\example `
  --task "parser.first_token 在空输入时不应抛出 IndexError，请修复并运行测试。"
```

也可以从 UTF-8 文件读取 Issue：

```powershell
tracefix run --repo D:\repos\example --task-file issue.md
```

常用预算参数：

```text
--max-steps                 最大模型请求次数，默认 30
--max-input-tokens          累计输入 Token，默认 80000
--max-output-tokens         累计输出 Token，默认 20000
--per-request-output-tokens 单次响应输出上限，默认 4096
--wall-time-seconds         总运行时间，默认 1200
--max-test-runs             Agent 内测试调用次数，默认 8
```

CLI 参数优先于 `TRACEFIX_*` 环境变量，环境变量优先于代码默认值。DeepSeek 默认关闭思考
模式；`thinking` 会按照 DeepSeek 的 OpenAI 兼容协议经由 `extra_body` 发送。当前消息协议尚未
保存思考模式工具轮次要求的 `reasoning_content`。

## 上下文压缩

TraceFix 保留两份不同用途的数据：`MessageHistory` 和 JSONL 轨迹保存全部原始消息，供审计
与复现；`ContextManager` 只为下一次模型调用生成临时视图。默认单次请求估算达到 32k
Token 时折叠较早的完整工具轮次，并保留约 37.5% 的近期上下文。单条工具结果超过 8,192
字符时保留前 4,096 和后 1,024 字符。

32k 是成本控制软阈值，不是 DeepSeek 的上下文极限。可使用以下参数调整或关闭：

```text
--no-context-compaction       关闭裁剪和折叠，用于未压缩对照组
--context-window-tokens       模型单次请求硬窗口，默认 1000000
--context-trigger-tokens      历史折叠软阈值，默认 32000
--context-retain-ratio        折叠后近期历史保留比例，默认 0.375
```

本地 Token 估算只决定是否触发压缩；计费、累计 Token 和预算判断仍以供应商 usage 为准。

## 运行 Baseline

首次真实冒烟建议只运行第一个任务，并使用更保守的预算：

```powershell
tracefix eval `
  --tasks benchmarks/tasks `
  --limit 1 `
  --max-steps 12 `
  --max-input-tokens 40000 `
  --max-output-tokens 8000 `
  --wall-time-seconds 300 `
  --max-test-runs 4
```

准备好后可以去掉 `--limit 1` 运行全部 10 个任务，也可以用多个 `--task-id` 选择任务。

每个合成任务包含：

- `task.json`：Bug 描述、测试命令和预期修改文件。
- `repo/`：不含嵌套 `.git` 的初始 Bug 仓库。
- `gold.patch`：证明任务可解的标准补丁。

自动化测试会验证所有任务在初始状态失败、应用 gold patch 后通过。gold patch 不会提供给
真实 Agent。

## v0.2.0 Baseline

2026-09-09 使用 `deepseek/deepseek-v4-flash` 完成全部 10 个合成任务。评测器只以 Agent
结束后的独立 pytest 结果判断是否解决，不依赖模型的自我声明。脱敏逐任务数据见
[`benchmarks/baselines/v0.2.0.json`](benchmarks/baselines/v0.2.0.json)。

| 指标 | 结果 |
| --- | ---: |
| Resolved | 10 / 10 |
| 模型请求步骤 | 74 |
| 输入 Token | 184,730 |
| 输出 Token | 8,982 |
| Agent 内测试调用 | 10 |
| 工具调用 | 87（完全重复 6 次） |
| API 费用 | $0.025195552 / 约 ¥0.18140797 |

运行时每个任务使用 12 步、40k 累计输入 Token、8k 输出 Token、5 分钟和 4 次测试的
上限。这些任务规模较小，100% 解决率不能代表真实 GitHub Issue 或 SWE-bench 表现。

## 运行产物

单任务目录如下：

```text
runs/<run_id>/
├── workspace/       # 模型实际修改的独立 Git 克隆
├── trajectory.jsonl # Agent 生命周期、消息、模型和工具事件
├── result.json      # 状态、预算、Token、费用与产物索引
└── patch.diff       # 相对源仓库 HEAD 的最终补丁
```

费用字段的含义：

- `cost_usd`：优先使用 LiteLLM 从响应计算的美元费用；若新模型响应别名暂未被其
  `completion_cost` 识别，则使用同一 LiteLLM 价格表和原始 usage 回退计算。
- `usd_cny_rate`：本次运行使用的固定汇率，默认 `7.20`。
- `cost_cny_estimate`：`cost_usd × usd_cny_rate`，只是可复现实验估算。
- `cost_complete`：如果任一模型响应缺少费用，则为 `false`，人民币费用保持 `null`，不会
  把未知费用错误显示为零。

## Python 接口

```python
from tracefix import RunConfig, TraceFixRunner

result = TraceFixRunner().run(
    RunConfig(
        repo="D:/repos/example",
        task="修复空输入导致的异常，并运行相关测试。",
        output_dir="runs",
    )
)

print(result.status, result.changed_files)
print(result.input_tokens, result.output_tokens)
print(result.cost_usd, result.cost_cny_estimate)
```

底层接口仍可单独组合：`BaseLLM`、`MinimalAgent`、`ToolRegistry`、`MessageHistory` 和
`TraceSink` 都从包根公开导出。

## 安全边界

- 文件路径被限制在隔离工作区内，并拒绝绝对路径、`..`、`.git`、真实 `.env` 和符号链接
  逃逸。
- `apply_patch` 会先执行 `git apply --check`，通过后才真正修改文件。
- `run_tests` 使用 `shell=False`，只允许 `pytest` 或 `python -m pytest`。
- Agent 仍然在本机执行 pytest，不是操作系统级沙箱。不要用于运行不可信仓库；Docker
  隔离将在后续版本加入。

## 测试

自动化测试不会访问真实模型 API：

```powershell
pytest --cov=tracefix --cov-report=term-missing
ruff check .
python -m compileall -q src tests
```

真实 API 冒烟测试由使用者显式执行，不进入 pytest 或 CI。

## 后续版本

下一阶段会用相同模型、任务和预算运行上下文压缩 A/B，再增加 Repository Indexer、AST
符号关系和错误栈驱动检索。本版本不包含 LLM 摘要、RAG、多 Agent、Docker 或前端。
