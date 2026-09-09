# TraceFix Baselines

这里保存可提交、可复核的脱敏实验汇总。完整工作区、模型原始响应和 JSONL 轨迹仍位于
本机 `runs/`，不会进入 Git。

`v0.2.0.json` 是未启用上下文压缩时的对照组。它使用同一模型、同一组 10 个合成
Python Bug 任务和统一预算，后续实验必须保持这些条件不变。合成任务规模很小，因此
该解决率不能代表真实 GitHub Issue 或 SWE-bench 表现。

字段 `exact_duplicate_tool_calls` 只统计工具名和 JSON 参数完全一致的重复调用，不把
语义相似但参数不同的调用判定为重复。

压力实验不属于 Baseline，统一保存在 `benchmarks/experiments/`。V0.3.0 的 3k 压缩实验
及其失败分析见 `docs/experiments/v0.3.0-context-3k.md`。

`v0.3.1-agent-loop-regression.json` 固化补丁协议与收尾修复后的 10 题回归结果。该轮没有
实际触发上下文折叠，因此只作为 Agent Loop 回归证据，不作为压缩效果证据。

`v0.3.1-context-32k-ab.json` 保存两道长上下文任务上“完全关闭压缩 vs 32k 压缩”的同条件
对照结果。它用于验证压缩机制，不能替代真实 Issue 评测。
