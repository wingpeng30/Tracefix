# TraceFix harness 能力比较与改进建议

TraceFix 下一步值得加强的是：让 agent 在一致的隔离边界内积累可复用知识、取回执行证据，并持续完成可恢复的任务。最有价值的增量是**有类型且可失效的记忆、可取回的工具产物、结构化任务状态和可取消作业**。能力策略和后端契约支撑这些功能，受控子代理在此之后添加。

这份建议比较 DeepSeek Harness、Codex 和 Claude Code 的官方资料与公开实现，聚焦 harness 层。模型升级、训练、界面、插件市场不属于本次优先范围。建议排序依据当前代码缺口、可验证性和引入的复杂度，不是已有实验测得的收益排名。

## 比较身份与结论边界

审阅日期为 2026-10-14，时区 Asia/Shanghai。TraceFix 工作区为 `D:\Tracefix`，分支 `codex/ac-30-180s`，HEAD 为 `bc8b224d0419c5acab4a0a4d2abfb92e0ff4c67e`，另有未提交修改；下面的判断来自当前文件，不能仅凭 HEAD 重建。源码清单及 SHA-256 在 `runs/harness-review-20261014/workspace-observation.json`。

| 参照对象 | 本次依据 | 使用边界 |
| --- | --- | --- |
| DeepSeek Harness | 官方 `deepseek-ai/deepseek-harness`，固定提交 `d743267388641bc76f17c45ce8b4c231aed1d32c`，0.2.1-alpha.2；实际读取工具管线、会话、压缩、spill、指令、沙盒和子代理源码 | developer preview；可借鉴机制，不能按功能数量认定成熟度或安全性。见[官方安全说明](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/SAFETY.md)。 |
| Codex | OpenAI Docs 与官方 `openai/codex`，固定提交 `95273b84fbff40f9df7d71d50f1ca52c6489b25d`；实际读取工具策略、并发、进程管理、工具发现、仓库指令和记忆实现 | 官方文档描述产品行为，固定源码描述该提交；文档页会继续更新。桌面产品能力不自动等于所有 CLI 配置都启用。 |
| Claude Code | Anthropic 官方文档；公开 `sandbox-runtime` 固定提交 `c392e6cf9f8df957c66d9ab1461e2cfa99b1ab5d` 的 Linux 实现，以及 v0.0.76 的 Windows 实现 | CLI 核心并非本次可完整审阅的开源实现；公开 sandbox-runtime 不能代替完整 CLI 源码，也不能把其 Windows alpha 当成 Claude Code 已普遍支持的能力。见[Claude Code 许可证](https://github.com/anthropics/claude-code/blob/main/LICENSE.md)。 |

TraceFix 已实现 LLMGateway 共享计数、长驻 tokenizer worker、稳定上下文投影、经验记忆、Skills、MCP、Docker 和检查点恢复。最新同题 A/B/C 主成功仍是 11/30、11/30、19/30；C 实验关闭了记忆、Skills、MCP 和连续对话，真实压缩触发为 0 次，因此不能拿 19/30 证明下面机制的效果。网关改动发生在该实验之后。见[最新横向报告](../experiments/2026-10-11-abc-full30-comparison.md)。

历史 CI139 未通过完整工程门槛。当前未提交文件已包含兼容发送输出上限、可选 close、低信任记忆回放识别及清理错误处理等修复，不能把历史归因直接当作当前尚未修复的缺陷；这些修改仍需各自的新验收身份。此处只做静态比较。

## 三种参照设计的启发

**DeepSeek Harness 最值得参考的是扩展后的约束仍然成立。** 它将能力接口、提供者和消费方拆开，并给注册与资源清理明确的 scope。工具管线在扩展 hook 和审批之后还有只能拒绝或弃权的最终 guard，参数在执行前固定。这适合 TraceFix 将横切逻辑抽成小接口，保留不可绕过的预算和执行保护。见[架构](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/docs/architecture.md#L131)、[工具管线](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/core/tools/src/index.ts#L1509)。

**Codex 最值得参考的是工具运行时的职责划分。** 工具审批、沙盒选择和失败处理集中于 orchestrator；并发能力由工具声明，并用共享读锁或独占写锁协调；长命令有进程身份、输出采集、取消和回收。记忆实现将单会话提取和全局整理分开，提取有工作认领及秘密清理，整理有独占作业。见[orchestrator](https://github.com/openai/codex/blob/95273b84fbff40f9df7d71d50f1ca52c6489b25d/codex-rs/core/src/tools/orchestrator.rs)、[并发实现](https://github.com/openai/codex/blob/95273b84fbff40f9df7d71d50f1ca52c6489b25d/codex-rs/core/src/tools/parallel.rs#L213)、[记忆提取](https://github.com/openai/codex/blob/95273b84fbff40f9df7d71d50f1ca52c6489b25d/codex-rs/memories/write/src/phase1.rs)、[记忆整理](https://github.com/openai/codex/blob/95273b84fbff40f9df7d71d50f1ca52c6489b25d/codex-rs/memories/write/src/phase2.rs)。

**Claude Code 最值得参考的是长期使用时的上下文组织。** 人工项目规则与自动记忆分开；小索引负责导航，主题正文按需读取；路径规则只在相关工作中加载；子任务可以有独立工具、技能和上下文。其规则与记忆属于模型上下文，强制权限另行执行。见[记忆与规则](https://code.claude.com/docs/en/memory)、[子代理](https://code.claude.com/docs/en/sub-agents)、[权限](https://code.claude.com/docs/en/permissions)。

## TraceFix 已有能力与具体缺口

| 能力 | 当前已有 | 值得补强的部分 |
| --- | --- | --- |
| 长期记忆 | 仓库命名空间、证据、候选与已验证版本、冲突继承、停用/回滚、固定 Skills 快照、提取预算 | 已验证内容限于固定工具流程；缺少有类型知识、当前内容适用性判断和检索效果反馈。 |
| 上下文 | 完整历史与请求视图分离、80%/60%策略、用户原文与工具配对保护、版本投影、主动压缩、共享计数 | 首尾摘要不充分表达决策和排除的假设；长会话需要结构化状态与可取回证据。 |
| 沙盒 | ordinary Docker 默认禁网、非 root、无宿主挂载、只读 root、资源限制、执行器写保护与身份检查 | 后端能力组合与诊断不统一；需要逐项报告实际隔离能力，并把扩展调用纳入同一策略。 |
| 恢复 | 原子检查点、锁、身份核对、未决请求保护、Docker 已提交批次快照及重建恢复 | 可扩展工具副作用分类、作业恢复、隔离 fork 和显式 reconciliation。 |
| 工具输出 | ToolResult 轨迹、模型展示裁剪、缓存失效机制 | 没有统一按需取回产物接口；部分原始测试输出在 ToolResult 形成前已裁剪，搜索无分页 cursor。 |
| 执行 | 固定工具、pytest 白名单、超时、累计预算和纠偏 | 同步执行，缺少作业身份、增量输出、取消与有界并发；需补 Windows 子进程树清理。 |
| 扩展 | 按需 Skills、引用限制、版本哈希；固定 Serena MCP 查询 | skill 元数据尚未形成强制能力契约；MCP 功能和生命周期较专用，缺少通用策略与发现层。 |
| 可观测性 | JSONL flush/fsync、请求视图、测试证据、验证门、恢复身份 | 需要以一个调用关联策略、排队、执行、展示、取回和失效，支持机制级分析。 |

主要源码依据：[memory.py](../../src/tracefix/memory.py) 第22、87、213、305行；[工具协议](../../src/tracefix/tools/base.py) 第29、107行；[工具实现](../../src/tracefix/tools/builtin.py) 第312、920、939行；[展示层](../../src/tracefix/agent/presentation.py) 第52行；[上下文管理](../../src/tracefix/context/manager.py) 第367、522行；[运行配置](../../src/tracefix/runtime.py) 第184、206至219行。

Docker 与高级能力的组合需要准确表述：当前 `docker_recovery_enabled` 允许 Docker 的记忆和连续对话路径，已有 `_attach_docker_memory` 实现；自定义 Skills、Serena MCP 和结束前 regression_targets 仍有本地后端限制。不能照旧文档笼统称全部高级功能仅支持 local。

## 优先改进

### 有类型且可失效的知识记忆

当前记忆最强的是发布与证据保护，最弱的是能保存多少有用知识。`_VERIFIED_INSTRUCTIONS` 只允许读取、搜索、聚焦补丁、相关测试和最终 Diff 五种固定句式。未来应能保存“该模块的分页约束”“此环境有效的测试配方”“一种已经失败的方法”，同时区分来源和确定程度。

建议保留现有版本链，新增 `project_fact`、`decision`、`negative_result`、`procedure` 和明确用户反馈等类型。每条记录包含来源、适用路径/符号、内容指纹、环境指纹、最后核对时间及撤销关系。用户反馈由用户原话支持；代码事实由源码和测试支持；模型推断留作候选。失败经验描述当时条件，不自动升级为禁止以后尝试的规则。

仓库身份拆为 logical repository 与具体 checkout。Git worktrees 可按可信 common-dir 或显式 ID 共享；独立 clone 是否共享由配置决定，不能仅凭 remote URL 合并信任域。命名空间迁移保留收据和旧记录。检索先加入路径、符号、错误签名和任务类型，返回选中理由，再评估是否需要 embedding；中文关键词和同义表达要单独验证。

现有 `select()` 校验的是历史证据内部一致性与文件是否存在，不等于检查当前文件内容仍然符合经验。依赖内容或环境变化应标为 stale，重新核对后才升回适用；无关文件变更不必让整库失效。增加命中、采用、复核成功、撤销等反馈，同时避免把访问次数当成正确性。

最小验收是：同仓库两个 worktree 按策略共享；不同仓库不互相污染；依赖文件变化后旧知识降级；第二个任务能召回一条真实模块约束和一条有条件的失败经验；记忆文字不能赋予网络或文件权限。Claude 的按需主题记忆、Codex 的提取/整理分工可作为参照，但 TraceFix 的发布证据要求应保留。[Claude 记忆](https://code.claude.com/docs/en/memory)、[Codex 记忆控制](https://learn.chatgpt.com/docs/customization/memories)。

### 可取回的工具产物

模型看到日志首尾时，关键异常可能在中段。TraceFix 虽保留 ToolResult 轨迹，但这不保证原始进程输出全部保留，也没有供模型读取的统一入口。重复跑测试既耗时又可能改变现场。DeepSeek 的 spill 将完整接受结果存成会话产物，再返回摘要和 locator，适合借鉴。[spill 实现](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/spill/spill-policy/src/index.ts#L91)。

建议在执行器采集阶段写入有界原始输出，再生成展示内容。增加 `ArtifactRef` 与 `read_tool_artifact(artifact_id, offset, limit)`，绑定任务、call、内容哈希、来源源码身份和保留策略；提供索引或产物内搜索。超过磁盘限额时明确标记原始输出不完整，不能声称完整存储。

取回产物是独立工具读取，计入读取和上下文预算。本地/Docker 返回相同协议，模型只能访问获准的 artifact ID，不能获得任意宿主 runs 路径。源码改变后旧产物仍可用于历史分析，但不能冒充当前测试事实。源码 read 分页、搜索 cursor 和日志取回应各有语义，不能用一个任意文件读取工具混在一起。

最小验收是：100KB 日志中段的唯一失败信息能取回；取回绑定原 call 和哈希；恢复后的 locator 仍有效；别的任务、敏感内容和超额读取被执行层拒绝；无需再次执行测试即可定位错误。候选指标是关键信息取回率、重复测试率、上下文开销和存储量。

### 结构化任务状态与上下文恢复

自动压缩已经实现，下一步应改善压缩后可继续工作的内容。建议在完整历史之外维护 `TaskState`：用户约束、验收条件、已确认事实、排除的假设、当前方案、待办与阻塞。每项事实附带 call、源码/环境身份和有效状态，模型提出的结论与工具确认的事实分开。

TaskState 写进检查点，并用于构造短工作摘要；完整用户原文和事件仍保持不变。用户追加要求应有明确版本和撤销关系，不能凭摘要猜测旧要求被取消。已激活 skill 保留来源与版本，长正文需要可重新加载的引用；涉及用户原文和 mandatory 规则的策略变化另立协议，不能为了压缩成功静默删除约束。

每次压缩检查授权、预算、工具配对和最新验证事实是否仍成立，并记录被替换的原事件范围。若没有实际缩减或没有安全可移除内容，保持现有 no-op 与拒绝超窗发送的原则。DeepSeek 的 derived surface 与确定性 pruning，以及 Claude 的上下文隔离，提供了参照。[会话视图](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/core/session/src/surface.ts#L80)、[工作机制](https://code.claude.com/docs/en/how-claude-code-works)。

最小验收是：小窗口连续多次跨阈值压缩后，仍能保留中段关键事实、用户要求和未完成验收；改动相关源码后旧结论失效；压缩后恢复保持投影身份、前缀和工具配对；不可压缩输入不产生重复付费请求。真实30题压缩触发0次，不能替代此测试。

### 统一能力策略与后端契约

建议给现有工具协议增加执行侧元数据：读写范围、网络需求、副作用类别、幂等性、并发方式、所需后端能力及结果身份。它们用于策略和恢复，不必全部进入模型 schema。策略返回结构化 allow/deny/needs-authorization 及理由，任务已有授权范围内的常规动作继续自动执行。

最终执行前，以用户授权、运行策略、工具能力和后端实际能力共同判定。Skills、记忆、仓库规则、MCP 返回值不能扩大权限。未来扩展 hook 可观察或收紧策略，最终 guard 不允许被覆盖。模型请求改写在最终窗口检查、费用准入和请求冻结前完成；工具调用参数改写在最终能力判定及执行前固定，审批针对固定后的参数，并记录原值与最终值。可参考 [Codex 沙盒与审批职责](https://learn.chatgpt.com/docs/sandboxing)、[DeepSeek 最终 guard](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/core/tools/src/index.ts#L725)。

后端应提供能力矩阵和行为探针，分别报告读、写、网络、进程、执行器保护与资源限制，避免把“配置为隔离”当成“已验证隔离”。TraceFix 已有执行器写保护探针，可扩展到网络拒绝、越界写入和子进程约束。能力不可用时按任务要求停止，不能静默降级成 local。

优先补 ordinary Docker 中的结束前回归门、可信自定义 Skills 快照和只读符号查询。记忆存储仍在容器外由可信 broker 管理；传入的是固定快照，执行环境不获得宿主记忆库写权限。保留当前禁网、无宿主可写挂载和最小环境原则。

最小验收是：同一工具在不同 profile 下有明确结果；容器中的恶意仓库规则和 MCP 内容不能提权；每次策略决定可追溯；local 与 ordinary Docker 同一离线任务保留相同验证语义；不支持的组合在模型初始化前给出一致诊断。

### 可取消作业与受控并发

同步 `execute()` 对短调用足够，但长测试、符号服务和构建需要独立作业。建议增加 `JobHandle`、poll、cancel、deadline、heartbeat、增量输出和最终 outcome。先用于测试与读取，不急于开放任意 shell。Codex 的进程管理有可参考的执行、输出、终止和回收职责。[进程管理源码](https://github.com/openai/codex/blob/95273b84fbff40f9df7d71d50f1ca52c6489b25d/codex-rs/core/src/unified_exec/process_manager.rs)。

可并行的工具由声明及策略共同决定。独立只读任务共享源码快照；补丁、测试和快照提交设 barrier，测试也可能写临时文件，不能简单视为纯读取。所有排队与执行时间纳入任务期限，所有作业共享累计资源和费用限制。

取消必须覆盖进程树，并保留实际结果状态。当前 Windows 本地测试超时主要调用 `process.kill()`，需要证明孙进程也被清理。工具分为 not_started、running、completed、cancelled、outcome_unknown；有副作用且结果未知时禁止自动重放，已知只读失败才按明确重试策略处理。DeepSeek 将未开始与已开始未知分开修复，可参考其[恢复分类](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/core/session/src/repair.ts#L157)。

最小验收是：长测试中能查询并取消；取消后无遗留进程；读作业并发不会混用源码版本；崩溃后作业状态可解释；未知供应商请求继续保留费用预留且不重发。先测等待耗时和重复调用，再评价真实任务提速。

### 分层仓库规则与扩展生命周期

TraceFix 尚未见原生 AGENTS.md 或路径规则加载器。建议加入独立 `RepositoryInstructions`，按项目根到目标文件父目录解析作用域，短入口先加载，访问文件时再加载适用规则。每次加载记录来源、哈希、范围、原因与 token 成本；变更或删除时追加失效通知，不追溯改写已发送请求。见[Codex AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md)、[DeepSeek 指令刷新](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/context/agent-instructions/src/index.ts#L315)。

仓库规则可表达代码规范和测试约定，执行权限仍由宿主策略决定。处理 symlink、目录嵌套、冲突和预算溢出，不能让仓库文件授权联网、访问凭据或执行任意脚本。对不可信仓库应将规则标为建议上下文，信任提升须来自用户或可信配置。

Skills/MCP 的下一步是 capability manifest、兼容性检查、固定版本、按需发现和隔离生命周期。目前 Skills 接受部分标准元数据，但没有据此执行 allowed-tools/compatibility 约束；Serena 为少量固定只读查询，每次重新建立容器。可先让同源码身份的只读 session 复用，patch 后重新核对。工具目录变大后再引入 lazy schema，新增 schema 必须参与上下文计数与恢复身份。当前上下文管理会拒绝运行中 schema 改变，因此动态暴露需要显式工具目录版本与投影代次转换，保存前后身份；意外 schema 漂移仍失败关闭。见[Codex 工具发现](https://github.com/openai/codex/blob/95273b84fbff40f9df7d71d50f1ca52c6489b25d/codex-rs/core/src/tools/handlers/tool_search.rs)、[Claude MCP](https://code.claude.com/docs/en/mcp)。

先开放 `pre_request`、`pre_tool`、`post_tool`、`pre_checkpoint`、`pre_finish`、`teardown` 等有类型接口。observer 只能观察，validator 只能收紧或拒绝；hook 的顺序、超时、失败策略和身份进入记录。未经可信配置的仓库 hook 不自动执行。这样能逐步拆开 MinimalAgent/Runner 的横切职责，无需迁移整个技术栈。

最小验收是：子目录规则仅对相关文件生效；规则更新可解释；不兼容技能明确拒绝；hook 不能关闭核心保护；扩展卸载后无残留资源；清理错误附加到结果而不覆盖主要停止原因。

## 后续可以添加的能力

| 能力 | 建议范围与理由 | 前置条件 |
| --- | --- | --- |
| 有界子代理 | 先做只读仓库探索、独立补丁审查或日志分析，使用小上下文并返回证据摘要；写入子代理使用独立 worktree，父 agent 整合验收 | 子权限是父权限与子策略交集；共享总预算；depth/live 数限制之外，还要限制累计调用和成本。参照 [Codex 子代理](https://learn.chatgpt.com/docs/agent-configuration/subagents)、[DeepSeek 子代理](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/subagent/subagent/src/child-agent.ts#L219)。 |
| 多语言执行适配器 | 在 pytest 之外增加受约束的 build/test/typecheck/lint 适配器，统一测试事实结构与环境身份；补足当前 Python 修复框架的应用范围 | 统一策略和作业协议；选择明确程序及 argv schema，保留可验证结果，避免直接退化为任意 shell。 |
| 环境准备与复用 | 显式准备阶段产出锁定镜像/依赖 manifest，按仓库与依赖身份预热；任务阶段继续默认离线 | 准备阶段网络和安装另有授权；环境缓存不跨信任域写共享目录，秘密不留在镜像/快照。当前运行不得自动建镜像或拉包。 |
| 隔离 fork 与回滚 | 从已提交安全批次分叉尝试；明确区分对话恢复、工作区恢复和新分支 | 父运行和费用 lineage 不变；回滚后证据与缓存失效；外部副作用和未知付费响应不能被文件回滚抵消。 |
| 轻量本机沙盒 | 对可信个人仓库提供可选 OS backend，降低 Docker 使用成本 | 独立证明平台能力，保持失败关闭；不替代当前 Docker 基线，不在此研究阶段默认启用。 |
| 机制诊断与评测 | 将 policy、queue、execute、present、artifact-read、invalidate、checkpoint 按 call/attempt 关联，提供查询和脱敏导出 | 复用已有轨迹，分别测记忆、上下文、隔离和恢复；把合成契约通过与真实模型主成功分开。 |

补丁并发之前还应增加文件内容前置条件与事务提交：读取时返回版本，应用时核对预期 hash，冲突时重新读取，不能把唯一上下文匹配当成已解决并发冲突。回滚必须失效对应测试事实，不能沿用回滚前的通过状态。

## 沙盒参考中不宜照搬的部分

DeepSeek 当前本机沙盒主要约束文件写效果；所审阅 Linux bwrap profile 没有 network namespace 参数，macOS profile 也不能被概括为全维度隔离，Windows 报告 partial。TraceFix ordinary Docker 已在禁网、非 root、挂载和执行器保护等方面建立更严格的默认边界。应借鉴能力报告与失败关闭，而不凭“支持 sandbox”几个字更换实现。[固定 profiles 源码](https://github.com/deepseek-ai/deepseek-harness/blob/d743267388641bc76f17c45ce8b4c231aed1d32c/packages/sandbox/sandbox-local/src/profiles.ts)。

Claude Code 文档说明 shell sandbox 不覆盖所有文件工具、MCP、hooks 和 LSP。公开 sandbox-runtime 的 Linux 实现使用 network namespace、只读文件系统和代理桥，可供将来轻量 backend 参考；不同工具的边界仍需分别验证。[Claude sandboxing](https://code.claude.com/docs/en/sandboxing)、[固定 Linux 源码](https://github.com/anthropics/sandbox-runtime/blob/c392e6cf9f8df957c66d9ab1461e2cfa99b1ab5d/src/sandbox/linux-sandbox-utils.ts)。

当前 Claude Code 官方文档与独立 sandbox-runtime 的 Windows 状态不同：前者仍说明原生 Windows shell sandbox 不可用，后者有 alpha 实现。Windows runtime 的真实拒绝探针值得参考，但不能当成 CLI 已部署的保证。[Windows runtime v0.0.76](https://github.com/anthropics/sandbox-runtime/blob/v0.0.76/src/sandbox/windows-sandbox-utils.ts)。

## 建议实施顺序与验收

1. **先形成有用且可撤销的知识。** 记忆类型、来源、内容失效和可解释检索先做，沿用原有发布/版本保护。
2. **让已有执行证据可取回。** ArtifactRef、原始输出采集、产物读取预算，并补齐相关本地/Docker接口。
3. **建立结构化工作状态。** TaskState 与上述知识/产物引用结合，验证多次压缩和恢复后的事实资格。
4. **建立统一工具能力与后端契约，再引入作业协议。** 完成策略、取消、进程树清理和副作用 outcome；独立只读任务才并发。
5. **补高级能力的 Docker 组合及扩展接口。** 路径规则、可信 Skills 快照、符号 session、最终回归门逐项交付。
6. **最后试有界子代理与多语言适配器。** 为真实收益另立对照，优先只读，按共享预算和独立验收决定是否采纳。

每个增量使用独立功能开关与配置身份，先做相应离线契约/故障回放，再按正式交付范围跑现有完整工程门槛。90%覆盖率、预算保护和未知调用规则保持。新真实模型实验需要另行授权、冻结源码与协议；不沿用历史额度，不重发未决请求，不把旧19/30当成新功能效果。

最小故障集合应包括：过期记忆、仓库规则注入、MCP身份变化、长日志中段遗漏、压缩丢失待办、并发读取后源码变化、取消孙进程、工具执行前后崩溃、快照写入失败及供应商响应未知。指标分别记录错误事实采用率、取回率、重复调用率、未授权动作、恢复重复副作用、时间和 token/费用；不把它们混成一个“agent能力分”。

## 本次记录与验证

本次产出为比较报告和文档入口同步，没有改动运行源码、测试或配置，没有产品测试、供应商调用、第三方代码执行、依赖安装、提交或推送。已有未提交源码修复保留原状。

官方文档通过网页工具读取，源码通过 GitHub connector 固定 ref 读取；本地 urllib 网络获取因 DNS 失败未取得源码，随后使用 connector 完成。Codex 和部分 DeepSeek/Anthropic 原始源码缓存、来源收据及本项目80个文件的哈希保存在 `D:\Tracefix\runs\harness-review-20261014`，供本地复核，不纳入报告功能实现。

本机默认 `python` 是 `D:\anaconda3\python.exe`，默认导入未找到 tracefix；显式将工作区 `src` 加入导入路径后，实际导入为 `D:\Tracefix\src\tracefix\__init__.py`。这次导入核对不等于安装包或工程验收。

报告交付检查为文档 diff 检查、本地链接解析、原始源码缓存/工作区哈希核对和入口回读；具体结果记录在同目录 `document-validation.json`。历史 CI、付费实验和固定当前入口继续分别保留自己的身份。
