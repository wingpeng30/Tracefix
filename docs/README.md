# TraceFix 文档索引

本目录保存代码演进、实验解释和后续任务记录。README 首页只保留当前使用方法与主要结果，
完整的历史和证据从这里进入。

## 新任务交接入口

- [`handoffs/2026-09-17-v084.md`](handoffs/2026-09-17-v084.md)：当前版本、优先级任务、
  必读代码、原始证据位置和可复制启动提示。最新状态见文末第 25 节：V0.8.13 诊断已更正、输出呈现单变量协议已冻结并完成合成零费用预检；完整付费批次因共享余额不足而推迟。

## 项目历程

- [`roadmap.md`](roadmap.md)：项目目标、当前阶段、验收门槛与下一步行动。
- [`development-history.md`](development-history.md)：从 V0 接口骨架到 V0.3.2 实验可追溯性
  A/B 的代码说明、设计取舍、验证结果和 Git 版本。

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
