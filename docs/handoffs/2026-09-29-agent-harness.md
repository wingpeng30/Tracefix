# 2026-09-29 Agent/harness 产品交接

## 2026-10-02 预算调查交接

当前新增离线调查入口 `scripts/analyze_live_history.py`：配对 27 个完成请求与一个未发送视图，并用真实工具验证补读、缓存失效、压缩和验收反馈。两个候选未达到采用标准；下一批只优先完善定向读取可见性。原账本及四次结果保持不变，不执行第五次。调查及复现命令见 [`../experiments/2026-10-02-offline-budget-investigation.md`](../experiments/2026-10-02-offline-budget-investigation.md)，历史交接保留。

## 2026-10-02 最新交接

三类公开任务资格已完成，PR #12 merge `6b5af67`、PR #13 merge `47f2619`；后者精确 head `351b600` 的 CI #88 全部通过，Windows 3.11/3.12 各 820 tests、零 skips，纯 pytest 覆盖率 90.0502446%。任务失败、纠正参考、原始记录哈希和下一轮未授权五次／五元方案见 [`../tasks/2026-10-02-three-public-task-types.md`](../tasks/2026-10-02-three-public-task-types.md)。不要重用已耗尽八次授权，不要将参考回放称为模型自主修复。历史未跟踪材料未提交或清理。

## 2026-09-30 当前状态

PR #9 已于 2026-09-30 合并：merge commit `b18958b8342b0642b7f4fe828c39852ada769319`，PR head `cb5571815a09f831cde0fd1f648059b6fdff6877` 的 CI #73 成功。当前后续从任务契约资格开始：本轮只读筛选优先预检 Sphinx-10323，暂缓有 HTTP 服务依赖/目标不一致风险的 Requests-1724 和有基线收集依赖的 Pylint-4604；目前没有多类型任务集通过新的 base/gold 复验。详情、SHA 和停止条件见 [`../tasks/2026-09-30-public-task-qualification.md`](../tasks/2026-09-30-public-task-qualification.md)。不要将筛选写成任务资格通过或模型效果证据；供应商调用需重新授权。

2026-09-30 PR #9（`codex/gated-regression-feedback`）CI #72 在精确 head `9501ee65c523f129074756f10573c2de518f4479` 全部通过：Windows Python 3.11/3.12 覆盖率均为 90.0006666%，Ruff、compileall、editable/wheel smoke、Linux 冻结 Docker/普通 Docker/Serena MCP 均通过；没有未解决审查意见。新路径增加 pytest 证据缺失/errors fail-closed 断言及 harness-owned 测试调用事件与预算计数验证。离线示例：`python examples/replay_ordinary.py --validation-gate-example --output runs/validation-gate-replay`。完整状态和证据见 [`../tasks/2026-09-30-validation-feedback-gate.md`](../tasks/2026-09-30-validation-feedback-gate.md)。

2026-09-30 更新：追加回归目标验证与 Boltons 派生审计见 [`../experiments/2026-09-30-regression-verification.md`](../experiments/2026-09-30-regression-verification.md)。普通本地 `verify` 可对冻结源码和保存补丁运行相同的已有 pytest 目标，识别回归、修复、仍失败及证据不完整；Docker 的追加目标尚未接入。提交 `a6609c9` 的 [CI #63](https://github.com/wingpeng30/Tracefix/actions/runs/36672415601) 五个 job 均通过。该批次独立于下述 2026-09-29 主线基线。

当前路线以 [`../tasks/2026-09-29-agent-harness-completion.md`](../tasks/2026-09-29-agent-harness-completion.md) 为准。[#1](https://github.com/wingpeng30/Tracefix/pull/1) 普通仓库首批、[#2](https://github.com/wingpeng30/Tracefix/pull/2) A checkpoint／恢复、[#3](https://github.com/wingpeng30/Tracefix/pull/3) B 预检与独立验证、[#4](https://github.com/wingpeng30/Tracefix/pull/4) C Serena MCP、[#5](https://github.com/wingpeng30/Tracefix/pull/5) D 普通仓库 Docker、[#6](https://github.com/wingpeng30/Tracefix/pull/6) 真实模型验收与修复已按序以 merge commit 合入 `main`，主线提交 `7a28687f897821b9600cc4a9e8bf8da583ec7d45`；各原提交保留。

A 的 [CI #25](https://github.com/wingpeng30/Tracefix/actions/runs/36532867162) 成功；B 修复后的 [CI #27](https://github.com/wingpeng30/Tracefix/actions/runs/36539596976) 成功。C 的 [CI #33](https://github.com/wingpeng30/Tracefix/actions/runs/36545042878) 全部 job 成功，Linux 原始 MCP 身份与结果见 [`../experiments/2026-09-29-serena-mcp-zero-call.md`](../experiments/2026-09-29-serena-mcp-zero-call.md)。D 的 [CI #38](https://github.com/wingpeng30/Tracefix/actions/runs/36549319954) Linux 普通仓库 job 成功，两次运行、新容器复验、超时／断连／导入错误与清理证据见 [`../experiments/2026-09-29-ordinary-docker-zero-call.md`](../experiments/2026-09-29-ordinary-docker-zero-call.md)。D 代码提交 `91bad0094c1738c76d68a62063b1778511e6c320` 的 [CI #43](https://github.com/wingpeng30/Tracefix/actions/runs/36551240955) 五个 job 全部成功：Windows Python 3.11／3.12 各 750 tests、0 failures／errors／skips，纯 pytest 语句与分支覆盖率均为 12828/14252 = 90.00841987089531%；Ruff、compileall、Diff、editable 和仓库外 wheel 检查通过，Linux 冻结 Docker、普通 Docker 与 Serena MCP job 通过。

2026-09-29 小批付费验收已执行 8/8 次、37 个已完成模型请求，无未决请求；保守高峰价成本 ¥0.33653，见 [`../experiments/2026-09-29-live-model-acceptance.md`](../experiments/2026-09-29-live-model-acceptance.md)。这不能推断总体修复成功率。不访问留出题，不改历史账本、评分或原始证据。Windows Docker Desktop 本机 daemon 不可用；既有冻结 Docker 流程、C 的隔离查询和 D 普通 Docker 由 Linux CI 验证。

D 已提供普通仓库独立 Docker profile，预构建镜像和依赖身份、无网络／非 root／只读根文件系统的容器、新容器独立验证，以及 Linux CI 的重复运行与故障清理。首版不支持恢复丢失容器；Windows Docker Desktop 未实测。PR #6 最新提交的 [CI #51](https://github.com/wingpeng30/Tracefix/actions/runs/36590277947) 五个 job 已通过。下一步依真实失败分布提高普通任务可靠性，优先审查外部任务的 API 兼容边界，再决定是否扩展实验；20 道留出题继续保持未使用。

2026-10-02 当前执行顺序更新：先交付安装包内 `regression-feedback` 入口，再冻结 more-itertools #462 空输入任务并执行资格矩阵；此前 Sphinx 初筛保留为历史，本批不重建环境。所有调用均离线，原八次付费额度不续用。执行记录见 `docs/tasks/2026-10-02-package-and-public-task.md`（文档内路径相对仓库根目录）。

2026-10-02 安装包回归反馈入口已通过 PR #10 精确 CI #77 并合并，主线 `0954c4f`。more-itertools #462 单任务已在本机 3.12 通过完整资格矩阵与生产参考回放；Windows 3.11/3.12 资格以本批 PR 的锁定依赖 CI 产物为准。当前不是多类型任务集，也没有新模型效果证据。下一步增加两种不同缺陷类型，先做契约与资格，再申请新的付费验收。详见 `docs/tasks/2026-10-02-package-and-public-task.md`，原始目录不提交。
# 2026-10-02 public task expansion

Continue from [the three-task execution record](../tasks/2026-10-02-three-public-task-types.md).
The user approved separately hashed corrections to the two upstream references;
do not describe those corrected patches as the original upstream commits.
Qualification remains zero-call and must precede a fresh experiment authorization.
