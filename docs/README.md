# TraceFix 文档索引

新会话首先读取[当前状态](current-status.md)。这是固定入口，区分当前工作区、已完成实验和历史计划；
不能按旧交接的文件名或文末“下一步”决定今天的进度。

本目录保存代码演进、实验解释和后续任务记录。README 首页只保留当前使用方法与主要结果，
完整的历史和证据从这里进入。

## 新任务交接入口

- [G5阶段验收](tasks/2026-10-16-g5-acceptance.md)：c02c79d / CI169完整门、平台互补、镜像/产物身份及使用限制；下一步G6。

- [后台作业候选说明](background-jobs.md)：本地启用、四工具、配额和恢复，以及ordinary组件与正式Docker/安装包的验收边界。
- [G5实施与验证记录](tasks/2026-10-16-g5-progress.md)：后台作业实现、失败修复、历史检查和最终阶段关闭的时间线。
- [G5—G8起点验收](tasks/2026-10-16-harness-runtime-baseline.md)：ac64510精确CI159、本地工程、修复及原始证据。
- [G5—G8实施计划](tasks/2026-10-16-harness-runtime-implementation.md)：已批准的逐阶段步骤、验收门和实际状态。
- [2026-10-16 GitHub保存记录](maintenance/2026-10-16-github-save.md)：原工作区与G4候选的远端提交、保存范围、验证边界与下一阶段入口。
- [`current-status.md`](current-status.md)：当前代码身份、最新结果、验证边界与下一任务起点。
- [`security.md`](security.md)：普通任务、文件工具、子进程、Docker 和提示注入的基础安全约定。
- [`maintenance/2026-10-14-harness-comparison-and-improvements.md`](maintenance/2026-10-14-harness-comparison-and-improvements.md)：DeepSeek Harness、Codex、Claude Code 的官方资料/源码比较，及记忆、沙盒、执行、上下文等候选改进与验收。
- [`tasks/2026-10-14-harness-foundations-implementation.md`](tasks/2026-10-14-harness-foundations-implementation.md)：harness 前四项的阶段实施计划、工程门和逐阶段完成清单；阶段进度以当前状态入口为准。
- [`experience-memory.md`](experience-memory.md)：候选分支有类型知识记忆的证据等级、checkout 失效、召回、管理命令和 v1→v2 迁移说明。
- [`tool-artifacts.md`](tool-artifacts.md)：候选分支 G2 的 CLI/TOML/Python 启用方法、产物读取搜索、配额、脱敏和恢复规则。
- [`task-state.md`](task-state.md)：候选分支 G3 TaskState 的字段、模型更新权限、压缩锚点、checkpoint 与恢复约束。
- [`capability-policy.md`](capability-policy.md)：统一执行策略、功能准入、local/ordinary Docker矩阵、独立验收和零供应商组合入口；实际验收边界见当前状态。
- [`tasks/2026-10-15-g4-local-completion.md`](tasks/2026-10-15-g4-local-completion.md)：用户要求不上传后的G4本地补齐、环境诊断、测试及安装包原始证据。
- [`tasks/2026-10-15-g4-environment-acceptance.md`](tasks/2026-10-15-g4-environment-acceptance.md)：获准下载依赖/镜像后的G4环境补齐、真实Docker/Serena组合与双版本本地工程验收。
- [`roadmap.md`](roadmap.md)与[`development-history.md`](development-history.md)：近期工作和具体证据。
- [`experiments/2026-10-11-abc-full30-comparison.md`](experiments/2026-10-11-abc-full30-comparison.md)：最新A/B/修复后C同题30题横向比较。
- [`handoffs/2026-09-29-agent-harness.md`](handoffs/2026-09-29-agent-harness.md)：历史交接日志，先按顶部当前入口阅读。
- [`handoffs/2026-09-17-v084.md`](handoffs/2026-09-17-v084.md)：早期版本归档；文末第26节是历史状态，不能当作今天的进度。

## 项目历程

- [`roadmap.md`](roadmap.md)：项目目标、当前阶段、验收门槛与下一步行动。
- [`development-history.md`](development-history.md)：从 V0 接口骨架到 V0.3.2 实验可追溯性
  A/B 的代码说明、设计取舍、验证结果和 Git 版本。
- [`reproduction.md`](reproduction.md)：干净安装、wheel、零模型调用本地/Docker 复现、材料清单与当前外部复验边界。
- [`tasks/2026-09-28-current-checkout-reproduction.md`](tasks/2026-09-28-current-checkout-reproduction.md)：当前 checkout 的 Skills/TLS 实现、验证门槛与未完成环境复验记录。

## 实验记录

- [`experiments/README.md`](experiments/README.md)：所有 Baseline、压力实验、回归实验和
  A/B 的统一索引与结论边界。
- [`experiments/v0.3.0-context-3k.md`](experiments/v0.3.0-context-3k.md)：3k 压缩压力
  实验、7/10 失败分析和修复启示。
- [`experiments/v0.3.1-agent-loop-regression.md`](experiments/v0.3.1-agent-loop-regression.md)：
  补丁协议与 Agent 收尾修复后的 10/10 回归。
- [`experiments/v0.3.1-context-32k-ab.md`](experiments/v0.3.1-context-32k-ab.md)：关闭压缩
  与生产 32k 阈值的真实 API 对照。
- [`tasks/v0.3.2-traceability-and-context-tasks.md`](tasks/v0.3.2-traceability-and-context-tasks.md)：
  运行溯源、实际请求视图和四道多文件任务的实现说明。
- [`tasks/v0.4.0-independent-verification-and-paired-eval.md`](tasks/v0.4.0-independent-verification-and-paired-eval.md)：
  独立验收、测试篡改检测与交替重复实验框架说明。
- [`tasks/v0.5.0-real-github-issue-tasks.md`](tasks/v0.5.0-real-github-issue-tasks.md)：
  三道 SWE-bench Verified 多源码文件任务、固定提交和隐藏补丁接口。
- [`tasks/v0.5.1-windows-ci-portability.md`](tasks/v0.5.1-windows-ci-portability.md)：
  Windows CI 的任务哈希与深层临时目录 Git 兼容修复。
- [`tasks/v0.5.2-runner-longpaths.md`](tasks/v0.5.2-runner-longpaths.md)：
  将 Windows 长路径兼容补齐到 Agent Runner 与 Git 工具链。
- [`experiments/v0.3.2-fixture-validation.md`](experiments/v0.3.2-fixture-validation.md)：
  多文件任务初始失败、标准补丁和隐藏测试的离线验收记录。
- [`experiments/v0.4.0-paired-context-32k.md`](experiments/v0.4.0-paired-context-32k.md)：
  24 次交替配对实验、未触发 32k 的否定性结论与下一功能决策。
- [`experiments/v0.5.0-real-issue-32k-prescreen.md`](experiments/v0.5.0-real-issue-32k-prescreen.md)：
  三道真实 Issue 的 32k 单次预筛选、0/3 入选结论和 Repo Map 功能决策。
- [`experiments/v0.8.1-real-environment-behavior.md`](experiments/v0.8.1-real-environment-behavior.md)：
  五道新候选的环境、base/gold 验收与 0/5 资格结论。
- [`tasks/v0.8.3-auditable-real-validation.md`](tasks/v0.8.3-auditable-real-validation.md)：
  构建、收集、pytest 执行证据和失败分类改进。
- [`experiments/v0.8.3-real-environment-validation.md`](experiments/v0.8.3-real-environment-validation.md)：
  12 道真实候选的 7/12 合格重验结果。
- [`tasks/v0.8.4-strict-real-validation.md`](tasks/v0.8.4-strict-real-validation.md)：
  严格 node ID、受审查收集失败、解释器复用与环境指纹修复。
- [`tasks/v0.8.5-p0-trusted-validation.md`](tasks/v0.8.5-p0-trusted-validation.md)：
  环境所有权、双阶段审计、源码隔离及 P0 全量测试与覆盖率门槛。
- [`experiments/v0.8.5-p1-real-environment-validation.md`](experiments/v0.8.5-p1-real-environment-validation.md)：
  新环境、固定配方下 12 道真实任务的统一 base/gold 行为复核。
- [`experiments/v0.8.6-p2-whole-system-protocol.md`](experiments/v0.8.6-p2-whole-system-protocol.md)：
  10 道任务、60 次整体优化 C/T 的冻结协议与零费用演练。
- [`experiments/v0.8.6-p2-formal-diagnostic.md`](experiments/v0.8.6-p2-formal-diagnostic.md)：
  已有 60 项正式轨迹的终止分类、测试修改细分和机制触发诊断；未新增付费调用。
- [`experiments/v0.8.7-p2-evidence-contract.md`](experiments/v0.8.7-p2-evidence-contract.md)：
  统一证据统计、评分契约核对、既有轨迹机制分析及消融与留出集冻结方案。
- [`experiments/v0.8.8-holdout-freeze.md`](experiments/v0.8.8-holdout-freeze.md)：
  不可变候选顺序、首轮留出资格缺口及独立长上下文机制集合。
- [`experiments/v0.8.9-holdout-final-freeze.md`](experiments/v0.8.9-holdout-final-freeze.md)：
  20 道普通资格留出任务的兼容重验、严格证据冻结及长上下文关键信息保留验证。
- [`experiments/v0.8.11-development-ablation.md`](experiments/v0.8.11-development-ablation.md)：
  开发集四组正式 120 项、独立对账及零调用恢复；无组达到预定净收益要求，保留基线与未使用留出集。
- [`experiments/v0.8.13-presentation-only-preflight.md`](experiments/v0.8.13-presentation-only-preflight.md)：
  更正工具裁剪/历史折叠统计，冻结 60 位置输出呈现单变量协议、合成零费用预检和预算停止决策。
- [`experiments/v0.8.14-presentation-only-paid-development.md`](experiments/v0.8.14-presentation-only-paid-development.md)：
  完成 C/T 60 项正式开发集比较，记录 58/60 有效证据、费用对账、恢复结果及未通过采纳门槛的原因。
- [`experiments/v0.8.15-validation-closure-results.md`](experiments/v0.8.15-validation-closure-results.md)：
  48 项验证闭环正式比较、独立验收成功率、费用与未采纳结论。
- [`experiments/v0.8.16-validation-feedback-diagnostic.md`](experiments/v0.8.16-validation-feedback-diagnostic.md)：
  48 项既有轨迹的空补丁与验证反馈离线诊断。
- [`experiments/v0.8.17-selection-feedback-correction.md`](experiments/v0.8.17-selection-feedback-correction.md)：
  去选测试审计修正、17 次历史反馈更正和下一轮 48 项比较预算草案。
- [`experiments/v0.8.10-ablation-preflight.md`](experiments/v0.8.10-ablation-preflight.md)：
  四组 120 项协议、真实任务与正式入口零费用模拟、恢复、完整依赖锁及共享预算预检。
- [`experiments/v0.8.4-real-environment-validation.md`](experiments/v0.8.4-real-environment-validation.md)：
  12 道真实候选严格重验的 10/12 行为资格结果与边界。
- [`../benchmarks/experiments/v0.8.4-real-environment-validation.json`](../benchmarks/experiments/v0.8.4-real-environment-validation.json)：
  V0.8.4 的脱敏机器可读资格汇总，不含模型输出或本机绝对路径。
- [`tasks/v0.8.2-space-aware-environments.md`](tasks/v0.8.2-space-aware-environments.md)：
  解释器发现、任务级环境配方、空间门槛与安全清理实现。
- [`experiments/v0.8.2-real-environment-validation.md`](experiments/v0.8.2-real-environment-validation.md)：
  本轮离线环境与 base/gold 验收的逐题结论和证据边界。
- [`../benchmarks/experiments/v0.5.0-real-task-fixture-validation.json`](../benchmarks/experiments/v0.5.0-real-task-fixture-validation.json)：
  三个真实任务固定提交、补丁可应用性、base 失败/gold 通过和测试环境指纹的机器可读记录。
- [`../benchmarks/experiments/v0.5.0-real-issue-32k-prescreen.json`](../benchmarks/experiments/v0.5.0-real-issue-32k-prescreen.json)：
  不含本机路径和模型正文的预筛选指标与下一功能决策。

机器可读、已脱敏的指标保存在 `benchmarks/baselines/` 和 `benchmarks/experiments/`。
完整工作区、模型消息和轨迹只保存在被 Git 忽略的本机 `runs/` 中。

## 留档规范

从当前版本开始，每项代码任务结束时至少记录：

1. 任务目标、实际改动、关键设计取舍和明确非目标。
2. 涉及的主要模块、公共接口和兼容性影响。
3. pytest、覆盖率、Ruff、compileall 或其他相关验证。
4. 若有实验，记录模型、任务、预算、对照条件、Token、费用、成功率和局限。
5. 对应 commit、tag，以及是否已经推送远程。

代码任务说明进入 `development-history.md` 或单独的 `docs/tasks/` 文档；实验解释进入
`docs/experiments/`，结构化数据进入 `benchmarks/experiments/`。聊天中的结论不能作为
唯一留档。

最新机制预检见 [`experiments/v0.8.20-no-effect-recovery-preflight.md`](experiments/v0.8.20-no-effect-recovery-preflight.md)；全量 457 项测试通过，pytest-only coverage 86.073278%，追加只读诊断入口后的综合 coverage 90.149626%。新门槛默认关闭，脚本化 fixture 不代表真实修复成功率。下一轮 C/T 草案及预算缺口见 [`../benchmarks/experiments/v0.8.20-no-effect-recovery-preflight/next-comparison.json`](../benchmarks/experiments/v0.8.20-no-effect-recovery-preflight/next-comparison.json)。前序证据更正与失败复盘见 [`experiments/v0.8.19-validation-closure-failure-review.md`](experiments/v0.8.19-validation-closure-failure-review.md)。V0.8.18 的原始结果、逐项哈希及后续更正索引分别位于 `benchmarks/experiments/v0.8.18-validation-closure-comparison/` 与 `benchmarks/experiments/v0.8.19-closure-failure-review/`。

最新冻结版本为 [V0.8.21](experiments/v0.8.21-no-effect-recovery-execution.md)：恢复门槛正式路径模拟、完成态恢复及离线 `p2-check` 已通过，执行提交 `adc8658`；48 项真实模型比较尚未运行。共享账本计算余额 ¥89.78873492（不是供应商账单）。全量 pytest 461 passed；Coverage.py 门槛命令通过，但 JSON 精确综合覆盖率为 89.634203%，未取整值低于 90%，报告已披露。
