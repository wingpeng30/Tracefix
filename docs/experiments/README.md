# TraceFix 实验索引与结论

本页用于快速找到每次实验的结构化数据、解释和正确结论边界。所有人民币金额使用实验记录中
保存的固定汇率估算，不代表结算金额。

## 实验总览

| 实验 | 任务 | Resolved | 输入 Token | 步骤 | 费用（人民币估算） | 正确用途 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| V0.2.0 Baseline | 10 个短合成任务 | 10/10 | 184,730 | 74 | ¥0.18140797 | 最初无压缩基线 |
| V0.3.0 3k 压力实验 | 相同 10 题 | 7/10 | 203,390 | 84 | ¥0.25072877 | 失败发现，不是生产结论 |
| V0.3.1 Agent Loop 回归 | 相同 10 题 | 10/10 | 110,060 | 53 | ¥0.12353363 | 验证补丁和收尾修复 |
| V0.3.1 32k A/B 对照组 | 2 个长上下文任务 | 2/2 | 370,387 | 24 | ¥0.25246310 | 完全关闭压缩 |
| V0.3.1 32k A/B 实验组 | 相同 2 题 | 2/2 | 185,587 | 22 | ¥0.22804456 | 生产阈值机制验证 |
| V0.3.2 夹具验收 | 4 个多文件任务 | 4/4 gold | 0 | 0 | ¥0 | 仅验证任务定义自洽 |

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
