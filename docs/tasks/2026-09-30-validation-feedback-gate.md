# 结束前回归验收与 Agent 反馈闭环

日期：2026-09-30
状态：已合入主线；PR #9 merge commit `b18958b8342b0642b7f4fe828c39852ada769319`，精确 PR head `cb5571815a09f831cde0fd1f648059b6fdff6877` 的 CI #73 全部通过
分支：`codex/gated-regression-feedback`
基线：`bc18a57a8dc16be1c8f2e934ca1dde06b19e07ed`
PR：[#9](https://github.com/wingpeng30/Tracefix/pull/9)

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
- PR #9 首轮 CI（提交 `3194081be1921c6b2f3ea59cac2917159cf7390f`，run `36684436884`）发现旧 checkpoint 直接恢复路径没有结束验收字段，导致完整 pytest `762 passed、1 failed`；覆盖率为语句 `10355/11287`、分支 `3068/3720`，总覆盖率 `13423/15007 = 89.44492570133937%`。修复为门控默认关闭时兼容未经过新 run 初始化的旧 Agent；随后同时修复配置身份比较，使旧 TOML 形状继续按旧哈希核验。
- 修复提交 `d4f44c13107648f336f80d99f342949f04b9d807` 的 CI #67（run `36687781517`，合并检出 `c9e2aea127917bb1ef0e6730246a03eaa122917a`）确认 Windows Python 3.11/3.12 各 `763 passed、0 failed、0 skipped`，但覆盖率为语句 `10368/11288`、分支 `3069/3720`，合计 `13437/15008 = 89.53224946695096%`，因此工程门槛仍失败。失败原因仅为覆盖率；Linux Docker、MCP 与普通 Docker 三个 job 通过，editable/wheel smoke 通过。
- 为填补实际验收分支而新增的 14 项本机定向行为测试覆盖 gate 的空补丁、重复源码状态、无效基线、已存在基线、基线断言失败／超时和安全目标拒绝。测试发现：当前回归目标通过但基线缺失或不完整时，汇总逻辑仍会接受整组任务。已将整组通过判定改为同时要求每项目标状态通过且验收结果为 `passed`、`preserved` 或 `fixed`；任何 `incomplete` 结果均阻断接受。这是本轮新增的正确性修复，不是单纯覆盖率补测。
- 相关本机定向验证 `python -m pytest -q tests/test_runtime.py tests/test_minimal_agent.py -k 'regression_baseline or validation_gate' --disable-warnings --basetemp=tests/.validation-gate-focused-temp`：`14 passed, 64 deselected`。Ruff 首次检查发现新导入顺序并已修正；后续 Ruff 已通过。此前另起的完整本机检查在 35 分钟仍运行时中止，不能作为通过或失败证据；CI #67 的完整 pytest 结果仍有效。
- CI #68（提交 `10776e7df93c8571c6767426016de6cc2e1f02a8`）两个 Windows 版本均再次完整测试通过：Python 3.11/3.12 各 `777 passed、0 failed、0 skipped`；综合覆盖率均为 `13482/15009 = 89.82610433739757%`，低于门槛，故 workflow 失败。三个 Linux Docker/MCP job、editable smoke 与仓库外 wheel smoke 均通过。
- 针对覆盖空缺补充了冻结基线重复/缺失原目标、目标二次检查、checkout 准备失败、pytest 命令策略拒绝、回归配置契约六种无效组合、Agent 验收检查点和测试预算耗尽用例。定向命令 `python -m pytest -q tests/test_runtime.py tests/test_minimal_agent.py -k 'regression_baseline or validation_gate or run_config_validates_regression_contract' --disable-warnings --basetemp=tests/.validation-gate-focused-temp5`：`25 passed, 64 deselected`。Ruff、compileall 与 `git diff --check` 均通过。全量 pytest 覆盖率只会由新提交 CI 确认。
- CI #69（提交 `1094398041519fbb41da7152cca9d0c60bfbcaa5`）两版本均 `777 passed、0 failed、0 skipped`，Ruff、compileall、editable smoke、wheel smoke 及三个 Linux Docker/MCP job 通过；唯一失败是覆盖率 `13502/15009 = 89.95935771870211%`，距离门槛 7 个合并语句／分支点。覆盖报告确认基线循环第二次执行的路径形状与 pytest 策略检查是重复校验，统一前置循环已对原目标和所有回归目标完成相同检查；现保留每个目标实际执行前的路径存在性及 checkout containment 复查，删去不可达的重复策略分支。变更后定向 25 项、Ruff、compileall 与 diff 检查通过，待 CI #70 确认。
- CI #70（提交 `ce61fe41f2546e6e4ff0f47c4b5c1475c6b638b9`）两版本均 `777 passed、0 failed、0 skipped`，Ruff、compileall、editable/wheel smoke 及三个 Linux Docker/MCP job 通过；唯一失败是覆盖率 `13498/15001 = 89.98066795546964%`，距离门槛 3 个合并点。新增门控负例覆盖 pytest 证据缺失和 pytest errors：二者都必须标记为 `incomplete`，不能被当成普通断言失败或通过；本机定向 27 项通过，等待 CI #71。
- CI #71（提交 `e7a675d646d7fb6fd8d4bb83949b71d12caa5d17`）继续确认两版本完整 pytest `777 passed、0 failed、0 skipped`，三个 Linux job 通过，覆盖率仍为 `13498/15001 = 89.98066795546964%`。新增 harness-owned 的真实工具注册路径检查，验证验收测试调用会生成配对工具事件、启动事件、预算计数与无悬空调用；本机定向 28 项、Ruff、compileall 与 diff 检查通过，待 CI #72。
- CI #72（run `36699916374`，精确 PR head `9501ee65c523f129074756f10573c2de518f4479`）五个 job 全部成功。Windows Python 3.11 与 3.12 的完整工程检查均为 `coverage 13501/15001 = 90.00066662222518%`，Ruff、compileall、editable 零调用 smoke 和仓库外 wheel smoke 通过；Linux 冻结 Docker、普通 Docker、Serena MCP 三个零调用 job 通过。工程制品：3.11 `sha256:2364132e18b3c95247ad98cc4728aa495f482bbb0c923f6d53c4706a7806a999`，3.12 `sha256:1b5f42af1a593f9863241b0809066bdec9d9f4142f16de902c4574091c0178d7`；MCP `sha256:c6e195af72a9d5513b8452b09de5c76023b8deda71cf28337976f1d0cb66aba7`；普通 Docker `sha256:25b538610f091ec876be95971d7732500fc73eb3d2d6a61a5c0bcdba5efaf4f7`；冻结 Docker `sha256:93e853a665c19f19a6cf17e5f1820d1ab2ceff7c8df26773da40dde1df71ec5c`。审查列表和行内讨论为空，PR 可合并。
- Ruff 对变更的源码、示例和测试文件通过；`compileall` 对源码与示例通过；`git diff --check` 通过；最新录制闭环回放退出码 0。
- 首轮 CI 中 Linux `docker-zero-call`、`mcp-serena-zero-call`、`docker-ordinary-zero-call` 均通过；Windows 3.11/3.12 工程步骤因同一恢复测试失败而未通过，wheel smoke 步骤也返回失败，需在修复后重新核验其具体结果。Artifact：3.11 `sha256:83af7a7695df3179611628ec5e3ab0f5e76e3c0c93d26728ac3545498da50214`，3.12 `sha256:a5ab860edf20b5d0146b1bfb85eb6a0535366a97b2cb92ca5808d2e07d5af169`。本机原始复现 `runs/ci-repro-20260930/summary.json` 与 `pytest.log`；pytest log SHA-256 `16a6e3280cc9a5875b3c198f9476eaf91f78f6502cc6b37e19a64d3a8b2e9468`。

## 已知范围

首版仅支持本地 pytest 运行，不支持 Docker 验收门。回归目标只表示用户明确列出的覆盖范围。原始源码已有失败的追加目标必须被修复，报告将其标为“原有失败”，而非新回归。首次预算要求至少为 `2 × 追加目标数 + 1` 个 pytest 进程；超出部分留给 Agent。测试结果不完整时不会显示通过。

## 后续

（历史计划，已完成）重跑相关 Ruff、compileall、diff 与定向测试，补充正确性修复及回归用例，并通过 PR #9 的双版本覆盖和 Linux CI 后合入主线。

当前后续是公开任务资格筛选，而不是直接启动新一轮模型评估。候选、环境风险和冻结标准见 [`2026-09-30-public-task-qualification.md`](2026-09-30-public-task-qualification.md)。真实模型实验需新的次数与金额授权。
