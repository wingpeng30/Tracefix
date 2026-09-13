# TraceFix 开发历程与代码说明

本文集中说明各阶段实际完成的代码、验证方式和版本位置。实验数字的详细解释见
[`experiments/README.md`](experiments/README.md)。

## V0.6.0（开发中）：确定性 Repository Indexer 与 Repo Map

新增 Python AST 符号、导入和保守调用名称索引，并在每次隔离运行中生成 `repo-map.json`，将任务相关候选源码、符号行号和测试候选作为模型可见 system 上下文。轨迹新增索引/地图事件，CLI 支持 `--no-repo-map` 作为未来定位 A/B 对照。详见 [`tasks/v0.6.0-repository-index-and-repo-map.md`](tasks/v0.6.0-repository-index-and-repo-map.md)。

本次不运行付费 API。离线检索评测显示：Repo Map 对 Pylint、pytest 有局部帮助，但三题总计
Hit@5 与文件名关键词基线打平，Hit@1 和 MRR 更低；下一步是完善模块/相对导入/符号图，而非
直接进行付费 LLM A/B。完整数据与解释见
[`experiments/v0.6.0-repo-map-retrieval-eval.md`](experiments/v0.6.0-repo-map-retrieval-eval.md)。

随后加入模块/符号反向索引、相对导入解析和一跳图扩展，在相同三题、零 LLM 成本复评上把
Hit@5 由 66.67% 提升到 100%、Recall@5 由 44.44% 提升到 55.56%。Hit@1 仍为 0%，MRR 仅与
基线打平，故下一步仍应先优化排序，不宣称 Agent 修复率提升。详见
[`experiments/v0.6.0-repository-graph-retrieval-eval.md`](experiments/v0.6.0-repository-graph-retrieval-eval.md)。

进一步加入源码目录先验、显式模块名精确加权和区分边权重的种子排序后，在同一离线样本中将
Hit@1 提升至 66.67%，MRR 提升至 0.833，Hit@5 保持 100%。这支持进行小规模真实 Agent
预筛选，但不构成修复成功率结论；详见
[`experiments/v0.6.0-seed-ranking-retrieval-eval.md`](experiments/v0.6.0-seed-ranking-retrieval-eval.md)。

随后完成真实 Issue 的 Repo Map 单次交替预筛选：每题关闭/开启各一次、两组均开启 32k 压缩并进行
Agent 不可见的隐藏验收。Repo Map 将首次读取目标文件的平均步骤从 2.00 提前至 1.33，但 6 次运行都在
80k 输入 Token 预算耗尽前中断，resolved 均为 0。因此没有进入每组 3 次的正式配对实验；下一步应先
压缩无效搜索并加强“读取后补丁—测试”的行动引导。完整方法、结果和限制见
[`tasks/v0.6.0-real-repo-map-prescreen.md`](tasks/v0.6.0-real-repo-map-prescreen.md)。

## V0.6.1（开发中）：定位后的行动收敛

针对真实预筛选中“已经读到目标文件，却持续搜索直到累计输入预算耗尽”的失败模式，新增
`EXPLORE/PATCH/VERIFY/FINISH` 阶段、Repo Map Top-2 首轮行动提示、成功搜索/读取的紧凑缓存、
探索软预算，以及累计输入 Token 50%/70%/85% 收敛提示。`search_code` 默认只返回 20 条且单文件
最多 5 条，并补充首次补丁、首次测试、目标文件后搜索与阶段转换指标。本阶段没有付费复跑，详见
[`tasks/v0.6.1-agent-action-efficiency.md`](tasks/v0.6.1-agent-action-efficiency.md)。

## V0.7.1（开发中）：验收完整性与失败反馈

将 `agent_selected_tests_passed`、Agent 可见的 `public_tests_passed`、隐藏
`independent_tests_passed`、补丁可应用性和测试文件修改拆分记录。当前真实任务的公开测试字段
明确为 `null`，不再把模型最后一次自选测试伪装为公开测试。另对无效补丁和截断搜索增加定向恢复
提示，并抑制 Pylint 故意非法转义夹具在 Indexer 中产生的 `SyntaxWarning`。一次 Pylint 实运行
验证了预算中断后仍可完整留档，但不是完整对照实验；详见
[`tasks/v0.7.1-evaluation-integrity-and-feedback.md`](tasks/v0.7.1-evaluation-integrity-and-feedback.md)。
随后完成同题 Token 行动优化冒烟运行：处理组显著减少搜索并运行了一次测试，但累计 Token 没有
下降、两组隐藏验收均失败。因此只作为机制观察，而不作为效果结论；详见
[`experiments/v0.7.1-pylint-token-optimization-smoke.md`](experiments/v0.7.1-pylint-token-optimization-smoke.md)。

## V0.7.2（开发中）：真实任务预算与重新预筛选

将真实任务命令的默认累计输入预算从通用的 80k 提高至 350k，普通 `run` / `eval` 默认值不变。
三个真实任务重新预筛选中，pytest 成功通过隐藏验收，Pylint 仍超预算、Sphinx 隐藏验收失败；三题都
没有触发 32k 历史折叠，故不进入正式压缩 C/T 配对实验。完整方法、运行命令、成本和决策见
[`tasks/v0.7.2-real-budget-and-prescreen.md`](tasks/v0.7.2-real-budget-and-prescreen.md)。

## V0.7.3（开发中）：非重复多文件长轨迹筛选

用四道规则分散、带隐藏验收的多文件合成任务筛选 32k 历史折叠候选。四题均被解决并通过隐藏
验收，却只形成约 4–6k 的单次请求、没有发生折叠。这证明“多文件”本身不足以构成压缩实验；
它们保留为准确率基准、拒绝进入压缩 A/B。完整数据与纳入标准见
[`experiments/v0.7.3-natural-multifile-screen.md`](experiments/v0.7.3-natural-multifile-screen.md)。
同日对三个真实 Issue 做离线结构筛选，Repo Map 将 Hit@1 从 33.3% 提升至 66.7%，Hit@5 从
66.7% 提升至 100%，MRR 从 0.444 提升至 0.833；这是定位指标，不是 Agent 修复率结论。详见
[`experiments/v0.7.3-real-offline-retrieval.md`](experiments/v0.7.3-real-offline-retrieval.md)。

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

当前自动化验收为 `155 passed`、覆盖率达到 90% 门槛，Ruff 与 compileall 通过；三个上游
固定提交均成功检出，三个隐藏测试补丁与 gold patch 组合均通过 `git apply --check`，且
3/3 base 隐藏验收失败、3/3 gold 隐藏验收通过。

32k 单次预筛选固定在 commit `6a5537f`，三题最大单次请求估算为 13,972～28,257 Token，
0/3 发生历史折叠，因此按预注册门槛跳过正式配对实验。三题合计 751,769 输入 Token、
9,471 输出 Token，费用 `$0.04610248` / 约 `¥0.33193786`。主要失败是搜索和跨文件定位，
下一主功能确定为 AST 符号索引与确定性 Repo Map；详细解释见
[`experiments/v0.5.0-real-issue-32k-prescreen.md`](experiments/v0.5.0-real-issue-32k-prescreen.md)。

## V0.5.1：Windows CI 可移植性修复

`v0.5.0` 在本机 155 项测试通过，但 GitHub Windows Runner 暴露了两项环境差异：Git
检出把 LF 转成 CRLF，导致真实任务文本工件的字节哈希变化；CI 的多层 pytest 临时目录还会
让 Git clone/add/apply 碰到传统 Windows 路径长度限制。

V0.5.1 将任务身份哈希定义为“UTF-8 内容 + 统一 LF”的规范化 SHA-256；除换行外的任何
内容变化仍会使校验失败。任务准备和真实实验器的内部 Git 子进程使用命令级
`-c core.longpaths=true`，不依赖也不修改用户的全局 Git 设置。新增 CRLF/LF 等价测试、任务
清单不变量和危险补丁路径测试，使完整验收达到 `168 passed`、总覆盖率 `90.74%`；Ruff、
compileall 与 diff 检查通过。本补丁不重跑模型实验，也不改写 V0.5.0 的实验数据。首次修复后
CI 由 3 个失败降至 1 个，暴露 Runner 二次 clone 与工具层 Git 命令仍未开启长路径。

## V0.5.2：补齐 Runner 与工具层长路径

将命令级 `core.longpaths=true` 扩展到 `TraceFixRunner` 的仓库检查与隔离 clone、
`ApplyPatchTool`、`GetGitDiffTool` 以及工具注册前的 Git 仓库检查。这样从任务源仓库准备、
Agent 工作区创建、补丁应用到最终 Diff 收集都使用同一跨平台策略。

本版是 V0.5.1 的发布修复续版，不进行 DeepSeek 实验，也不改变 V0.5.0 已保存的预筛选数据。
本地完整验收为 `168 passed`、总覆盖率 `90.74%`，Ruff、compileall 与 diff 检查通过。
GitHub Actions Run #7 也在 Windows Python 3.11/3.12 两个作业成功通过；详细说明见
[`tasks/v0.5.2-runner-longpaths.md`](tasks/v0.5.2-runner-longpaths.md)。
## V0.7.0（开发中）：Token 效率策略可控化

新增模型可见工具结果投影、缓存可见性校验、测试后缓存失效和分段耗时统计。策略可用 `--no-token-optimization` 关闭，支持在固定任务、模型和预算下比较准确率、供应商 Token、费用与时延；完整实现说明及尚未运行付费实验的边界见 [`tasks/v0.7.0-token-efficient-agent.md`](tasks/v0.7.0-token-efficient-agent.md)。

三个真实任务的单次 C/T 预筛选中，开启组累计输入 Token 降低 10.42%，并将
`pytest-dev__pytest-8399` 从未解决变为解决；另外两题仍因累计 Token 超限中断，六次运行均未触发 32k 历史折叠。由于每组仅一次且工作区为 dirty，本结果只作为方向性证据，详见 [`experiments/v0.7.0-real-token-optimization-prescreen.md`](experiments/v0.7.0-real-token-optimization-prescreen.md)。
