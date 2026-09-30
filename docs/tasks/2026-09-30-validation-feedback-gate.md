# 结束前回归验收与 Agent 反馈闭环

日期：2026-09-30
状态：实现完成，待 PR 审查及 CI
分支：`codex/gated-regression-feedback`
基线：`bc18a57a8dc16be1c8f2e934ca1dde06b19e07ed`

## 目标与边界

允许用户为普通本地 Python/pytest 任务声明追加回归目标。模型提出结束后，TraceFix 在同一任务预算内运行原目标和所有追加目标；失败证据返回给 Agent 继续修复，通过后再由新 checkout 独立复验。此机制默认关闭。本批仅验证功能、状态和证据闭环，不声称它提高总体修复成功率；未调用供应商、不访问留出题。

## 实现内容

- `run`、`doctor` 和 TOML 支持重复声明回归目标。CLI 声明整体覆盖 TOML；目标进入配置及恢复身份。启用目标时要求本地后端、原测试目标和源码导入探针。
- Runner 在构造模型前检查预算，并在独立冻结源码 checkout 对每个目标记录基线。原有断言失败可以继续；收集、导入、超时和身份问题会阻断启动并保存诊断。
- Agent 只在模型提出结束时执行一次整组测试；自动测试作为 `origin=validation_gate` 工具事件配对并计入测试次数和活动时间。通过后接受暂存答复；失败摘要和证据位置交回下一轮模型。相同源码不重复执行，改动后整组结果失效。
- checkpoint 保存验收状态，恢复前校验基线来源与契约身份。`verify` 自动合并运行中保存的目标。报告区分 Agent 内验收与独立 checkout 复验，并展示覆盖目标。
- 新增 `examples/replay_ordinary.py --validation-gate-example`，以录制响应演示第一版通过原目标但引入回归，第二版收到反馈并修复，再完成独立复验。该结果是离线控制流验证，不是真实模型效果证据。

## 离线验证记录

原始运行产物位于本机 `runs/validation-gate-replay-20260930-final/`，不纳入 Git。录制模型返回 4 次请求；Agent 状态为 `completed`；原仓库保持干净；原目标和两个追加目标全部通过；独立验证状态为 `passed`。`result.json` 与 HTML 报告位于 `runs/20260930T072948Z-16743602/`。

行为测试：

- `tests/test_runtime.py::test_end_of_task_regression_gate_feedback_repairs_before_independent_verify`：1 passed；最终联测与分类用例 2 passed。
- 相关回归、运行、CLI、doctor、报告测试合计首轮 89 passed、1 failed。失败指出显式重复目标曾被新逻辑静默去重，现已修复为拒绝重复目标；针对性复验 `tests/test_regression.py::test_regression_verification_preserves_distinct_outcomes`：1 passed。
- Ruff 对变更的源码、示例和测试文件通过；`compileall` 对源码与示例通过；`git diff --check` 通过；最新录制闭环回放退出码 0。
- 全量纯 pytest 覆盖率及 Windows 双版本、wheel、Linux Docker CI 尚待 PR CI 验证。

## 已知范围

首版仅支持本地 pytest 运行，不支持 Docker 验收门。回归目标只表示用户明确列出的覆盖范围。原始源码已有失败的追加目标必须被修复，报告将其标为“原有失败”，而非新回归。首次预算要求至少为 `2 × 追加目标数 + 1` 个 pytest 进程；超出部分留给 Agent。测试结果不完整时不会显示通过。

## 后续

完成完整相关测试和 PR CI，核对报告、恢复和预算边界；审查通过后按计划合入主线。之后冻结少量不同类型的公开任务，以失败分类决定下一项改进。真实模型实验需新的次数与金额授权。
