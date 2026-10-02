# TraceFix 持续维护约定

2026-09-30 当前产品顺序：普通受信任 Python 仓库本地闭环、任务检查点、普通用户路径、可选只读 MCP、普通仓库 Docker、真实模型小批验收及结束前回归门均已交付。PR #9 已合并（merge commit `b18958b8342b0642b7f4fe828c39852ada769319`）；精确 PR head `cb5571815a09f831cde0fd1f648059b6fdff6877` 的 CI #73 全部通过。下一步先对公开任务做任务契约和环境资格筛选，再冻结少量不同类型任务；候选筛选现状见 [`docs/tasks/2026-09-30-public-task-qualification.md`](docs/tasks/2026-09-30-public-task-qualification.md)。当前没有通过资格复验的新多类型任务集，不开展真实模型比较；已用完的八次付费运行额度不续用，后续调用需重新授权。演示与历史报告保留；简历材料和案例包装后置。首批使用说明见 [`docs/ordinary-repository.md`](docs/ordinary-repository.md)，能力边界见 [`docs/reviews/2026-09-28-system-review.md`](docs/reviews/2026-09-28-system-review.md)。下文作品集顺序作为历史记录。

开始工作前阅读 [`docs/roadmap.md`](docs/roadmap.md) 与最近的开发、实验记录。
当前跨会话交接入口：[`docs/handoffs/2026-09-29-agent-harness.md`](docs/handoffs/2026-09-29-agent-harness.md)。历史 V0.8.4 交接保留在 [`docs/handoffs/2026-09-17-v084.md`](docs/handoffs/2026-09-17-v084.md)。

历史约定（2026-09-28 较早版本）：目标为尽快交付可用、可演示、可形成简历亮点的项目，
按 [`docs/tasks/2026-09-28-portfolio-release-plan.md`](docs/tasks/2026-09-28-portfolio-release-plan.md)
优先完成 demo、真实案例、首次使用体验和作品说明。其具体排序已由本文首段更新；
仍不自动恢复旧路线中的历史环境诊断/大规模实验前置链。保留既有工程与证据真实性门槛，
付费调用仍需明确授权。

每次代码任务结束后，记录实际改动、验证结果、限制和下一步；每次实验记录固定代码版本、
任务身份、环境、配置和原始结果位置。合成任务成功率、离线定位指标与真实修复成功率必须
分开表述，并明确哪些结论已经验证、哪些仍是探索性观察。

2026-10-02 当前执行顺序更新：先交付安装包内 `regression-feedback` 入口，再冻结 more-itertools #462 空输入任务并执行资格矩阵；此前 Sphinx 初筛保留为历史，本批不重建环境。所有调用均离线，原八次付费额度不续用。执行记录见 `docs/tasks/2026-10-02-package-and-public-task.md`（文档内路径相对仓库根目录）。

2026-10-02 安装包回归反馈入口已通过 PR #10 精确 CI #77 并合并，主线 `0954c4f`。more-itertools #462 单任务已在本机 3.12 通过完整资格矩阵与生产参考回放；Windows 3.11/3.12 资格以本批 PR 的锁定依赖 CI 产物为准。当前不是多类型任务集，也没有新模型效果证据。下一步增加两种不同缺陷类型，先做契约与资格，再申请新的付费验收。详见 `docs/tasks/2026-10-02-package-and-public-task.md`，原始目录不提交。
