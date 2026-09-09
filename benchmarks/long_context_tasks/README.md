# 长上下文压力任务

该目录包含两道专门用于验证上下文折叠机制的合成任务，不用于代表真实 GitHub Issue
解决率。每题在准备 Git 源仓库时，通过 `task.json` 中受限的声明式配置生成 24 份契约
文档；生成器只写文本并替换固定占位符，不执行任务目录中的代码。

任务要求 Agent 分六轮、每轮最多读取四份契约。完整读取产生的历史在默认估算器下自然
超过 32k 软阈值，同时保留真实 Coding Agent 的“读契约—定位实现—修改—测试—检查
Diff”流程。单元测试会验证：

- 每题初始测试失败，应用 gold patch 后通过；
- 每题恰好生成 24 份大于 6 KB 的契约；
- 按任务要求分批读取后，默认 `ContextManager` 确实触发 32k 历史折叠。

对照组与实验组必须使用同一个 TraceFix commit、模型、任务、预算和文档生成配置：

```powershell
# 对照组：完全关闭上下文裁剪和折叠
tracefix eval `
  --tasks benchmarks/long_context_tasks `
  --no-context-compaction `
  --max-steps 20 `
  --max-input-tokens 400000 `
  --max-output-tokens 16000 `
  --wall-time-seconds 600 `
  --max-test-runs 4

# 实验组：生产默认 32k 软阈值
tracefix eval `
  --tasks benchmarks/long_context_tasks `
  --context-trigger-tokens 32000 `
  --context-retain-ratio 0.375 `
  --max-steps 20 `
  --max-input-tokens 400000 `
  --max-output-tokens 16000 `
  --wall-time-seconds 600 `
  --max-test-runs 4
```

这一压力任务仍有明确的人为控制条件，主要回答“折叠发生时能否减少实际 Token 并保持
任务闭环”，不能替代后续真实仓库或 SWE-bench 评测。
