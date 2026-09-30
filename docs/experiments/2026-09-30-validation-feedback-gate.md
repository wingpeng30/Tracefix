# 回归验收反馈闭环离线实验

日期：2026-09-30
代码基线：`bc18a57a8dc16be1c8f2e934ca1dde06b19e07ed`
实现分支：`codex/gated-regression-feedback`
供应商请求：0；真实模型调用：否

## 场景

使用普通仓库示例和录制的 LiteLLM 形状响应。第一版补丁满足原任务目标，但破坏两个追加回归目标。Harness 在模型结束时自动测试、生成与原始源码基线的对比，将两个失败目标及证据位置送回 Agent；录制的下一轮修改修复失败。最后在新 checkout 复跑原目标与两个追加目标。

运行命令：

```powershell
python examples/replay_ordinary.py --validation-gate-example --output runs/validation-gate-replay-20260930-final
```

## 结果

- Agent 逻辑调用数：4 个录制模型响应；无供应商请求。
- 第一版触发追加目标失败，并向后续模型请求提供失败反馈。
- 第二版通过原目标与两个追加目标。
- 新 checkout 独立验证通过；源仓库状态干净。
- 原始 `result.json`、轨迹、补丁及报告位于 `runs/validation-gate-replay-20260930-final/runs/20260930T072948Z-16743602/`。该目录为本地运行证据，不提交。

## 自动化验证

- 端到端反馈及独立验证测试：1 passed；最终联测与 outcome 分类测试：2 passed。
- 涉及运行、doctor、CLI、回归和报告的相关测试：首轮 89 passed、1 failed。已根据失败修复显式重复追加目标被静默合并的问题。
- 修复后重复目标结果分类测试：1 passed。
- 首轮 GitHub CI（run `36684436884`，commit `3194081be1921c6b2f3ea59cac2917159cf7390f`）发现旧 checkpoint 恢复没有新门控运行态字段，完整 pytest 为 762 passed、1 failed，整体覆盖率为 89.44492570133937%。本机完整脚本复现的 pytest log SHA-256 为 `16a6e3280cc9a5875b3c198f9476eaf91f78f6502cc6b37e19a64d3a8b2e9468`，摘要位于 `runs/ci-repro-20260930/summary.json`。已修复旧恢复路径和配置身份兼容。
- 修复后端到端、旧 checkpoint、追加验证 outcome 联测为 3 passed in 112.00s；完整覆盖率与 wheel 需等待修复提交后的 CI。
- 变更文件 Ruff、源码和示例 `compileall`、`git diff --check` 通过；最新离线闭环命令退出码 0。
- PR 全量覆盖率门槛与跨平台 CI 尚未完成，不能记作通过。

## 结论边界

该回放验证控制流能发现“原目标已通过但补丁破坏其他目标”的情况、形成反馈并允许第二版补丁修复。录制模型没有自主生成补丁，因此不构成真实模型修复成功率证据；追加目标以外的行为也不在结论范围内。
