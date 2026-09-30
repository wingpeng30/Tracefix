# TraceFix 持续维护约定

2026-09-30 当前产品顺序：普通受信任 Python 仓库的本地闭环、任务检查点、普通用户路径、可选只读 MCP、普通仓库 Docker，以及 8 次真实模型小批验收已交付。当前分支 `codex/gated-regression-feedback` 正在实现结束前回归验收与 Agent 反馈闭环；状态及验证见 [`docs/tasks/2026-09-30-validation-feedback-gate.md`](docs/tasks/2026-09-30-validation-feedback-gate.md)。审查和 CI 通过后，将此能力合入主线，再依据失败证据挑选不同类型的公开任务。演示与历史报告保留；简历材料和案例包装后置。首批使用说明见 [`docs/ordinary-repository.md`](docs/ordinary-repository.md)，能力边界见 [`docs/reviews/2026-09-28-system-review.md`](docs/reviews/2026-09-28-system-review.md)。下文作品集顺序作为历史记录。

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
