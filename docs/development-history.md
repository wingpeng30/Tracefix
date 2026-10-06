# TraceFix 开发历程与代码说明

## 2026-10-06 面试归档与空间清理

新增项目历程、技术取舍、实验节点、问答与离线演示入口。清理工具只处理明确归属的
TraceFix 产物，先压缩精选证据并逐文件核验，保护 Git 跟踪文件、当前 C 记录及冻结依赖，
远程精确提交备份通过后再删除；目录变化、越界、链接、访问失败均拒绝或跳过。
历史结论与失败不改写，原始证据覆盖及删除/跳过清单保存在专用 E 盘档案目录。
不调用付费模型，不修改产品策略；完整当前 C 工作区保留，因为 record 校验依赖其文件。

## 2026-10-06 B/C新协议：180秒硬请求期限

用户已选择仅B/C、单次模型请求硬超时180秒，整题60分钟。新协议与旧ABC不混算；
原300元授权扣除旧批次79.085016元，剩余220.914984元，换批次不重置额度。
固定20题×两臂×三次，共120次；产品策略不变，工程与精确CI门槛通过前不付费。
产物在E:\TracefixExperiments\20261006-bc-holdout-180s，归属和清理范围有标记。
详情见[新协议记录](experiments/2026-10-06-bc-holdout-180s.md)。


2026-10-06 后续：用户明确要求继续至实际余额不足，已取消额外的账户余额30元启动门槛。
原300元协议账本、单次10元上限、冻结代码及日程保持不变；首段6次报告已归档到
`E:\TracefixExperiments\20261006-abc-holdout\phase-1-before-wallet-resume`。
恢复授权与原始报告哈希见同根目录`resume-authorization.json`；恢复批次已按用户指示停止：19次启动、18次完成、146个请求全部结算。
第19次Agent已完成，但独立验收交付被停止；原记录与固定补丁保持原样。
下面的6次统计为首段历史结果，不是恢复批次的最终统计。


## 2026-10-06：修复评测污染并冻结A/B/C留出协议

仅修改评测基础设施，产品基线e380329不改。新增20题/180次版本化协议、平衡三臂块、
统一受保护的binary产品补丁收集、异常自动证据、连续交付计时、官方估算/响应身份校验、
三臂统计及题目原始资格审计。历史配置、账本和失败均保留。

精确付费实现cf8d175五项CI通过；Windows3.11/3.12各1178项零失败/跳过，精确覆盖率
90.17053776940783%/90.16007532956685%，本地空间恢复后90.2542372881356%。
180次零供应商演练、660模拟请求及独立进程重入通过，已知十题和20题资格复验通过。
单次本地磁盘失败和WSL解释器身份变更中止均保留，未降低保护。

使用现有账户余额正式执行6/180次，52请求全部结算，无计数差异/身份变化/未知响应；
账户余额不足下一完整三臂块预留而停止，174次未执行。A/B/C主成功均1/2，不能证明提升。
E盘正式产物有storage-owner标记，仓库storage-locations索引方便后续归档和清理。
完整资源、状态、成本、时间、失败与统计限制见
[本轮报告](experiments/2026-10-06-abc-holdout-capability.md)。

## 2026-09-27：TraceFix Runtime 按需 Skills 与 MCP 评估

在容器资格与恢复 Goal G0–G7 全部验收后，接入 Agent Skills 的小型受控子集。新增 `tracefix-debugging`，并通过 `AgentConfig.skills_enabled`、CLI `--skills` / `TRACEFIX_SKILLS_ENABLED=1` 默认关闭；仅激活工具加载正文，references 单独按需读取。发现器限制在 TraceFix 随包技能目录，拒绝重名/格式错误、符号链接和越界引用；不运行技能脚本，也不消费 `allowed-tools` 扩权。激活轨迹包括名称、版本、SHA-256 和实际上下文正文；正文以 system 锚点保留，因此上下文折叠后仍存在，重复激活不再次注入。Docker bridge 仅在同一开关开启时添加工具，基线工具描述和 system 提示保持不变。

verification-before-completion 没有新增：当前 Agent 已有测试完成提醒、可选 `require_tested_completion`、run_tests 反馈与独立 Diff 证据要求；此前验证闭环正式比较未达到采纳条件，重复提示会增加上下文但无新验证能力。Serena 的符号查找、文件概览和引用查询能补充当前文本检索和保守 Repo Map 的符号引用关系，但没有在冻结容器源码视图完成可复现 MCP 调用、边界与会话清理验证，故本轮不接入 MCP SDK/Serena，不改 Docker bridge 协议。Context7 与 GitHub MCP 仍是未来候选，不带入冻结基准。

本轮针对性回归 91 passed；最终零费用全量 pytest 550 passed、0 failed、678.59 秒、0 warnings。Ruff、129 个跟踪 Python 文件 compileall、改动文件 compileall、`git diff --check` 均通过。Agent Skills `quick_validate.py` 通过。没有新测本次工作区的 Coverage.py 合并口径，G6 的旧覆盖率结果不代表本次版本；无模型调用、付费实验或留出题访问。原始 pytest 输出和退出码位于 `E:\TraceFixRunsActive\skills-mcp-integration-final-20260927`。执行 HEAD 为 `5258dc44e324626cc9ab5345d5f7b68cc5d47613`；当时工作区含环境 Goal 已有的大量 dirty/untracked 文件，本轮保留且未清理，相关新增文件与代码哈希、Skills/MCP 版本和限制见 [`experiments/2026-09-27-skills-mcp-integration.md`](experiments/2026-09-27-skills-mcp-integration.md)。

## 2026-09-27：容器资格与恢复 Goal 配置（未执行）

按用户要求建立一个总 Goal 的执行合同、八个子目标、依赖顺序、共同约束与逐节点提示词；计划和机器状态位于 [docs/goals](goals/2026-09-27-container-qualification.md)。P0 先复用正式 Harness 完成 pytest-10081 的 base/gold/保存补丁 × 失败节点/整文件 × 两次矩阵并归因；P1 随后验证真实容器断连、中断与恢复分类并修复；P2 完成准确工程门槛与审计交接。子目标依赖验收通过才自动推进，准备失败和未知调用不得记成功。

本轮只新增计划 MD/JSON 并更新路线图与本记录，未改运行代码、镜像、旧实验、账本或评分，未运行 Docker、pytest 或供应商调用。文档与状态文件完成 JSON 解析、八节点唯一性/依赖顺序/提示词完整性检查及指定文档 diff 检查；这不是工程测试通过证据。计划时 HEAD 为 5258dc44e324626cc9ab5345d5f7b68cc5d47613，已有大量脏改动，真正执行身份待 G0 快照固定。保留 501 passed 但覆盖率 83.462867% 未达 90% 的历史结论；pytest 整文件归因和真实恢复仍未完成。按用户先切换模型的要求，Goal 配置后等待恢复，从 G0 开始；无新增实验结果可报告。

## 2026-09-26：历史 run 目录迁到 E 盘

将四份早期 P2 模拟目录复制至 `E:\TraceFixRunsArchive`，以并发校验脚本逐文件比较路径、大小和 SHA-256：464,618 个文件全部一致。D 盘旧路径改为 NTFS junction 指向 E，四份 D 盘重复副本移除；旧绝对路径仍可读取汇总、协议和 Git checkout。D 可用空间从约 14.4 GiB 增至约 29.4 GiB。Docker 数据仍占 D 盘约 4.37 GiB，TraceFix 其他默认 `runs/` 输出仍可能落回 D；后续新运行应显式指定 E 上目录。校验报告与空间记录见 [`maintenance/disk-space-plan-2026-09-26.md`](maintenance/disk-space-plan-2026-09-26.md)及`runs/docker-foundation-20260926-v1/space-migration-final.json`。

## 2026-09-26：Docker 基础迁移与计数器离线核验

增加显式历史 Windows 路径映射与越界拒绝，独立镜像安装官方 `deepseek-recipe==0.1.1`，断网比较 755 个完整请求和供应商 usage；逐项计数相同，但只授予 `estimate`，新正式调用仍在请求前停机。增加通用 staging、Git bundle 恢复和 Docker 严格复验入口；pytest-10081、Requests-1766、Sphinx-10449 的 base/gold/保存补丁各两次结果一致。Linux checkout 对旧 CRLF 补丁的应用已修正。未改原始评分和账本，未发送模型请求。定向回归 13 passed、全量 pytest 489 passed；全项目覆盖率 85.9106216437608%，低于 90% 门槛，故覆盖率命令退出码 1；Ruff、86 文件编译与 diff 检查通过。固定执行身份、报告、失败试运行、验证和限制见 [`experiments/2026-09-26-docker-foundation.md`](experiments/2026-09-26-docker-foundation.md)。下一步是容器 Agent 接口与原目录不可访问条件下的入口验证，不自动迁移所有题目。副本候选见 [`maintenance/docker-migration-cleanup-candidates.md`](maintenance/docker-migration-cleanup-candidates.md)。

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

## V0.8.0：真实候选池与离线结构筛选

新增固定 revision 的 SWE-bench Verified 候选收集器，按四个 Python 仓库各 3 题生成脱敏结构清单、
问题/补丁工件和排除原因；新增 `collect-real-candidates` 与 `screen-real-candidates` CLI。结构筛选
复用 Repo Map 离线指标，不调用 LLM，并明确将任务获取、源码行为验证和后续付费预筛选分为独立阶段。
实现与验证记录见 [`tasks/v0.8.0-real-candidate-pool.md`](tasks/v0.8.0-real-candidate-pool.md)。

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

## V0.8.0：候选规则可行性修正与首批 12 题工件

首次从本机缓存的 SWE-bench Verified 固定 revision 读取 500 条实例时，原始“每题至少修改
两个非测试 Python 文件”的规则导致 pytest 只有 2 题、Requests 为 0 题，无法满足四仓库各
3 题的预注册配额。这是数据分布与硬筛选条件不兼容，不是下载、磁盘或 API 故障。

因此候选入口改为默认“至少一个非测试 Python 文件 + 非空 test patch”；多文件复杂度交给
后续源码检出后的图结构筛选，而不在收集阶段淘汰有效真实 Issue。此次生成并提交 12 道
候选工件：pytest、Pylint、Sphinx、Requests 各 3 题。每题均含原始问题、gold/test patch、
规范化 SHA-256 和可加载的 `task.json`。为兼容官方混合 CRLF/LF 文本，收集器在写入前统一
换行符；并将 gold patch 中的配置文件一并登记，避免工件校验遗漏 `setup.cfg` 等真实修改。

验证结果：12/12 `RealIssueTask` 清单加载及工件哈希校验通过；候选池、真实任务清单和 CLI
回归测试 `39 passed`，Ruff 与 compileall 通过。未调用 LLM、未产生 API 费用。候选尚未代表
可运行的 Agent 基准；下一阶段仍需固定 commit 检出、创建兼容 pytest 环境，并验证 base 失败
与 gold patch 通过。

## V0.8.1：真实任务环境与可信行为验收

新增逐任务虚拟环境准备器 `prepare-real-environments`：环境使用显式解释器和清华 PyPI 镜像，
非 editable 安装固定源码依赖，并以任务 ID、固定 commit、镜像和依赖指纹建立健康复用标记。
环境、安装命令及有限日志全部进入忽略目录 `runs/`，而脱敏汇总进入版本库。

真实行为验收改为在 base/gold 两个独立 clone 中运行同一官方 pytest 选择器并写 JUnit。只有
base 出现实际断言失败、gold 在同一入口通过，才可进入后续 LLM 预筛选；依赖错误、收集错误、
零测试、超时和执行异常均单独记录，不能被误算为 Bug 复现。

首批五题在现有 Python 3.12.5 下均有明确的不合格证据（0/5）：历史 Pylint 依赖与 Python
3.12 不兼容，pytest 缺少构建期版本文件，Sphinx 的官方用例 base 为 xfail 而非失败。未调用
LLM、未产生 API 费用，也没有开始 Token 对照实验。详见
[`tasks/v0.8.1-real-environments-and-behavior.md`](tasks/v0.8.1-real-environments-and-behavior.md)
与 [`experiments/v0.8.1-real-environment-behavior.md`](experiments/v0.8.1-real-environment-behavior.md)。

## V0.8.2：节省空间的配方环境与严格资格审查

V0.8.2 将真实任务环境从“每题显式解释器”升级为任务级配方：TraceFix 自动盘点本机 Launcher、
Conda 与已登记解释器，优先复用兼容 Python；环境身份纳入固定 commit、配方哈希、解释器版本和
依赖指纹。新增空间盘点、10 GiB 创建门槛及带标记目录的预览式安全清理，避免无限下载 Python
或误删用户 Conda 环境。所有 Python 包仍经清华 PyPI 镜像安装。

验收器加入 collect-only 选择器核验、构建诊断、JUnit/退出码交叉证据和源码导入探针；base/gold
必须从各自独立副本加载实现。离线审查 12 道候选后仅 2/12 合格，未达到真实付费 C/T 实验至少
三题的门槛，故未调用 LLM、未产生费用。完整实现和实验边界见
[`tasks/v0.8.2-space-aware-environments.md`](tasks/v0.8.2-space-aware-environments.md) 与
[`experiments/v0.8.2-real-environment-validation.md`](experiments/v0.8.2-real-environment-validation.md)。

## V0.8.3：可审计验收与 Python 版本修正

V0.8.3 为真实任务的构建、pytest 收集与执行分别保存 argv、工作目录、退出码、时长和完整日志。
轻量 pytest 审计插件记录 node ID 与阶段结果；JUnit、源码导入探针和实际执行集合共同用于资格判断。
失败被区分为权限、网络、依赖、收集、报告、skip/xfail 与业务断言，避免把环境噪声宣传为修复能力。

重验发现旧 pytest 候选在 Python 3.12 上的 `ast.Str` 弃用异常属于版本兼容问题，改用本机 Python
3.11 后三题都通过 base/gold 闭环；Sphinx 10449 也经逐题审查后在 Windows 合格。最终 12 题中
7 题合格，达到下一阶段预注册 C/T 实验的最低任务数。未调用 LLM，详细结果见
[`experiments/v0.8.3-real-environment-validation.md`](experiments/v0.8.3-real-environment-validation.md)。

## V0.8.4：严格 node ID 与历史环境配方

V0.8.4 删除了“缺少执行 node ID 时按 JUnit 测试数量继续判断”的宽松回退；现在报告、审计或
实际执行集合缺失都会 fail closed。验收器保留完整相对 node ID、参数化标识、导入路径与收集异常
特征，并让 pytest 子进程隔离继承参数而保留被测仓库自己的插件配置。

新增受审查 collection ImportError 配方类型，Pylint-4551/4604 因 test patch 导入 gold 新增 API
而在 base 收集失败的情形被单独计数，绝不放行其他收集错误。环境创建、ensurepip 与 pip 改用
TraceFix 管理的临时目录；健康 marker 验证实际依赖指纹。Sphinx-10435 经依赖审查改为 Windows
可验证，pytest 三题固定兼容运行器和隔离版本文件生成。

最新离线重验形成 10/12 可资格化任务：8 道正常断言失败/gold 通过，2 道受审查收集失败/gold
通过；Requests-1724 在 Python 3.9 未复现原 Python 2.7 行为，Sphinx-10323 仍因上游 Windows
xfail 待 Linux。未调用 LLM、未产生模型费用。详见
[`tasks/v0.8.4-strict-real-validation.md`](tasks/v0.8.4-strict-real-validation.md) 与
[`experiments/v0.8.4-real-environment-validation.md`](experiments/v0.8.4-real-environment-validation.md)。

## P0 后续：环境所有权与验收报告闭合

在 `d491c91` 基线上，环境准备器在创建 venv 前写入任务所有权标记；不再因健康标记缺失或依赖
漂移递归删除旧环境，而是在新的受管理目录重建。清理和空间统计只接受管理根目录内、无链接跳转且
所有权匹配的环境。pytest 审计现记录格式版本、运行 ID、阶段、完成状态、退出码、收集异常和实际
源码导入路径；资格判断要求审计与对应 pytest 阶段及退出码一致。node ID 仅移除明确的 checkout
绝对前缀，保留参数化标识和目录，选择器不再按任意路径后缀匹配。

定向回归在提升权限后的独立 pytest 临时目录中为 `20 passed`；Ruff 与 `compileall` 通过。全量
分支覆盖率运行有明确终态：`210 passed`，但总覆盖率为 `87.59%`，低于配置的 `90%`，因此 pytest
以覆盖率门槛失败退出。普通权限下 Windows pytest 临时目录出现 `WinError 5`；同一命令经受控提升
权限后可完成，故不将其表述为测试挂起或业务失败。本轮未运行 12 题正式复核、未调用模型、未推送、
未清理旧环境或实验目录。P1 已只读盘点现有解释器环境、固定源码和 12 份配方；Sphinx-10449 的
实际依赖仍待独立复核。后续先补足有意义的覆盖率测试并重新达到门槛，再创建新的逐题复核证据目录。

### P0 续修：路径与审计边界复核

本轮复核发现上一版路径保护仍会按纯词法接受 `root/../outside`，空的已完成审计也会被视为完整，
且 node ID 规范化会改写参数化标识中的反斜杠。现已拒绝原始路径中的 `..`、符号链接和 Windows
junction；环境所有权标记绑定管理根和解析后的环境目录。node ID 只规范化 `::` 之前的文件路径，
参数文本保持原样；执行审计必须包含收集节点及每个节点的 setup/call/teardown 报告，普通资格也要求
base/gold 的收集审计完整。新增目录逃逸、链接和参数保留回归测试。

验证：定向 `tests/test_real_experiment.py tests/test_real_environment.py` 为 `21 passed`；Ruff 与
compileall 通过。全量测试在受控临时目录明确结束为 `211 passed`，分支覆盖率 `87.58%`，仍低于
90% 门槛，因此最终退出码为 1。P0 尚未完成；未运行 P1 的 12 题正式复核，未调用模型、未删除旧
现场、未提交或推送。下一步以 CLI、环境和验收异常路径的行为测试提高覆盖率，并补齐实际 pytest
导入路径及混合 collection 异常的资格检查。

### V0.8.5 P0 完成：可信验收底座

在前述续修基础上，补齐了健康重建环境解析、CLI 共用环境选择、实际 pytest 进程源码审计、
收集/执行双审计及失败分类回归。最终全量测试明确结束为 `239 passed`、退出码 0；精确综合
覆盖率 `90.052614%`，其中语句覆盖率 `92.402165%`、分支覆盖率 `81.220657%`。Ruff、
compileall 和 diff 检查均通过。

普通沙箱中的临时 Git/虚拟环境操作出现可复现 `WinError 5`，仅对具体 pytest 命令受控提升权限
后完成验证；旧临时目录未清理。代码仍基于 `d491c91` 的未提交工作区，未推送、未调用模型，也
未开始 12 题复核。实现、文件哈希、最终证据路径和限制见
[`tasks/v0.8.5-p0-trusted-validation.md`](tasks/v0.8.5-p0-trusted-validation.md)。

### V0.8.5 P1：新环境与 12 题统一复核

在提交 `71da107` 的固定代码快照下，重新准备 11 个受管理任务环境：Requests 使用 Python 3.9.21，
Pylint 使用 3.10.20，pytest 与 Sphinx 使用 3.11.16。pytest 历史源码依赖 setuptools-scm 版本文件，
因此将配方固定为 `setuptools-scm==6.4.2`，并让环境构建副本通过只读 gitdir 引用固定源码 Git 元数据；
失败副本和原始环境均保留。Sphinx-10323 依配方在 Windows 标为待平台验证。

12 题的 base/gold 复核已结束：8 道常规断言失败资格、2 道受审查收集失败资格、Requests-1724
在 Python 3.9.21 未复现、Sphinx-10323 待平台验证。每题保存收集与执行审计、JUnit、日志、实际导入
路径和三次依赖指纹；未调用模型或产生费用。完整分类、配方边界与复现命令见
[`experiments/v0.8.5-p1-real-environment-validation.md`](experiments/v0.8.5-p1-real-environment-validation.md)。

### V0.8.6 P2：整体优化对照协议

P2 将 P1 的 10 道行为合格任务固定为 60 次 C/T 运行：三轮 C/T、T/C、C/T，C 同时关闭行动优化、Repo Map
与上下文压缩，T 同时开启三者。新增协议与零费用 CLI 会锁定任务、补丁、配方和代码身份；正式调用缺少
模型、供应商、价格来源或总费用上限时拒绝启动。独立验收增加 Agent 补丁专用的严格入口，复用 P1 的审计、
JUnit、源码和依赖漂移规则。零费用演练未初始化供应商客户端。详见
[`experiments/v0.8.6-p2-whole-system-protocol.md`](experiments/v0.8.6-p2-whole-system-protocol.md)。

P2 收尾接通确定性模拟模型、正常 Agent 工具循环、严格独立验收和完成态恢复。10 题共 60 项全部执行，
恢复重跑复用 60/60；模拟补丁不含任务答案且独立验收均按非修复处理。最终全量为 253 passed，覆盖率 90.15%。

后续审计保留早期脏源码失败尝试，并在隔离源码副本完成新的 60/60 演练；恢复复用 60/60，费用为零。
新增 P2 输入冻结检查、运行锁、未跟踪测试篡改拒绝和基础设施错误分类。最终为 257 passed、分支覆盖率
90.05%，证据在 `runs/p2-evidence-20260918/`；未调用供应商。

最终审计发现早期模拟模型的新增文件补丁未被工具接受，随后又发现未跟踪文件 diff 在 Windows 使用绝对路径。
修复两处后，以提交 `91f0e0b` 完成新的 60 次演练：补丁应用 60/60、验证 JSON 60/60、源码审计 60/60、
依赖漂移 0，恢复复用 60/60 且没有新增 Agent 运行。正式执行器接入持久化费用预留、实际 Token 核算、费用
帽和不确定请求冻结。最终全量 260 passed，精确综合覆盖率 90.0270351456894%；原始验证工件位于
`runs/p2-final-verification-20260918-v4/`。这些结果证明工程闭环，不是修复能力实验。

### 2026-09-18 P2 正式启动门槛：身份与恢复

P2 协议现在读取并哈希 P1 原始行为记录，逐题冻结资格类型、base commit、P1 环境依赖指纹及 gold
完整执行的 node ID 集合。正式/模拟运行前必须重新检查受管环境指纹；严格 Agent 验收要求执行集合与
冻结集合完全一致。另修复 collection-only 路径把 collection 审计误作 execution 审计的字段映射，并避免
不存在的 execution 审计遮蔽有效 collection 审计。

重新执行 Pylint-4551 与 4604 的 base/gold 验收：两题仍为配方精确声明的 collection ImportError，gold
均通过，源码审计有效、依赖无漂移，新的 collection audit diagnostic 均为空。证据在
`runs/p2-gate-p1-collection-audit-v2-20260918/`。新的 `p2-check` 已在 10 题 clean source 与 P1 管理环境
上通过，路径为 `runs/p2-gate-input-check-20260918/p2-check/p2-input-check.json`。

费用账本改为逐请求持久化预留/结算状态；缺失或越界 Token、费用超过预留、未知请求及费用帽不足都会冻结
后续请求。试次在 Agent 结束后先持久化 `agent_completed`，恢复只续做严格验收，不重发 Agent 请求。定向
P2 回归、Ruff 与 compileall 已执行；尚未调用供应商。下一步是补齐任务级 C/T 汇总和正式模式的模拟供应商
端到端门槛，之后才可填写商业参数启动付费对照。

本次完整回归为 `263 passed`、JUnit 0 failure/error；在全量数据上补入 P2 失败关闭分支后，综合覆盖率
`90.02%`（语句 `92.23%`、分支 `81.38%`）。JUnit 在 `runs/p2-gate-coverage-v2-20260918.junit.xml`，
最终 coverage JSON 在 `runs/p2-gate-coverage-v4-20260918.json`。

### 2026-09-20 P2 工程最终交付

执行代码 `f27f95b` 在 `runs/p2-agent-simulation-final5-20260920/` 完成 60/60 次模拟严格验收，恢复运行复用 60/60。最终全量 `265 passed`；汇总输出与工件篡改负例分别追加验证后，综合覆盖率 `90.00143864192202%`（语句 `92.26309113969923%`、分支 `81.28491620111731%`）。Ruff 与 compileall 通过，证据位于 `runs/p2-final-verification-20260920/`。模拟结果不代表修复能力。

### 2026-09-21 P2 正式恢复与对账

首次正式批次在 4 项完成后，于 Pylint-4551 控制组的第 17 个请求停止。旧实现只缩小费用预留，
未在调用前按实际序列化请求校验剩余输入 Token，也未把剩余输出额度传给供应商；响应后超限检查还会
混淆“响应已返回”和“请求未知”。旧人民币金额同时被写入 `*_usd` 兼容字段。

恢复实现让 LiteLLM 调用和 Token 计数共用请求参数，在调用前拒绝超出试次余额的请求，动态下传剩余
输出上限，并在响应校验前原子保存模型、用量和响应哈希。新账本显式记录币种、协议和价格身份，同时
保留旧字段读取兼容。新增只读 `p2-reconcile`，另写哈希索引和更正记录，不改写旧账本。

旧批次仍有一个请求无法从本地证明是否返回：已计算人民币 1.427328 元，未结算预留 0.086948 元。
付费执行保持冻结；旧前 4 项不并入修正版 60 项主要分析。

最终工程验证为 `271 passed in 351.97s`，综合覆盖率 `90.00559910414334%`，其中语句覆盖率
`92.34567901234568%`、分支覆盖率 `81.00407055630936%`；Ruff、compileall 和 diff 检查通过。
JUnit、coverage JSON/XML 位于 `runs/p2-recovery-verification-20260921/`。

### 2026-09-21 P2 账单闭合与重跑门槛

`p2-reconcile` 新增供应商 CSV 筛选和哈希记录。TraceFix 历史账单合计人民币 3.24553320 元；9 月 20 日
67 次请求的日汇总与本地 66 次已结算请求相减，得到最后一次输入 29,721、输出 544 Token。旧批次保留
为工程缺陷证据，最后一次请求不重发，旧四项不并入新批次。

执行版本 `e464f9b` 使用包含工具定义的 UTF-8 序列化字节上界、人民币独立记账和跨目录共享账本。
真实探测返回 `deepseek-flash`，usage 与缓存明细完整。全量为 276 passed，综合覆盖率
90.06228373702422%（语句 92.34257805686377%、分支 81.30026809651474%）；Ruff 与 compileall 通过。
60 项运行因外部数据传输自动审批暂停，等待用户明确授权把任务、源码片段和工具输出发送到 DeepSeek。

### 2026-09-21 P2 正式重跑完成

获得明确外发授权后，新批次完成 60/60 项且无基础设施失败；恢复检查复用 60/60，模型请求和 Agent
目录均未增加。C 独立验收成功 8/30，T 成功 7/30。T 输入 -2.73%、输出 +10.32%、Agent 用时
-6.14%、保守费用 -2.08%，成功率差值 -3.33pp。两道收集失败资格任务两组均未修复。
原始证据保留在本机，脱敏 JSON 和中文报告已提交。结果仅是 10 道任务的探索性证据。

### 2026-09-21 P2 正式批次离线诊断与可靠性修复

新增只读 `p2-diagnose`，从原始试次、运行结果、补丁和验收 JSON 重建 60 项分类，不发起供应商
请求。诊断确认 17 次正常输入预算停止、1 次单请求上界停止和 1 次 Windows 共享账本替换事故；
此前“基础设施失败 0”的结论已在实验报告、路线和交接中更正。32 个测试修改拒绝细分为 28 个
源码加测试、3 个仅测试、1 个含配置。T 组 Repo Map 30/30，工具结果精简 662 次，但两组上下文
折叠均为 0，现有 Token 差异不能归因于压缩。

预算耗尽新增 `p2_trial_budget_exhausted` 终止代码；共享账本使用唯一临时文件并对 Windows 短暂
占用有限重试；P2 Agent 提示明确禁止提交测试和 pytest 配置，以匹配既有严格验收。原始 60 项结果
不改写、不追溯改判。

### 2026-09-21 P2 证据与评分契约统一

`p2-summarize` 与 `p2-diagnose` 共用版本 2 证据审计，逐项核对计划身份及运行、补丁、验收工件
哈希。运行终止、独立验收和基础设施事故分开统计；缺失验收不再默认为通过。评分契约规则集中到
严格验收模块，新协议冻结契约版本和提示哈希；预算耗尽作为正常 Agent 中断处理。

既有 60 项证据全部通过身份核验。轨迹确认 T 组 662 条工具结果实际缩短、Repo Map 候选读取 52 个，
但两组上下文折叠均为 0。旧请求提示与严格评分并不完全一致，pytest-10356 还明确要求测试修改；
因此旧分数保留，仅将该问题记录为协议缺陷。已生成只读消融和留出集方案，本地留出候选缺口为 20。

最终全量验证为 `283 passed in 540.35s`，综合覆盖率 `90.30065359477125%`（语句
`92.50082590023125%`、分支 `81.95488721804512%`）；Ruff 与 compileall 通过。证据位于
`runs/p2-evidence-contract-verification-20260921/`。首次全量虽 282 项全部通过，但覆盖率只有
89.12%，已如实保留为失败记录；补充实际轨迹与完整工件行为测试后才达到门槛。

### 2026-09-21 留出候选冻结与长上下文机制验证

新增固定种子、排除和同仓库重复检查的留出采集模式，以及行为证据冻结和长上下文确定性回放入口。
SWE-bench Verified 固定在 `78f471bf655a3137b2e8a75af1501690ec009ec3`，69 道结构候选的完整
顺序已保存。首轮严格验收得到 4/20 普通资格，缺口 16，未放宽资格规则。两道长上下文机制题均在
32k 阈值触发折叠且 base/gold 闭环通过；它们不计入真实任务成功率。

最终全量验证为 286 passed，综合覆盖率 90.31804828202836%（语句 92.4987901274399%、分支
82.02453987730061%），Ruff 与 compileall 通过。

### 2026-09-22 留出集兼容重验与最终冻结

严格冻结器不再信任汇总布尔值，而是复核 base/gold 审计、JUnit、退出码、完整阶段、源码位置和依赖
指纹；缺失审计路径现在稳定拒绝，不再抛异常。逐题兼容配方解决历史 Python 与依赖问题，最终按原始
顺序冻结 20 道普通资格任务，四仓库各 5 道，并生成提交、目标 node ID、配方与依赖哈希锁。两道长
上下文 fixture 分别验证压缩触发、预声明关键事实保留和 base/gold 闭环，继续独立于真实留出集。

最终验证为 289 passed，综合覆盖率 90.14328063241106%（语句 92.3028589381087%、分支
82.19653179190752%），Ruff 与 compileall 通过。首次全量的 89.7452% 覆盖率失败保留在同一证据目录，
没有被冒充为通过。证据位于 `runs/holdout-final-verification-20260922/`。

### 2026-09-22 四组消融协议与零费用预检（V0.8.10）

从 `f352e4b` 继续，保留旧默认两组协议，增加四组开发集 120 项确定性排程、有效配置和任务等权汇总。
完善冻结器的 run_id/阶段/目标模块路径校验、全依赖版本锁及 Requests-1921 的可复现 httpbin 服务
配置。共享费用账本在启动和恢复时检查；实验锁覆盖整个更新过程，修复异常路径漏释放与错误释放风险。
证据审计 v3 独立保存 Agent 终止、验收无法判定和基础设施事故，拒绝用残缺验收作为普通失败。

首批真实任务模拟发现 pytest-10081 缺失阶段证据，追溯到测试配置选择不同。配方显式指定
`pyproject.toml` 后 base/gold 重验通过；另两道 pytest 配置差异也补验通过。最终真实题模拟 120/120
有效（96 断言失败、24 受审查收集失败），全部完成态恢复复用。正式入口合成模拟 120/120 验收通过、
240 次虚拟请求，恢复新增请求为零，预算上限跨目录拦截通过。没有新增真实供应商调用。

最终代码全量 **335 passed**，综合 **90.98527475158626%**、语句 **93.05066585029849%**、分支
**83.57142857142857%**；pytest、Ruff、compileall 均退出 0，源码/测试/脚本验证前后哈希不变。
原始证据与失败尝试全部保留，完整索引见
[`experiments/v0.8.10-ablation-preflight.md`](experiments/v0.8.10-ablation-preflight.md)。
余额 ¥70.70445880 小于开发集固定 Token 预算的 ¥103.20 保守费用上界；下一阶段先解决阶段预算，
再付费运行消融，不默认追加原 ¥100 上限，不提前评测留出集。模拟结果不构成模块收益证据。

### 2026-09-22 开发集正式消融前的费用停止修复（V0.8.11）

总费用耗尽现以 `campaign_budget_exhausted` 正常终止，先验收已有补丁，再停止排程；已保存结果可
无调用恢复，真正未知请求仍阻断。报告保留全部计划位置并分开预算未执行与证据损坏。增加官方端点、
原始模型身份与完整缓存用量检查，并在每四组开始前复核冻结输入。优化算法、提示和评分规则未改变。
最终全量 391 passed、综合覆盖率 91.00807867931155%，Ruff/compileall 均通过；旧 CLI 模拟对象
缺字段导致的首轮失败保留。用户选择原 ¥100 总上限内运行固定 120 项，未完成时不选择配置。
详见 [`experiments/v0.8.11-development-ablation.md`](experiments/v0.8.11-development-ablation.md)。

### 2026-09-22 开发集四组正式消融完成与无调用恢复（V0.8.11）

在执行提交 `a8309eb5a20aab7825c797ee8467eacf8c42c3b0` 上完成固定 120 项，运行 6493.297 秒、
进程退出 0。120 项全部证据有效：48 成功、72 修复失败、基础设施及验收证据异常均为零。新增 2094
条响应逐条核对模型别名、原始 Token/cache、工件哈希与费用；本轮保守计算 ¥51.073224，共享累计
¥80.3687652，未重置原 ¥100 上限。自动恢复及带客户端失败守卫的独立恢复均复用 120 项，新调用零，
账本、试次、协议与 Agent 目录哈希/清单不变。

主要 8 题普通资格每组三次：基线 13/24、Repo Map 10/24、行动 11/24、上下文 14/24；2 道收集失败
资格每组均 0/6，分开报告。行动输入 -35.77%，但成功率 -8.33 个百分点、Agent 用时 +0.50%，无组
满足冻结选择规则，保留基线。离线脚本改用整数成功数比较，防止浮点 1/3 边界误排持平，不改变规则。
上下文工具裁剪 18/30 项、历史折叠 0，补充裁剪前/后峰值与轨迹哈希，纠正“整个模块未触发”的误读。

执行后仅补脱敏 JSON 和文档；69 个运行源码、测试、脚本与最终工程检查快照一致，继续对应 391 passed、
综合覆盖率 91.00807867931155%、语句 93.03407896712206%、分支 83.82978723404256%，三个检查退出 0。
原始证据 `runs/ablation-formal-development-20260922/`；报告见
[`experiments/v0.8.11-development-ablation.md`](experiments/v0.8.11-development-ablation.md)。
下一步仅优先规划既有轨迹的配对失败诊断；未启动留出集、未修改优化算法或追加费用上限。

### 2026-09-23 P2 开发集离线详细诊断

在代码起点 `50312e8` 上增加 `p2-diagnose --detailed-ablation`，旧诊断入口保持兼容。详细入口复用 P2 共享证据审计，额外核对 120 条冻结轨迹哈希、2,094 条供应商响应与用量、480 个试次/运行/补丁/验收工件哈希及共享账本身份；分析前后原实验和账本未改变，付费请求和供应商客户端均为零。正式运行身份为 `a8309eb5a20aab7825c797ee8467eacf8c42c3b0`。脱敏报告位于 `benchmarks/experiments/v0.8.11-development-ablation/detailed-diagnostic-20260923-final-v4/`；完整原始输入仍留在本机。

普通 24 对基线/行动优化结果为 5 退步、3 反向、16 通过状态相同（8 对双方通过、8 对双方未通过，失败细类不全相同）；特殊资格 6 对单独保留且双方均未通过。全部 120 项历史分类仍为 48 通过、24 空补丁、35 执行失败、12 收集失败、1 测试/配置修改拒绝。行动优化组观察到 603 条工具输出缩短；上下文组初版记为 417 次工具裁剪与 253 次历史折叠；V0.8.13 按事件字段更正为 417 次裁剪操作、253 个裁剪事件、0 次历史折叠，Repo Map 在 519 个模型请求视图中出现并读取 55 个候选文件。字符数和本地估算不代表供应商 Token 节省。

当前轨迹未证明可复现实现缺陷。改进决策为不改优化实现；仅提出开发集单变量假设：单独启用工具输出呈现缩短，关闭行动引导和读取缓存，检验输入变化及诊断标记保留。配置、题目、提示、评分和预算固定，预先声明指标；20 题冻结留出集继续不触碰。

验证：全量 pytest `401 passed`。Coverage.py 合并全量测试及对冻结真实轨迹执行一次离线详细诊断的覆盖数据后为综合 `90.22915340547422%`，语句 `92.5399889685604%`、分支 `82.52069917203312%`。Ruff 和 compileall 均退出 0。全量测试单独运行曾得到 `86.60%`，未达到门槛；随后真实离线诊断覆盖路径并重新跑全量后，才得到合并覆盖门槛结果。首轮临时 Git 元数据访问失败和覆盖率不足记录保留在本机，不伪称通过。详见 [`../experiments/v0.8.12-offline-detailed-ablation-diagnostic.md`](../experiments/v0.8.12-offline-detailed-ablation-diagnostic.md)。


### 2026-09-24 V0.8.13 更正诊断并冻结单变量预检

按 `context_compacted` 事件内的 `tool_results_pruned`、`messages_compacted` 与 `batches_compacted` 独立分类，重核了 120/120 条既有轨迹。上下文组为 417 次工具裁剪操作、253 个含裁剪事件、0 次历史折叠；原分类与评分不变，账本和原始实验未改，付费请求及供应商客户端均为零。更正 JSON、配对记录、证据哈希见 [`experiments/v0.8.13-presentation-only-preflight.md`](experiments/v0.8.13-presentation-only-preflight.md)。

将输出呈现、行动引导和读取缓存拆为独立开关，同时保留旧 `token_optimization_enabled` 的兼容行为。冻结提交 `8954d77860aa17dfdc579c40ed14f50ef7fb2021` 上的 10 题、60 位置协议：每题三次，依次 C/T、T/C、C/T；C/T 的有效配置仅在 `tool_result_presentation_enabled` 上不同。8 道普通题为主要集合，2 道收集失败资格题单列；两组提示、评分和预算固定。20 题留出集未用于 Agent 评测。

离线合成 Agent→工具→独立 pytest 验收演练执行 6 个 fixture 位置，6/6 通过，完成态恢复复用全部 6 项；预设路径、断言、异常类型及完整 node ID 在模型可见工具结果中保留，长输出裁剪可观察，真实供应商客户端守卫未触发。单任务、预置补丁的成功仅证明工程链路，不构成模型修复能力结论；全量 60 个真实任务试次未运行。共享账本余额 ¥19.63123480（保守计算余额，不是供应商账单）；完整 60 位置的冻结价格保守上界为 ¥51.60，缺口 ¥31.96876520，因此不启动半批付费运行。

最终 pytest `414 passed`、退出码 0、594.42 秒；覆盖率合并全量 pytest 与离线诊断入口后综合 `90.08471917163476%`，语句 `92.3955924364032%`、分支 `82.39819004524887%`。Ruff 与 compileall 退出码均为 0。原始日志、JUnit、coverage JSON/XML 及专用合成试次保存在本机 `runs/v0.8.13-final-verification-20260924-v3/` 和 `runs/presentation-only-simulation-evidence-20260924/`。本轮不启动付费运行或留出集评测；待重新核价并保证完整日程预算后，再决定开发集单变量实验。

### 2026-09-24 V0.8.14 工具输出呈现单变量开发集正式比较

按用户确认将共享累计 CNY 上限调整为 ¥150，并设本轮阶段上限 ¥55；核对 DeepSeek 官方高峰价格、冻结 P1 证据、任务/配方/源码身份和健康环境后，基于执行版本 `e5ab04b36c25c4d1c724b932db7e397cbd143c4b` 顺序执行 10 道开发任务、两组各三次，共 60 个计划位置。配置、提示、评分、工具和预算固定；T 仅开启工具输出呈现。未运行 20 题留出集。

60/60 试次记录完成，独立验收通过 24 次；8 道普通题 C/T 均为 12/24，2 道收集失败资格题均为 0/6。普通题 T 输入 Token 低 510,938（10.51%），输出低 3,952（5.37%），但 Agent 时间高 237.297 秒（18.83%）；先按任务汇总三次后，T−C 每题等权输入差为 −21,289.083、Agent 时间差为 +9.887 秒。工具呈现记录了 763 个缩短视图，字符差 −597,536；不把字符量换算为供应商 Token 节省。

详细审计确认 58/60 位置证据有效；第 11 项缺少完整执行阶段审计，第 51 项 pytest 执行失败。使用原补丁在新隔离目录重验仍未解决，原工件保留。全试次分类为 24 通过、10 空补丁、5 收集失败、18 pytest 执行失败、1 测试/配置修改拒绝、2 验收证据问题。无未知供应商请求；账本 4,345 个请求已结算、余额预留为零。阶段保守计算 ¥26.177326，共享累计 ¥106.54609120/¥150；这不是实际供应商账单，缓存命中明细也未返回。完成态恢复复用 60/60。

依照预注册规则，工具输出呈现配置不采纳：普通题成功率与基线持平，输入 Token 降低但 Agent 时间上升，且未满足全部位置证据有效条件；继续保留全关闭基线，不启动留出集。机器结果与哈希见 `benchmarks/experiments/v0.8.14-presentation-only-paid-development/`，实验解释见 `docs/experiments/v0.8.14-presentation-only-paid-development.md`。本轮没有更改运行代码；该版本运行前全量 pytest `415 passed`，综合覆盖率 `90.050010%`（语句 `92.380694%`、分支 `82.313231%`），Ruff 与 compileall 均退出码 0。

### 2026-09-24 Agent 测试反馈与验证闭环

在 V0.8.14 的 12/24 普通题同期基线后，修复 Agent 与独立验收构建不一致：冻结配方现在随运行配置进入 Agent checkout，逐条记录构建命令、退出码、构建产物摘要及导入探针；构建失败或目标源码未从运行 checkout 导入时，在模型构造前停止。`run_tests` 的通过结果现在依赖非空、真实通过的测试集合及匹配的结构化审计和 JUnit；清理继承 pytest 参数，固定 root/config/conftest 边界。验证闭环组在未取得当前补丁的有效测试证据时不得直接以普通最终回复结束；记录 Agent 验证状态，补丁变化会清除该状态。新增 48 项固定 C/T 排程，只改变 `require_tested_completion` 行为；摘要分别核对全证据、零基础设施事故、T≥16/24、同期净增≥4 次和至少两题有净增。

零费用回归：全量 pytest 为 426 passed，0 测试失败；该覆盖运行首次以退出码 1 结束，因为单独全量测试覆盖率只有 86.46844660194175%，低于既定阈值。按既有覆盖口径，在同一 Coverage.py 数据追加既有冻结 120 项轨迹的离线详细诊断路径及最终边界回归后，综合覆盖率 90.0586569579288%，语句覆盖率 92.35635213494992%，分支覆盖率 82.47826086956522%；`coverage report --fail-under=90` 退出 0。该报告没有改变统计范围或覆盖阈值。最终定向套件 108 passed；另有 14 个边界用例 passed。Ruff 退出 0；对 Git 跟踪的 127 个 Python 文件执行 compileall API，127/127 成功。

正式比较尚未运行。DeepSeek 官方价目页在 2026-09-24 14:49:19 UTC 核对：高峰缓存命中输入 ¥0.04/M、未命中输入 ¥2/M、输出 ¥8/M。共享账本 4,345 笔均已结算，保守累计 ¥106.54609120/¥150，未决预留为零、未知请求为否，余额 ¥43.45390880。48 个计划位置的 Token 最坏费用为 ¥41.28，阶段上限拟设 ¥42，若达到 cap 将停止并保留未完成项；账本数据是保守计算值，不是供应商实际账单。官方模型名称为 `deepseek-flash`，文档说明对应 V4.1-Flash；别名不构成不可变版本证明。价格快照见 `benchmarks/experiments/v0.8.15-validation-closure-development/price-evidence.json`。

需继续完成干净执行版本提交和正式 `p2-check`，确认 10 题源代码、P1 资格、配方及环境均匹配后，才启动 48 项 C/T 同期实验。任何预检失败都应修复后重新核价和预检，不发送模型请求。报告、逐次结果和本机日志/覆盖率文件位置见 [`experiments/v0.8.15-validation-closure-preflight.md`](experiments/v0.8.15-validation-closure-preflight.md)。

执行验证更正：最终代码实际全量 pytest 为 443 passed、0 failed（754.97 秒）；pytest-only 覆盖率 86.64245% 因 fail-under=90 退出 1。按既定 Coverage.py 统计方案，在同一份全量测试数据上追加只读 V0.8.11 详细诊断的真实执行路径后，综合覆盖率 90.05246%，语句 92.36129%，分支 82.43712%，`coverage report --fail-under=90` 退出 0。Ruff 退出 0，Git 跟踪 Python 文件 127/127 compileall 成功。完整日志与工件保存于 `runs/agent-feedback-final4-full-20260924/`。提交前 `p2-check --design validation_closure` 检查通过，但显示工作区有跟踪改动；正式调用前须对提交后的干净版本重新执行。预检查与密钥配置只验证存在性，没有创建供应商客户端或发送请求。

### 2026-09-25 V0.8.15 正式开发集验证闭环比较完成

在干净执行提交 `d3a9a9e940d316822b6ff19e93384448474bfe69` 上重新通过 `p2-check` 后，按冻结协议完成 48 项正式运行：8 道普通开发题，C/T 各 24 次，三轮 C/T、T/C、C/T。DeepSeek 官方直连配置 `deepseek/deepseek-flash`，5105 次响应均报告 `deepseek-flash`；别名不保证不可变后端。模型关闭思考、temperature 0、自动重试 0。原始工件和账本留在本机 `runs/validation-closure-paid-development-20260924-v1/`。已修复汇总报告的正式/模拟标注，报告修复提交为 `7c9be64`，不影响执行代码或分数。

48/48 位置的工件证据有效，独立验收成功 27、失败 21，基础设施事故 0、未执行 0。主要指标 C 15/24（62.5%），T 12/24（50%），T−C −3 次；仅 pytest-10051 一题净增，其余任务净持平或下降。预注册要求 T≥16/24、净增≥4 且至少两题有净增，均未满足，故不采纳闭环配置，保留全关 C 基线。比较属于小样本探索性结果，不代表统计显著性，也不支持留出泛化结论。

21 次失败由 6 次空补丁和 15 次目标 pytest 失败构成。具体目标失败包括：Pylint-4661 的六次 `test_pylint_home` 路径断言失败；Pytest-10051 三次 `test_clear_for_call_stage` 阶段日志断言失败；Pytest-10356 五次 `test_mark_mro` 断言失败；Sphinx-10435 有一次 `test_latex_code_role` 断言失败。Pytest-10081 的三次 T 失败是空补丁。该结果表明补丁未符合目标断言或未产出补丁；不足以归结为单一模型/工程根因。

T 相较 C 多用输入 300,066 Token、输出 2,701 Token、Agent 时间 167.443 秒；任务等权平均差分别为输入 +37,508.25 Token、时间 +20.930375 秒，未改善成本/时长。终止原因包括正常预算耗尽 20 项、请求输入上界拦截 9 项、步数上限 1 项、正常完成 12 项和未验证即结束 6 项。请求上界拦截不是基础设施事故。批次保守费用 ¥20.329288，共享账本保守累计 ¥126.87537920/¥150、余额 ¥23.12462080，无未决预留或未知请求；这些金额不是供应商实际账单。恢复前后试次数 48、Agent 目录 48、请求数 5105 均未增加。

最终工程检查：443 passed、0 failed；pytest-only coverage 86.64245% 导致 pytest 命令按既有 fail-under=90 退出 1。追加同一统计数据上的只读离线详细诊断真实入口后，综合覆盖率 90.05246%（语句 92.36129%、分支 82.43712%），阈值检查退出 0。Ruff 退出 0；127/127 tracked Python 文件 compileall 退出 0。日志、JUnit、coverage JSON/XML 和退出码保存在 `runs/agent-feedback-final5-full-20260925/`。脱敏结果及原始摘要、协议哈希见 [`experiments/v0.8.15-validation-closure-results.md`](experiments/v0.8.15-validation-closure-results.md) 与 [`../benchmarks/experiments/v0.8.15-validation-closure-development/validation-closure-results.json`](../benchmarks/experiments/v0.8.15-validation-closure-development/validation-closure-results.json)。20 题留出集没有运行；下一步应对失败工件做离线逐例归因并设计零费用可证伪测试，不据此直接改提示或跑留出题。

### 28. 2026-09-25 验证反馈离线诊断

在 `a7b9dea` 之后增加只读 `p2-diagnose --validation-feedback` 和独立分析模块；校验 48 项原工件，记录全部状态、18 个重点试次和 C/T 配对索引。原执行代码为 `d3a9a9e940d316822b6ff19e93384448474bfe69`。Agent 已验证 6/48，17 次通过的 Agent 测试调用因去选测试仍在收集集合而被误报无效，此问题留待后续单独修复。运行代码本轮仅修补丁无效果却报告成功：应用后比较文件和 Git 状态，无变化返回 `no_effect`。新增 fixture 先失败再修复，覆盖正常修改、新增、删除、重命名及撤回。历史评分和旧轨迹不改。pytest-10081 的 T 三次空补丁各有不同形成链路，详见 [`experiments/v0.8.16-validation-feedback-diagnostic.md`](experiments/v0.8.16-validation-feedback-diagnostic.md)。本轮未调用供应商或运行留出集。全量检查和覆盖率以本条后续验证记录为准。

最终验证：定向 78 passed；全量 449 passed、0 failed、600.95 秒。pytest 单独覆盖率 85.93%，因 90% 门槛正确退出 1；同一最终代码的两个真实只读诊断入口合并后综合覆盖率 90.02418964683116%，语句 92.29501964263085%，分支 82.6923076923077%，独立门槛退出 0。Ruff 与 129/129 项目 Python compileall 均退出 0。日志、JUnit、覆盖率 JSON/XML 及各退出码在 `runs/validation-feedback-final-v3-20260925/`；中断的旧尝试单独保留，不拼接为最终通过证据。

### 29. 2026-09-25 Agent pytest 去选审计修正

从 `a998918` 出发，在独立 Git/pytest fixture 先复现 `-k` 去选后执行测试通过却被旧审计判作 `invalid_test_run`。将 Agent 审计升级为格式 2，同时记录完整收集和最终执行 node ID；阶段与 JUnit 按最终执行集合核对，保留空集合、skip、重复或额外报告及残缺阶段拒绝。`-k` 与 `--deselect` 回归均通过。

零费用模拟 Agent 使用真实补丁、测试和 Diff 工具：有效测试后无效果补丁保留已验证状态，真实修改使旧证据失效，撤回修改导出空源码 Diff；已有正式入口预算停止 fixture 验证保存的补丁仍经独立验收并且恢复不重发。离线重新检查冻结的 48 项轨迹和哈希后，17 次调用分布在 13 个试次，单次反馈应由无效改为通过。历史独立验收结果、原始账本、轨迹不回写。逐次脱敏更正见 [`experiments/v0.8.17-selection-feedback-correction.md`](experiments/v0.8.17-selection-feedback-correction.md)。

下一轮 48 项开发集同期 C/T 比较的任务、三次重复、顺序、共同工具与审计、主指标和采纳门槛已在机器草案中固定。共享计算余额 ¥23.12462080，低于历史价格计算的 ¥41.28 完整上界；阶段 ¥42 还差 ¥18.87537920。正式价格和账本将在实际启动前复核；本轮没有付费请求或留出集评测。

最终代码验证：定向 90 passed；全量 456 passed、0 failed，pytest 单独综合覆盖 86.021817%（语句 88.716003%、分支 77.343113%），因既有 90% 阈值退出 1。合并两个本轮只读离线诊断入口后，综合覆盖 90.105223%（语句 92.346616%、分支 82.885086%），门槛退出 0。Ruff 退出 0、129/129 个受跟踪 Python 文件 compileall 退出 0。完整日志、JUnit、覆盖率 JSON/XML、耗时、60 秒 faulthandler 设置和各命令真实退出码在 `runs/selection-feedback-final-v2-20260925/`；定向日志在 `runs/selection-feedback-targeted-final-20260925/`。限制是 pytest 单独覆盖不足 90%，工程门槛依照此前既定的合并统计范围达标。

### 30. 2026-09-25 验证闭环修正后的 48 项同期比较

正式执行版本 `97f521599f0602e79a50f782c4282001bbab6ed3`；账单更正沿用既有共享账本和 ¥150 总上限，TraceFix 两份账单重叠 ¥0.12438212 去重后历史账单 ¥41.74029308。8 道普通开发题、C/T 各三次共 48 项完成，阶段保守计算 ¥18.470972，共享账本累计 ¥60.21126508；不等于供应商账单。

C 独立验收 16/24，T 13/24；配对为双方成功 12、双方失败 7、C 独赢 4、T 独赢 1。T 仅在 pytest-10051 净增，pytest-10081 净减 2。19 个原始 `benchmark_error` 经结构化事件核对都是调用前单请求输入上界拒绝；不是外部基础设施事故。19 个独立验收失败的原因计数为 pytest 执行失败 13、空补丁 4、测试/pytest 配置修改拒绝 2。Agent 自报验证状态与独立验收分别记录。

恢复运行成功复用 48/48 位置，Agent 运行目录 48、请求计数 5,847、账本计算金额和未决状态均不变。48 项运行结果、补丁、独立验收工件哈希全部复核。T 输入减少约 6.0%、Agent 用时减少约 0.4%，但修复成功数下降；按预注册规则不采纳 T，继续保留 C。20 道留出题没有运行。本轮运行代码未改动，因此复用 `97f5215` 上已记录的 pytest、覆盖率、Ruff 和 compileall 结果，没有重复跑全量工程检查。报告：[`v0.8.18-validation-closure-comparison.md`](experiments/v0.8.18-validation-closure-comparison.md)；机器结果与哈希清单：[`report.json`](../benchmarks/experiments/v0.8.18-validation-closure-comparison/report.json)、[`evidence-manifest.json`](../benchmarks/experiments/v0.8.18-validation-closure-comparison/evidence-manifest.json)。

### 31. 2026-09-25 验证闭环空补丁与证据有效性复盘

只读核对执行提交 `97f5215` 的 48 项原轨迹和协议；C/T 配置仅闭环开关不同。pytest-10081 六项首次请求内容哈希相同，三组首次工具分歧在第 5/6/5 步。T 第 010、025 项各只有两种无效果补丁内容、分别尝试 12/10 次，后续请求可见 `no_effect` 和重复失败反馈；均未运行有效测试，最终空补丁。T 第 042 项从同一无效果补丁恢复为实际修改并通过独立验收。该题三次 T 都没有触发闭环结束提醒；全批 T 仅第 017、019 项触发。T 第 038 项四条不受支持的测试命令也计入六次测试上限，另两次 pytest 通过但没有补丁。观察关联不构成闭环因果结论。

复用共享审计时发现 V0.8.18 的手工哈希核对遗漏验收状态：第 019 项 `network_error`、第 039 项 `execution_error`，有效证据 46/48。新目录严格复验第 019 项超时、第 039 项重复执行错误；不能授予两项完整证据。原始固定分母仍为 C 16/24、T 13/24，即使唯一无效的 T 第 019 项变为通过，仍不能达到 16/24 采纳门槛。旧报告加注更正，原始评分不回写。离线 CLI `p2-diagnose --validation-feedback` 因两项无效证据拒绝生成全证据诊断，这是正确保护行为。共享账本哈希与请求数 5,847 在复盘前后不变；没有模型调用和运行代码修改，故未重跑工程检查。复盘说明见 [`v0.8.19-validation-closure-failure-review.md`](experiments/v0.8.19-validation-closure-failure-review.md)。

### 32. 2026-09-25 重复无效果补丁恢复门槛零费用预检

在独立 fixture 为重复无效果补丁增加可选恢复门槛：同一 `no_effect` 补丁再次提交后，Agent 要求重读目标路径；未读取即直接换补丁会被拒，成功重读后不同补丁可应用。默认配置保持关闭。脚本化 Agent 循环完成修复、pytest 和 Diff 验证，仅证明控制流可运行，不证明真实模型会恢复或提高成功率。最终定向回归 3 passed。全量 pytest 457 passed、0 failed、623.41 秒；pytest-only 综合覆盖率 86.073278%（语句 88.754717%、分支 77.463651%），故按原 fail-under=90 正确退出 1。沿用项目综合统计口径追加 `p2-diagnose --validation-feedback` 与 `--detailed-ablation` 真实只读入口后，覆盖门槛退出 0，综合 90.149626%、语句 92.377358%、分支 82.996769%。Ruff 退出 0，129/129 个跟踪 Python 文件 compileall、`git diff --check` 均退出 0。日志、JUnit、Coverage JSON/XML、只读诊断及各项准确退出码保存在本机 `tracefix-noeffect-full-short-20260925/`。下一轮已固定为 8 道普通开发题、C/T 各 24 项、C/T—T/C—C/T；两组只允许 T 开启恢复门槛，采纳要求 T≥16/24、较 C 净增≥4 且至少两题净增，并要求完整证据、无未解决基础设施事故。机器草案 SHA-256 为 `e99aa7531a85426cda57743edb8f1facc26f8899f5a65b78d5304bb81ee017b2`。最近记录共享余额 ¥23.12462080，低于完整日程 ¥41.28 上界；阶段预算提案 ¥42、缺口 ¥18.87537920，开跑前须刷新账本并接入正式执行器。未调用供应商、未改历史评分或账本、未使用 20 题留出集。详见 [`experiments/v0.8.20-no-effect-recovery-preflight.md`](experiments/v0.8.20-no-effect-recovery-preflight.md)。

### 2026-09-25 V0.8.21 正式路径接线与执行冻结

V0.8.20 的“执行器尚未暴露恢复门槛”与 ¥23.12462080 余额均已被本轮结果取代。重复 `no_effect` 后的完整目标读取门槛现已接入 `no_effect_recovery` P2 设计、CLI、有效配置、固定日程和恢复流程。C/T 唯一配置差异是该门槛开关；8 道普通题共 48 项，2 道收集失败资格题不进入主分析。六位置模拟供应商端到端测试通过，恢复后请求数不增加。Requests-1766 固定本地 httpbin 服务配置；pytest-10051 两阶段断言失败被具体分类，仍为失败。

执行提交 `adc8658ddf0425f8e88058203126536f093b38e1` 的离线 `p2-check --design no_effect_recovery` 通过，绑定 10 题 P1 证据、源码哈希、配方和依赖身份。全量 pytest 461 passed，Ruff/compileall 对 129 个跟踪 Python 文件退出码均为 0。pytest-only coverage 85.658153%；追加真实 P2 只读路径后的精确综合 coverage 89.634203%、语句 91.946391%、分支 82.276995%。`coverage report --fail-under=90` 按 Coverage.py 整数百分比规则退出 0；精确值未达到 90%，在交付中如实保留。完整结果见 [`experiments/v0.8.21-no-effect-recovery-execution.md`](experiments/v0.8.21-no-effect-recovery-execution.md)；协议和执行身份见 `benchmarks/experiments/v0.8.21-no-effect-recovery-execution/`。原始验证产物留在本机 `runs/priority-five-final-rerun-20260925/`。本轮没有付费请求或留出题运行。

共享 CNY 账本哈希仍为 `f0f1be69c2911ee01a853ff65f7a62fabe331e0da33045cf86f0926164ac74fc`，累计计算金额 ¥60.21126508、余额 ¥89.78873492、预留 0、未知请求否、5,847 笔请求。该账本计算值不是供应商账单；启动前须刷新价格与账本，并确认 Requests 本地服务健康。按最近已保存价格估算，完整 48 项上界 ¥41.28、阶段上限草案 ¥42；现有本地账本容量足够，但本轮没有启动付费比较。

保存状态：执行提交 `adc8658ddf0425f8e88058203126536f093b38e1`，协议与文档提交 `7906a2dd7f356cdaf8a9dd5628f58561670ce6b0`。普通推送被本机 `127.0.0.1:7897` 代理不可达阻断；没有改网络设置、没有强推，提交保留在本地分支。

### 2026-09-26 V0.8.22：修正反馈恢复门槛比较

固定 8 道普通开发题、C/T 各 24 项的同期比较已完成，协议 SHA `a98782c1dd266ed8658513332fd718b15dabe6bbd3bf186385e59b394fae0a15`，执行代码 `b435368`。C、T 独立验收均为 6/24；12/48 总成功，严格证据有效 28/48、问题 20/48，另有一次操作中断基础设施事故。证据问题集中在 Requests-1766 网络验收、Sphinx JUnit/审计报告缺失、pytest-10081 权限错误。未达 T≥16/24、净增≥4及至少两题净增的预注册标准，不采纳 T。账本计算费用 ¥19.466102、共享计算累计 ¥79.67736708；供应商账单只覆盖本批前 95 个请求，后 661 个请求尚未对账。完成态恢复复用 48/48、没有新请求。全量工程验证绑定执行版本：471 passed，综合覆盖率 90.02713069510712%（语句 92.29066765031354%、分支 82.82472613458529%），Ruff 和 129 个 Python 文件 compileall 通过。报告及限制见 [`experiments/v0.8.22-no-effect-recovery-48-campaign-results.md`](experiments/v0.8.22-no-effect-recovery-48-campaign-results.md)。

### 2026-09-26：输入边界、测试计数与产品补丁隔离

从 HEAD `5258dc44e324626cc9ab5345d5f7b68cc5d47613` 出发，调整 P2 新协议默认单请求限制为 null，增加 350k/2M 累计输入档与官方模型容量检查；因官方计数契约尚未验证，新正式请求请求前停机。新增只读校准入口，冻结证据 756/756 旧上界重建一致、755 个 usage 与响应证据核对无差异、37 个未发送视图不推断。测试调用先校验后计额，pytest 成功启动才计一次；审计文件迁至 checkout 外，内部目录显式登记并阻止补丁写入。最终全量 pytest 476 passed，Ruff、compileall、diff 检查通过；聚焦 87 例的五模块合并覆盖率 53%，未达 90% 门槛。原账本、协议、评分未更改，无付费调用、正式实验或留出题运行。限制与下一步见 [`experiments/2026-09-26-input-bound-zero-cost.md`](experiments/2026-09-26-input-bound-zero-cost.md)。

### 2026-09-26：冻结补丁独立复验与官方计数器安装核验

建立当前工作区与 244 份旧输入哈希清单，新增只读旧补丁派生复验、48 项汇总和官方计数诊断。运行准备阶段的 `.tracefix-build-tmp` 提前登记，避免构建失败时内部产物混入最终产品补丁。四道问题任务的 base/gold 在固定复验身份下均重复两次合格；原 20 项证据问题的保存补丁为 14 项两次通过、2 项稳定断言失败、4 项无产品改动。原始评分及账本不变。官方 Python 0.1.1 在 Windows 隔离环境安装失败：PyPI 无 Windows 轮子/源码包，本机无 Rust 工具链；755 个有供应商 usage 请求未获得官方本地 Token 计数，新正式请求继续停机。全量 pytest 477 passed，但全项目综合覆盖率 85.74% 低于 90% 门槛；定向新增回归、Ruff、compileall 通过。工程检查与限制详见 [`experiments/2026-09-26-frozen-evidence-rescue.md`](experiments/2026-09-26-frozen-evidence-rescue.md)。

### 2026-09-26：Docker 保存补丁验收试点

使用 Docker Desktop Linux 引擎对 pytest-10081 的 base、gold 和历史第 26 份产品补丁各重复两次严格验收：base 两次断言失败，gold 和补丁各两次通过。原输入只读，测试 checkout 位于容器 Linux 文件系统，审计证据另存。发现并修正 base/gold 补丁在 Linux checkout 的 CRLF 应用问题；Windows 挂载文件系统不适合作为旧 pytest 的运行 checkout，已移至容器原生文件系统。相关测试文件原有 81 项通过，新增 CRLF 回归 1 项通过；Ruff、compileall、diff 检查通过；244 份冻结输入哈希零变化。未跑全项目覆盖率，也未将该单题派生结果改写为历史评分。当前镜像和入口仅为 pytest-10081 试点；下一步锁定可重建依赖并试点 Requests、Sphinx。详情见 [`experiments/2026-09-26-docker-reverify-pilot.md`](experiments/2026-09-26-docker-reverify-pilot.md)。

### 2026-09-26：Agent 容器工具桥与三题新容器验收

新增 Linux JSONL 工具桥及三题零费用脚本入口。Agent 容器内使用现有五工具；Agent 容器只取得源码 bundle，不接收 gold、隐藏测试补丁、密钥或 Docker socket。独立验收使用同一镜像 ID 的新容器，基于固定 base 重新构造工作区。pytest-10081、Requests-1766、Sphinx-10449 均完成读取、产品补丁应用、公开 smoke、diff 导出；测试前 diff 为空，补丁后 diff 只含预期产品文件。三题独立严格验收各两次通过，测试节点、返回码、测试计数和依赖指纹与旧容器复验一致。Requests 在 Agent 和验收容器分别启动本地 httpbin；额外 Agent 文件未进入独立验收容器。详细运行报告写入 `E:\TraceFixRunsActive`，实验说明见 [`experiments/2026-09-26-docker-agent-container-e2e.md`](experiments/2026-09-26-docker-agent-container-e2e.md)。

最终全量 pytest `492 passed`、640.41 秒、退出码 0。改动文件范围 Ruff、compileall 和 `git diff --check` 退出 0。全仓 Ruff 遇到已有临时夹具和拒绝访问目录，不能作为通过；全树 compileall 返回 0 但也提示若干旧临时夹具不可遍历。覆盖率未运行。三个冻结输入 manifest 执行后校验一致；未调用模型、未运行正式实验、未改历史评分或账本。新 Agent 输出根支持 `TRACEFIX_RUNS_ROOT=E:\TraceFixRunsActive`。正式 `TraceFixRunner` 尚无 Docker 执行模式，自动恢复中断容器也未实现；脚本化闭环仅验证工具通道与验收隔离，不能证明模型修复能力。下一步为正式 Runner 增加容器开关和中断身份恢复 fixture，继续零费用验证。

### 2026-09-27：三类环境问题复核

Windows 短路径 E 盘临时根下，全量 pytest 501 项全部通过且无 setup 错误；覆盖率 83.462867%，因低于原 90% 门槛退出 1。四个此前报 Git 仓库识别错误的测试在短路径下 4/4 通过，原失败 workspace `.git` 路径长 262 字符，确认应同时缩短 TEMP/TMP、basetemp 与 cache 路径。pytest-10081 更换到源 tox 支持的 Python 3.10，正式 Runner base/gold/补丁目标资格均重复通过；完整公开文件两个新 Agent 容器结果相同：61 passed、9 skipped、1 个未 await coroutine warning 失败，未放宽 warning。Requests 整文件在两个独立 Agent 容器均为 90/90；Sphinx 保存补丁整文件 31/31 两次通过，Python 3.11 的同一失败在支持的 Python 3.10 下消失。更新的报告与 E 盘证据索引见 [`experiments/2026-09-27-environment-qualification.md`](experiments/2026-09-27-environment-qualification.md)。最终定向测试 23 passed、Ruff、compileall 与 diff check 通过。无模型调用、正式实验或留出集运行；pytest 整文件 warning 的 base/gold 对照待补，coverage 门槛仍未达标。

### 2026-09-27：pytest-10081 整文件正式矩阵与归因

恢复容器资格 Goal 后，以正式 Harness 验证入口准备 pytest-10081。attempt 01 缺 protocol 输入、attempt 02 用 Python 3.10 调度器触发 `datetime.UTC` 兼容错误，均未启动有效 pytest 格。attempt 03 四条容器命令完成但 Windows `docker cp` 无法创建 Linux 符号链接，原始结果未导出且不接受。修正为容器内 tar 打包、Windows 侧普通文件提取并保留链接清单后，attempt 04/05 的证据成功落盘。新增 `scripts/pytest10081_diagnostic_matrix.py` 运行官方 issue 节点、async warning 节点和整文件三组对照；Ruff、compileall、`git diff --check` 通过。attempt 05 六条命令均返回 0、18 格审计/JUnit/源码身份逐项复核完整。官方 issue 节点 base 两次失败、gold 与保存补丁两次通过；这是资格结果，不是模型能力指标。async warning 节点三种补丁两次均失败；整文件 base 两次 2 failed/9 skipped，gold 和保存补丁两次均 1 failed/9 skipped，剩余失败稳定为 `test_plain_unittest_does_not_support_async` 的未 await coroutine 被记录为 `PytestUnraisableExceptionWarning`。保存补丁解决其目标 PDB teardown 测试；环境指纹未漂移，导入来自每个 Linux checkout。

attempt 05 的默认矩阵摘要因保留了旧的 4 命令计数而误报 `all_commands_completed=false`；原始六条 host log、18 个 matrix-cell 报告、JUnit、审计、tar 和逐文件哈希均保留，E 盘 `g1-matrix-review.json` 已独立核对并记录。严格正式证据解析器把 8 个合法 skip 阶段（setup skipped、teardown passed、无 call）拒为不完整阶段；完整文件资格仍明确不通过，不能据独立诊断完整性改成通过。上游 async 测试注释本来预期子进程打印 RuntimeWarning 后通过，而冻结环境中子测试被转成失败；未做 Python 点版本对照，故不宣称已证明 Python 3.10.21 是单独根因。工作区保存补丁及原历史分数/账本未改，供应商调用为零。下一步先完成 Docker 中断/断连故障注入协议，再启动限定在 Goal 自有容器的真实恢复验证。

### 2026-09-27：Docker 恢复故障注入与首轮修复

G3 在执行前冻结 F01–F08 协议；协议 JSON SHA-256 为 `671da32465681732928932cb492a99f40a4664521b1a664d4f65bc321c295a4f`。G4 使用 Goal 自有、`network none`、无宿主挂载容器和精确子进程注入，保留全部现场。F01a 注入边界无效（请求已写），F01b 在 payload 写前中断且 call 保持 unknown；F02 在 `apply_patch` 已执行后丢弃响应帧，外部 diff 证实副作用且未重放；F03 在 host journal `result_received` 后、trace/result 落盘前崩溃，旧检查器把它报为 completed。F04 Docker 不可达与 F05 容器不存在旧输出无法区分；F04b 反证了“completed + Docker 不可达会误验身份”的源码担忧。F06 身份标签错配被拒；F07 正式验收 pytest 子进程被 SIGKILL，报告明确不合格；F08 完成态只读重复检查一致；F09 容器停止后旧检查器仍将其身份匹配报告成功。五个 Goal 自有长驻容器已停止但保留，未删除。

G5 首轮改动：`scripts/inspect_docker_run.py` 现在分类 `docker_unavailable`、`container_missing`、`container_stopped` 和 `result_received_not_persisted`；只有 trajectory 中存在对应 `tool_returned` 才把结果视作可复用，所有未知结果均不重放；不可恢复检查分类返回非零。检查器也会跟随已保存 `result.json` 的历史 trace 路径。`runtime.py` 在 `result.json` 落盘后才写 terminal phase。新增工具日志对照用例。`tests/test_docker_backend.py` 与 `tests/test_runtime.py` 24 passed，后续最终定向测试 7 passed；Ruff、compileall 与 `git diff --check` 通过。E 盘 `g5-recovery-01`、`g5-recovery-repeat-02` 对 F03/F04/F04b/F05/F09/F08 各重复两次，分类和输出稳定、源证据哈希不变；历史 F08 有 4 个 result_received 缺少持久化 trace，现正确按 unknown 拒绝复用。三题正式回归和最终验收见下段，整文件资格仍不合格，coverage 90% 门槛待 G6 验收。本工作不证明 Agent 模型修复率提升，供应商调用为零，历史评分和账本未改。

G5 最终验收：同一正式 Harness 对 pytest-10081、Requests-1766、Sphinx-10449 各完成一次 Runner 脚本替身贯通；每题严格资格与保存补丁分别在两个全新验收容器运行。三份 `docker-runner-e2e.json` 均 `passed=true`，十项 Runner/隔离/身份/严格审计检查全通过，每份 `model_requests=0`，验收镜像与冻结摘要匹配、无挂载且 Agent-only sentinel 不存在。详细容器 ID、公开文件诊断、JUnit、阶段审计和 report 在 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g5-formal-reruns`。这些结果是脚本替身的端到端和题目资格验证，不计为模型修复成功率。G6 全量工程/coverage 验收继续进行。

### 2026-09-27：G6 工程门槛最终运行（未验收）

最终全量 pytest 收集 **516 项，516 passed、0 failed**，耗时 622.37 秒；原 Coverage.py `source=tracefix` 与 `fail_under=90` 均未修改，pytest 退出码 1 仅因精确合并覆盖率不足。coverage 数据在相同脏工作区源码版本中包含全量 pytest、两个真实只读 P2 诊断入口、三题 `docker_runner_e2e` 正式桥接与独立验收、只读任务环境盘点，以及新增恢复边界定向用例。合并精确覆盖率 **89.32642487046633%**（语句 91.6837315%、分支 81.8996416%），低于 90% 共 0.6735751295 个百分点。最终 516 项 `--cov-append` 报告未单独导出 pytest-only 数值；G6 首次 502 项 pytest-only 为 83.462867%，不得误标成最终 516 项 pytest-only。

为 G6 新增 `tests/test_docker_backend.py` 行为回归：RPC 超时后调用保持 unknown 且不伪造结果、错误 bridge protocol 拒绝、容器清理必须有匹配 run label；准备阶段拒绝缺少 frozen manifest、任务或源码 commit 错配、冻结文件哈希错误、manifest 路径逃逸、错误镜像、缺源码 tag，以及会遮蔽 TraceFix 保留证据目录的源码归档。最终此文件 21 passed。Tracked Python 范围 Ruff 0，compileall 129/129，`git diff --check` 0。E 盘完整原始日志、JUnit、Coverage JSON、覆盖报告、退出码、静态检查和执行身份位于 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g6-engineering-final2`。额外一条不同协议历史诊断在运行前拒绝输入，记录为 preparation failure，不纳入结果/覆盖结论。

G6 仍为 in_progress：继续寻找与真实项目行为相关的覆盖缺口，不降低阈值、不扩大真实任务范围、不用无关执行凑覆盖率。pytest-10081 整文件 async warning 仍不合格；Requests/Sphinx 与三个正式 Runner 目标/补丁资格回归通过。供应商调用为零，未用留出题，历史评分和账本未改；脚本替身贯通不代表模型修复成功率变化。

### 2026-09-27：G6 Docker Runner 终态持久化回归与最终复跑

新增 `tests/test_runtime.py::test_docker_runner_persists_result_before_terminal_phase`，用 Docker 后端替身验证真实 `TraceFixRunner` 编排顺序：终态 phase 调用时 `result.json` 已落盘且状态相符；无产品差异时容器关闭不请求删除。该单测 1 passed，不涉及 Docker daemon 或模型调用。初次全量运行有 517 passed，但 pytest cache 仍指向 D 盘；保留为诊断记录，不作为最终合规运行。

最终全量运行将 TEMP、TMP、basetemp、cache 全放在 E:\TFP，517 passed、0 failed、589.73 秒，未出现 warning。Coverage.py 累计同一工作区的有效 pytest 与 G6 真实入口数据为 8059/8790 语句、2286/2790 分支，精确总覆盖率 89.33506044905009%（语句 91.68373151308305%、分支 81.93548387096774%）；覆盖门槛仍为 90%，pytest exit 1 仅由 coverage 导致。Ruff、compileall（`src/tracefix` 与 `tests/test_runtime.py`）和 `git diff --check` 通过。该新回归命中既有覆盖行，未提升累计覆盖总数，说明这条持久化路径虽有直接行为断言，但不是当前覆盖缺口来源。

最终日志、JUnit、coverage JSON、退出码在 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g6-runtime-durable-phase-final`；含 D 盘 cache 的早期尝试保留在同级 `g6-runtime-durable-phase`。G6 继续未验收，缺口 0.66493955094991 个百分点。下一步先审查其余未覆盖生产分支是否能由真实、隔离的行为场景触达；若无合理路径则继续如实保留短缺，G7 不启动。供应商调用为零，留出题、历史评分与账本未动；不能据此宣称模型修复率变化。

补齐 G6 要求的最终代码版本 pytest-only coverage：517 passed，604.11 秒，纯 pytest exact coverage 84.4041450777202%（语句 87.13310580204778%、分支 75.80645161290323%），因覆盖率不足退出码 1。使用全新 E 盘 `COVERAGE_FILE`，TEMP/TMP/basetemp/cache 均在 E:\TFP，证据目录 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g6-pytest-only-final`。该值和同版本有效诊断/Harness 入口累计值 89.33506044905009% 分开表述；均未达 90%，G6 保持 in progress。

### 2026-09-27：Goal 最终工程门槛与 G7 审计结论

后续为 G6 增加并复核 Runner、MinimalAgent、Docker 五工具及独立验收路径的行为覆盖；最终同版本全量 pytest **543 passed、0 failed**，综合有效入口覆盖率精确 **90.00863557858376%**（8108/8790 语句、2315/2790 分支），严格高于原 90% 门槛。独立 pytest-only 覆盖率 **85.77720207253886%**，单独报告。Ruff、129 个跟踪 Python 文件 compileall、`git diff --check` 均退出 0；没有降低阈值或更改覆盖范围定义。

G7 逐路径与哈希复核确认 G0 脏工作区快照、此前正式 Runner 摘要、G1 18 格原始诊断、G3 协议及 G4/G5 恢复重复证据完整。审计时发现 G5 Sphinx 实际读取 `E:\TraceFixRunsActive\environment-recovery-20260926\inputs-v3`（manifest `2ce71e…`），基线正式验收使用 `inputs-v4`（manifest `8dcbc6…`）。两份 manifest 的 9 个输入文件中仅 recipe 文件不同：G5 recipe JSON 为 Python 3.11/Windows 元数据，基线 recipe 为 Python 3.10/Linux；源 bundle、任务描述、候选、gold/test patch、协议、产品 patch 均逐项哈希相同。G5 与基线均在相同固定 Sphinx Linux 镜像、相同 source commit 上执行，G5 runtime 与四个独立验收容器没有构建步骤并使用该固定镜像。因此保留其固定镜像上的执行/验收结果，但明确不把两次称作完全相同 manifest/recipe，也不据 G5 证明该 recipe 可重建环境。原始报告保留不改。

历史账本文件的当前哈希与时间戳已只读核验，时间早于本 Goal；本 Goal 没有写入账本或评分。G0 快照范围本来不含该账本/评分文件，因此不声称由快照哈希证明其未变；审计结论以零调用守卫、执行范围及只读时间/哈希检查为限。供应商调用为 0，没有付费实验或留出题，旧评分、账本和历史运行证据未改。pytest-10081 公开文件资格继续不合格，async/unraisable warning 的归因仅限当前冻结 profile，未证明 Python 点版本单独致因；三题脚本替身 Runner 结果也不构成模型修复能力证据。

最终交接：[`handoffs/2026-09-27-container-qualification-final.md`](handoffs/2026-09-27-container-qualification-final.md)。主要原始证据仍保存在 `E:\TraceFixRunsActive\goal-container-qualification-20260927` 与既有 `environment-recovery-*` 目录；审计不覆盖此前现场。

### 2026-09-28：当前 checkout Skills/Docker 复现与工程复核

当前 GitHub 基线 `5a5bf8a24f605b3f2dc5392995b22940be9f3d61` 上实现默认关闭的 Skills 目录贯通、按需加载与字节预算；新增公开本地/Docker 零调用复现入口、pytest-10081 诊断参数校验、历史 pilot 配方清单、请求哈希前置契约和复现文档。Docker bridge 只传 path-free 技能目录，完整指令按需加载，并跨压缩保留。发现候选补丁路径会接受 `tests/../unsafe.py` 后，修为拒绝路径中任意 `..` 段，新增回归。

当前生产源码树 SHA-256 `430f586726f58f102e4abaac14429be95c861cb7c1c731bf7771d09c4b448020`。Requests-1766 正式 Runner Skills/TLS 在实际测试子进程完成 HTTPS CA 与 hostname 校验，Skills 去重及 7 次上下文压缩行为通过；4 个全新独立验收容器通过，零模型/供应商调用。E2E 摘要位于 `E:\TraceFixRunsActive\reproduce-zero-call-20260928\docker-requests-skills-tls-release-candidate`。本地与 Docker baseline/Skills-only 合成闭环均通过，Docker smoke image ID `sha256:1b9173d675c3214aecebc6f687736ab9401e06a2915f33cd4b190820c595b911`；均非真实模型修复成功率证据。

最终 pytest-only 全量 602 passed、0 failed、681.40 秒，Coverage.py 精确覆盖率 89.62%（8392/9128 语句、2415/2926 分支）；命令因保留的 90% coverage gate 退出 1。Ruff、compileall、`git diff --check` 与相关目标测试通过。纯 pytest gate 未满足，不能按终端四舍五入显示的 90% 认定工程验收通过。全量 JUnit、日志、coverage JSON/XML 和退出码位于 `E:\TraceFixRunsActive\reproduce-zero-call-20260928\pytest-only-final-v2`。

未完成边界：跨平台完整传递依赖哈希锁和 apt/build tool 锁定；pytest-10081 上一个可用 Python 3.10 点版本对照；Sphinx 基线 inputs-v4 外部重建材料；纯 pytest 90% gate。未接入 MCP/Serena。无供应商请求、付费实验、留出题访问或历史评分/账本变更。干净安装、材料缺口、回退和后续 baseline/Skills-only 方案见 [`reproduction.md`](reproduction.md) 与 [`tasks/2026-09-28-current-checkout-reproduction.md`](tasks/2026-09-28-current-checkout-reproduction.md)。
