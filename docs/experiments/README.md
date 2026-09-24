# TraceFix 实验索引与结论

本页用于快速找到每次实验的结构化数据、解释和正确结论边界。早期美元费用按记录中的固定汇率
估算人民币；新 CNY 账本直接使用人民币单价，不进行隐式换汇。计算费用不代表供应商实际扣费。

## 实验总览

| 实验 | 任务 | Resolved | 输入 Token | 步骤 | 费用（人民币估算） | 正确用途 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| V0.2.0 Baseline | 10 个短合成任务 | 10/10 | 184,730 | 74 | ¥0.18140797 | 最初无压缩基线 |
| V0.3.0 3k 压力实验 | 相同 10 题 | 7/10 | 203,390 | 84 | ¥0.25072877 | 失败发现，不是生产结论 |
| V0.3.1 Agent Loop 回归 | 相同 10 题 | 10/10 | 110,060 | 53 | ¥0.12353363 | 验证补丁和收尾修复 |
| V0.3.1 32k A/B 对照组 | 2 个长上下文任务 | 2/2 | 370,387 | 24 | ¥0.25246310 | 完全关闭压缩 |
| V0.3.1 32k A/B 实验组 | 相同 2 题 | 2/2 | 185,587 | 22 | ¥0.22804456 | 生产阈值机制验证 |
| V0.3.2 夹具验收 | 4 个多文件任务 | 4/4 gold | 0 | 0 | ¥0 | 仅验证任务定义自洽 |
| V0.4.0 配对对照组 | 4 题 × 3 次 | 12/12 | 254,975 | 68 | ¥0.37364763 | 关闭压缩 |
| V0.4.0 配对 32k 组 | 相同 4 题 × 3 次 | 12/12 | 265,145 | 70 | ¥0.38225877 | 0 次折叠，不构成压缩证据 |
| V0.8.2 环境资格验收 | 12 个真实候选 | 2/12 合格 | 0 | 0 | ¥0 | 仅环境/base-gold 资格，不是 Agent 实验 |
| V0.8.3 环境资格重验 | 12 个真实候选 | 7/12 合格 | 0 | 0 | ¥0 | 审计 node ID 与环境版本后得到的任务资格 |

## V0.2.0 Baseline

- 数据：[`../../benchmarks/baselines/v0.2.0.json`](../../benchmarks/baselines/v0.2.0.json)
- 条件：`deepseek/deepseek-v4-flash`，10 个短合成任务，每题最多 12 步、40k 累计输入
  Token、8k 输出 Token、5 分钟和 4 次 Agent 测试。
- 结果：10/10，输入 184,730，输出 8,982，74 步，87 次工具调用，其中 6 次完全重复。
- 解释：证明 V0.2 最小闭环能够运行并解决简单任务，但不能代表真实 Issue 或 SWE-bench。

## V0.3.0：3k 压力实验

- 数据：[`../../benchmarks/experiments/v0.3.0-context-3k.json`](../../benchmarks/experiments/v0.3.0-context-3k.json)
- 解释：[`v0.3.0-context-3k.md`](v0.3.0-context-3k.md)
- 结果：7/10；输入和费用均高于 Baseline，完全重复调用升至 23 次。
- 关键发现：三个失败任务在第一次历史折叠前已经出现补丁失败。模型提出的修复方向基本正确，
  但工具不接受 Begin Patch 或错误 hunk 行数，之后缺少重复失败护栏。
- 结论：这不是“上下文压缩一定有害”的证据，而是一次成功发现工具协议缺陷的压力实验。
  3k 仍不应作为生产默认阈值。

## V0.3.1：Agent Loop 回归

- 数据：[`../../benchmarks/experiments/v0.3.1-agent-loop-regression.json`](../../benchmarks/experiments/v0.3.1-agent-loop-regression.json)
- 解释：[`v0.3.1-agent-loop-regression.md`](v0.3.1-agent-loop-regression.md)
- 结果：10/10，10 个任务均正常完成；输入 110,060，53 步，66 次工具调用，无失败、无重复。
- 关键证据：模型 10 次都使用 Begin Patch，新工具 10 次均首次应用成功；测试和 Diff 完成后，
  10 次收尾提示均让 Agent 正常结束。
- 结论：相较 V0.2 输入降低 40.42%，主要原因是失败重试和多余尾部步骤消失。本轮压缩次数为
  零，不能将 Token 降幅归因于上下文压缩。

## V0.3.1：关闭压缩 vs 32k 压缩

- 数据：[`../../benchmarks/experiments/v0.3.1-context-32k-ab.json`](../../benchmarks/experiments/v0.3.1-context-32k-ab.json)
- 解释：[`v0.3.1-context-32k-ab.md`](v0.3.1-context-32k-ab.md)
- 条件：同一 commit、模型、两道长上下文任务和预算；唯一配置差异是完全关闭压缩与启用
  32k 软阈值、37.5% 近期保留比例。
- 结果：两组均为 2/2；实验组输入从 370,387 降至 185,587（-49.89%），费用降低 9.67%。
- 强配对证据：`long_policy_window` 两组都完整读取相同 24 份契约，实验组还多一步，但经过
  5 次历史折叠后输入仍降低 59.69%。
- 混杂因素：另一题未发生历史折叠，只发生工具结果裁剪，且实验组少三步；该题的下降不能
  完全归因于历史折叠。
- 结论：结果支持 32k 确定性压缩可以在保持这两题成功率时显著减少长轨迹输入 Token，但只
  能作为机制验证。两题、每组一次的结果不能外推为真实 Issue 平均收益。

## 指标解释注意事项

V0.8.2 的真实候选环境与行为资格审查见
[`v0.8.2-real-environment-validation.md`](v0.8.2-real-environment-validation.md)。其中 2/12
表示“base 断言失败且 gold 同入口通过”的离线任务资格数；它不等于模型 resolved rate，也没有
调用 LLM。

V0.8.3 的重验结果见
[`v0.8.3-real-environment-validation.md`](v0.8.3-real-environment-validation.md)。7/12 表示可进入
未来付费实验的任务数，不表示 Agent 已解决 7 道真实 Issue。

V0.4.0 的 24 次交替配对实验见
[`v0.4.0-paired-context-32k.md`](v0.4.0-paired-context-32k.md)。两组均 12/12，但所有任务
都未达到 32k，必须作为“未触发机制”的否定性结果报告，不能用于宣传压缩收益。

V0.3.2 多文件任务的离线验收数据见
[`../../benchmarks/experiments/v0.3.2-fixture-validation.json`](../../benchmarks/experiments/v0.3.2-fixture-validation.json)，
解释见 [`v0.3.2-fixture-validation.md`](v0.3.2-fixture-validation.md)。这里的 4/4 指标准补丁
通过公开与隐藏测试，不是 Agent resolved rate。

- `input_tokens` 是每次模型请求完整上下文的累计值，不只是原始题目长度。
- `estimated_tokens_saved` 来自本地估算器，只用于压缩决策，不能替代供应商 usage。
- `tool_results_pruned` 是每次请求中执行裁剪的累计操作次数，不是唯一消息数量。
- Token 降幅不一定与费用同比；现有记录不能分解缓存命中、缓存未命中和不同 Token 类型价格。
- `resolved` 来自 Agent 外独立 pytest；Agent 的 `completed` 状态应单独观察。
- 所有当前任务均为合成任务，不能表述为真实 GitHub Issue 或 SWE-bench 解决率。

## P2 正式重跑准备（2026-09-21）

- 执行代码：`e464f9b`。
- 账单：仅筛选 `Tracefix`，历史供应商账单合计人民币 3.24553320 元；原始账单不提交。
- 真实探测：`deepseek-flash`，20 输入、4 输出 Token；共享账本累计人民币 3.24560520 元。
- 工程门槛：276 passed，综合覆盖率 90.06228373702422%；Ruff、compileall 通过。
- 正式 60 项尚未启动。自动审批要求用户明确授权将任务、源码片段与工具输出发送给 DeepSeek。

用户授权后正式重跑已完成。脱敏结果见
[`../../benchmarks/experiments/v0.8.6-p2-formal-rerun.json`](../../benchmarks/experiments/v0.8.6-p2-formal-rerun.json)，
解释见 [`v0.8.6-p2-formal-rerun.md`](v0.8.6-p2-formal-rerun.md)。C 成功 8/30、T 成功 7/30；
T 输入 -2.73%、Agent 用时 -6.14%，但成功率 -3.33pp，因此尚未达到长期目标中的成功率约束。

## 四组消融零费用预检（2026-09-22）

[`v0.8.10-ablation-preflight.md`](v0.8.10-ablation-preflight.md) 记录四组开发集 120 项确定性日程、
20 题留出证据复核、完整依赖锁及预算。真实任务无答案模拟 120/120 证据有效，正式入口合成模拟
120/120 fixture 通过，两类恢复均不新增调用；全部仅作为工程验证。首批配置错误和更正记录保留。
最终 335 passed、综合覆盖率 90.98527475158626%，Ruff/compileall 通过。共享原 ¥100 预算剩余
¥70.70445880，无法覆盖开发集固定预算的 ¥103.20 保守上界；付费阶段尚未启动。

## V0.8.11：开发集四组正式消融

- 解释：[`v0.8.11-development-ablation.md`](v0.8.11-development-ablation.md)。
- 数据：[`../../benchmarks/experiments/v0.8.11-development-ablation/formal-results.json`](../../benchmarks/experiments/v0.8.11-development-ablation/formal-results.json)。
- 执行代码 `a8309eb`，120/120 完成且证据有效，48 成功、72 失败，基础设施与证据异常均零；恢复零新调用。
- 普通资格 8 题：基线 13/24、Repo Map 10/24、行动 11/24、上下文 14/24；特殊资格 2 题各组 0/6。
- 行动输入 -35.77%，但成功率 -8.33 个百分点，Agent 用时 +0.50%。无组满足预定规则，保留基线。
- 历史折叠 0；上下文工具裁剪在 18/30 项发生，不能将两个机制混为“未触发”。20 题留出集未评测。
- 本轮保守计算 ¥51.073224，共享累计 ¥80.3687652/100；不是供应商实际账单，未新增预算。
- 最终代码全量 391 passed，综合覆盖率 91.00807867931155%，Ruff/compileall 通过。下一步优先离线
  检查配对失败和上下文阶段，不直接从小样本开发结果宣称泛化或非劣。

## V0.8.12：开发集离线详细诊断

- 报告：[`v0.8.12-offline-detailed-ablation-diagnostic.md`](v0.8.12-offline-detailed-ablation-diagnostic.md)。
- 脱敏机器结果：[`../../benchmarks/experiments/v0.8.11-development-ablation/detailed-diagnostic-20260923-final-v4/detailed-diagnostic.json`](../../benchmarks/experiments/v0.8.11-development-ablation/detailed-diagnostic-20260923-final-v4/detailed-diagnostic.json)。
- 配对索引：[`../../benchmarks/experiments/v0.8.11-development-ablation/detailed-diagnostic-20260923-final-v4/paired-cases.json`](../../benchmarks/experiments/v0.8.11-development-ablation/detailed-diagnostic-20260923-final-v4/paired-cases.json)。
- 120/120 试次、2,094 响应、120 轨迹和 480 试次工件哈希通过复核；普通集 24 对为 5 退步、3 反向、16 同结果。
- 未证明可复现实现缺陷；不改算法。下一开发集仅验证工具输出呈现的单变量假设，20 题留出集未使用。
- 全量 401 passed；综合覆盖率 90.22915340547422%（语句 92.5399889685604%、分支 82.52069917203312%），Ruff/compileall 通过。原始日志、JUnit 与 coverage JSON/XML 留在本机 `runs/detailed-ablation-verification-20260923/`。


## V0.8.13：工具输出呈现单变量预检与诊断更正

- 报告：[`v0.8.13-presentation-only-preflight.md`](v0.8.13-presentation-only-preflight.md)。
- 脱敏协议、完整 60 位置日程、校正后的诊断和哈希索引：`../../benchmarks/experiments/v0.8.13-presentation-only-preflight/`。
- 单变量 C/T 配置冻结于提交 `8954d77860aa17dfdc579c40ed14f50ef7fb2021`；仅工具输出呈现开关不同，未执行付费批次。
- 合成 fixture Agent/独立验收 6/6 通过并完成无调用恢复；这仅是工程证据。20 题留出评测未运行。
- 共享余额 ¥19.63123480，完整 60 位置的保守上界 ¥51.60；预算不足，继续暂停付费运行。

## V0.8.14：工具输出呈现单变量开发集正式比较

- 报告：[`v0.8.14-presentation-only-paid-development.md`](v0.8.14-presentation-only-paid-development.md)。
- 脱敏 60 项汇总、逐位置指标及工件哈希：[`report.json`](../../benchmarks/experiments/v0.8.14-presentation-only-paid-development/report.json) 与 [`sha256-manifest.json`](../../benchmarks/experiments/v0.8.14-presentation-only-paid-development/sha256-manifest.json)。
- 用户授权将共享累计上限提高至 ¥150、本阶段上限设为 ¥55；同一账本下完成 60/60 个试次记录，阶段保守计算 ¥26.177326，共享累计 ¥106.54609120。以上金额不是供应商账单。
- 8 道普通题 C/T 均 12/24；T 输入 Token −10.51%，Agent 时间 +18.83%。2 道特殊资格任务两组均 0/6。证据审计仅 58/60 有效，2 项复验仍不完整；不采纳 T，保留基线。
- 同目录恢复复用 60/60；留出集未运行。全量工程门槛引用运行前执行代码 `e5ab04b` 的验证证据；本轮未改运行代码。
