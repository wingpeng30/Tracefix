# TraceFix Baselines

这里保存可提交、可复核的脱敏实验汇总。完整工作区、模型原始响应和 JSONL 轨迹仍位于
本机 `runs/`，不会进入 Git。

`v0.2.0.json` 是未启用上下文压缩时的对照组。它使用同一模型、同一组 10 个合成
Python Bug 任务和统一预算，后续实验必须保持这些条件不变。合成任务规模很小，因此
该解决率不能代表真实 GitHub Issue 或 SWE-bench 表现。

字段 `exact_duplicate_tool_calls` 只统计工具名和 JSON 参数完全一致的重复调用，不把
语义相似但参数不同的调用判定为重复。
