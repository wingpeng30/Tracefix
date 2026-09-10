# TraceFix 开发历程与代码说明

本文集中说明各阶段实际完成的代码、验证方式和版本位置。实验数字的详细解释见
[`experiments/README.md`](experiments/README.md)。

## 版本总览

| 阶段 | Git 位置 | 核心成果 |
| --- | --- | --- |
| V0 / 0.0.1 | `6243e2a` | 强类型核心协议骨架 |
| V0.1 / 0.1.0 | `5c6e15b`，tag `v1` | 最小 Agent Loop 与五个工具 |
| V0.2 / 0.2.0 | `657fdc1`，tag `v0.2.0` | 真实 LLM、CLI、隔离运行与 Baseline |
| V0.3 / 0.3.0 | `66df9fa`，tag `v0.3.0` | 确定性分层上下文压缩 |
| V0.3.1 | `6f3ff29`、`7ef5a66`，tag `v0.3.1` | 补丁兼容、失败恢复与可靠收尾 |
| 长上下文评测 | `247a881`、`fe54a35` | 长上下文任务与 32k A/B 留档 |

说明：早期发布时将 V0.1 标签命名为 `v1`；包版本仍是 `0.1.0`。后续标签统一采用
语义版本格式。

## V0：强类型核心接口

目标是在不实现循环和工具逻辑的前提下，先建立稳定、可测试的 Agent 协议。

完成内容：

- `tracefix.messages`：`MessageRole`、`ToolCall`、`Message`、`MessageHistory`，校验 assistant
  工具调用和 tool result 的调用 ID 配对关系。
- `tracefix.models`：同步 `BaseLLM.complete()`、`LLMConfig`、`LLMResponse`、`TokenUsage` 和
  可选的 `LiteLLMAdapter`。
- `tracefix.agent`：抽象 `BaseAgent`、默认预算、生命周期状态和运行计数。
- `tracefix.tools`：`ToolSpec`、`ToolResult`、同步 `BaseTool`、拒绝重复名称的 `ToolRegistry`。
- `tracefix.tracing`：稳定的 `TraceEvent`、事件类型和 `TraceSink` 协议。
- `tracefix.exceptions`：Agent、预算、LLM、工具、消息和轨迹分层异常，并保留原异常链。
- 使用 `src/tracefix/` 包布局、Pydantic 强类型和统一包根导出。

这一阶段的关键取舍是先固定模块边界，不提前加入 Agent Loop、CLI、Docker、检索器或
Verifier，避免后续功能建立在裸字典和不稳定异常上。

## V0.1：最小 Coding Agent 闭环

目标是让单 Agent 能完成“模型调用—工具执行—反馈—继续推理—最终回答”。

完成内容：

- 新增 `MinimalAgent`，按模型请求次数累计步骤，按供应商 usage 累计输入/输出 Token 和费用。
- 实现步骤、Token、时间与测试次数预算；终止时为未回应工具调用补齐失败结果，避免消息悬空。
- 实现五个稳定工具：`search_code`、`read_file`、`apply_patch`、`run_tests`、`get_git_diff`。
- 路径工具限制在仓库根内，拒绝绝对路径、`..`、`.git`、敏感 dotenv 和符号链接逃逸。
- `apply_patch` 采用 `git apply --check` 后再应用；`run_tests` 使用 `shell=False` 且仅允许 pytest。
- Agent 产生任务、消息、模型、工具、状态和错误事件，完整工具结果以 JSON 写回模型。

安全边界：pytest 仍是本机进程，不是操作系统级沙箱；仓库必须使用独立干净副本。

## V0.2：真实 DeepSeek、CLI 与评测框架

目标是从只能在测试中运行的 Agent，扩展为可真实调用模型、保存产物并批量评测的系统。

完成内容：

- `LiteLLMAdapter` 接入 `deepseek/deepseek-v4-flash`，通过 `extra_body` 关闭 thinking。
- `tracefix run` 和 `tracefix eval` 非交互 CLI，支持命令行、环境变量和默认值的配置优先级。
- `TraceFixRunner` 验证干净 Git 源仓库，从当前 HEAD 建立无硬链接独立克隆。
- 每次任务保存 `trajectory.jsonl`、`result.json`、`patch.diff` 和工作区。
- `.env` 只在 Runner 进程加载；模型密钥不会复制到工作区，pytest 子进程移除敏感环境变量。
- 记录输入/输出 Token、LiteLLM 美元费用、固定汇率和人民币估算；费用缺失时显式标记不完整。
- 建立 10 个可复现合成 Python Bug，评测器在 Agent 结束后独立运行测试判断 resolved。

当时完整自动化验收为 100 项测试通过，总覆盖率 92.39%，Ruff 和 compileall 通过。

## V0.3：确定性分层上下文压缩

目标是在不调用额外摘要模型、不破坏完整审计历史的情况下，减少长轨迹发送给供应商的内容。

完成内容：

- 新增 `ContextConfig`、`ContextManager`、`ContextView` 和 `ContextMetrics`。
- 每次模型请求前创建临时请求视图；原始 `MessageHistory` 和 JSONL 轨迹保持不变。
- 第一层裁剪超长工具结果，保留首部、尾部和明确裁剪标记。
- 第二层以完整 assistant/tool 批次为单位折叠旧轮次，永久保留 system prompt、原始任务和近期批次。
- 确定性摘要只抽取已有事实，不生成新推理，也不产生额外模型费用。
- 默认 1M 硬窗口、32k 软触发阈值和 37.5% 近期保留比例；CLI 支持关闭和覆盖参数。
- 新增 `context_prepared`、`context_compacted` 事件与压缩前后估算指标。

3k 压力实验暴露了补丁工具协议问题。详细失败证据记录在
[`experiments/v0.3.0-context-3k.md`](experiments/v0.3.0-context-3k.md)。

## V0.3.1：补丁协议与 Agent 完成条件修复

目标是根据真实 7/10 失败轨迹修复工具兼容和无效恢复循环。

完成内容：

- `apply_patch` 同时支持 Git unified diff 与 `*** Begin Patch / *** Update File` 更新块。
- Begin Patch 先在内存中做严格上下文匹配并转换为标准 diff；危险路径、歧义或不匹配时不写文件。
- Git 应用增加 `--recount`，只修正错误 hunk 行数，不放宽代码上下文匹配。
- 完全相同且已经失败的补丁不再执行，仍返回合法 tool result 维持消息协议闭合。
- 连续补丁失败后加入一次恢复提示，要求重新读取文件并切换受支持格式。
- 最近测试通过且 Git Diff 非空后加入收尾提示，避免已经解决却耗尽步骤。
- Runner 要求显式传入 Git 根目录，避免 pytest 临时目录意外继承外层仓库。

10 题真实 API 回归恢复到 10/10，所有任务正常完成且没有工具失败或重复调用；Token 下降
来自更短、更稳定的轨迹，而非上下文压缩。详细解释见
[`experiments/v0.3.1-agent-loop-regression.md`](experiments/v0.3.1-agent-loop-regression.md)。

## 长上下文任务与 32k A/B

为避免在短任务上用不合理的 3k 阈值，新增两道受控长上下文任务。`BenchmarkTask` 支持受限
声明式文本生成：只生成仓库内相对路径的 UTF-8 文本，不执行任务代码。每题生成 24 份大于
6 KB 的契约，并要求每轮最多读取 4 份，形成可整体折叠的多个工具批次。

离线测试验证完整读取会自然超过 32k。真实 A/B 中，关闭压缩和 32k 压缩均解决 2/2，实验
组实际输入 Token 降低 49.89%。其机制证据与局限见
[`experiments/v0.3.1-context-32k-ab.md`](experiments/v0.3.1-context-32k-ab.md)。

## 当前验证状态

在 commit `fe54a35` 后重新执行完整测试：

- `123 passed`
- 总覆盖率 `91.20%`，高于 90% 门槛
- `ruff check .` 通过
- `python -m compileall -q src tests` 通过

真实 API 原始产物位于本机 `runs/`，不会提交 Git；脱敏实验数据和解释已经纳入仓库。

## V0.3.2：实验可追溯性与语义型多文件任务

本版为每次 `run` 自动生成 `RunProvenance`：记录 TraceFix 源码版本、commit、工作区脏状态、
任务 SHA-256、实际 LiteLLM 参数、Python/平台和直接依赖版本。该清单同时写入
`result.json` 与轨迹首条 `run_provenance` 事件，配置或认证失败也尽量保留。

`--record-request-views` / `TRACEFIX_RECORD_REQUEST_VIEWS=true` 可额外写入
`model_request_view`。该事件保存压缩后真正传给模型的消息和工具定义，统一清理凭据；完整
`MessageHistory` 不变。由于事件可能明显增大轨迹，生产默认关闭。

新增 `benchmarks/context_tasks/` 下四道任务，并扩展 `BenchmarkTask.hidden_tests_dir`。隐藏测试
只在 Agent 结束后复制进运行工作区，既不进入模型上下文，也不占 Agent 测试预算。详细设计、
非目标和接口影响见 [`tasks/v0.3.2-traceability-and-context-tasks.md`](tasks/v0.3.2-traceability-and-context-tasks.md)，
离线验证见 [`experiments/v0.3.2-fixture-validation.md`](experiments/v0.3.2-fixture-validation.md)。

发布前自动化验收：`130 passed`，总覆盖率 `91.14%`，`ruff check .` 与
`python -m compileall -q src tests` 均通过。本版没有产生真实模型费用。

## V0.4.0：独立验收与重复配对实验

将 Agent 正常结束、公开测试、隐藏验收和测试篡改拆为独立指标；`resolved` 采用四项联合
判定。新增 `paired-eval`，按 C/T、T/C 顺序交替运行至少三次，并从 JSONL 轨迹统计文件
重读、重复调用、失败工具、工具结果裁剪、真实历史折叠及折叠后失败。代码实现说明见
[`tasks/v0.4.0-independent-verification-and-paired-eval.md`](tasks/v0.4.0-independent-verification-and-paired-eval.md)。

正式实验在 commit `2c3de4e` 上交替运行 24 次，两组均 12/12，但 32k 组真实折叠为 0；
不能把 +3.99% 输入 Token 波动解释为压缩影响。实验数据、无效预运行费用和下一功能决策见
[`experiments/v0.4.0-paired-context-32k.md`](experiments/v0.4.0-paired-context-32k.md)。

最终自动化验收为 `136 passed`、覆盖率 `91.76%`，Ruff、compileall 与 Git diff 检查通过。

## V0.5.0：真实 GitHub Issue 任务接口

基于已发布的 `v0.4.0`（commit `007520a`），新增 SWE-bench Verified 真实任务清单、固定提交
克隆、任务文件 SHA-256、gold/test patch 文件集合校验和隐藏测试补丁接入点。首批三题来自
pytest、Pylint 和 Sphinx，gold patch 均实际修改多个源码文件；源码不纳入本仓库。

随后加入历史项目独立 Python 3.9 测试环境、环境依赖指纹、测试 bootstrap 哈希、
`validate-real-behavior`、32k 预筛选和受门槛保护的真实配对实验器。三题清单中的隐藏用例均
在 base 上失败、gold 后通过；仍未执行官方 Docker PASS_TO_PASS，也不形成 Agent 解决率结论。完整说明见
[`tasks/v0.5.0-real-github-issue-tasks.md`](tasks/v0.5.0-real-github-issue-tasks.md)。

当前自动化验收为 `152 passed`、覆盖率达到 90% 门槛，Ruff 与 compileall 通过；三个上游
固定提交均成功检出，三个隐藏测试补丁与 gold patch 组合均通过 `git apply --check`，且
3/3 base 隐藏验收失败、3/3 gold 隐藏验收通过。
