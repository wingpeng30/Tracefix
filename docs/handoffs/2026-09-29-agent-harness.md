# 2026-09-29 Agent/harness 产品交接

当前路线以 [`../tasks/2026-09-29-agent-harness-completion.md`](../tasks/2026-09-29-agent-harness-completion.md) 为准。原 PR #1 保存普通仓库首批，CI 通过但相对旧 `main` 变更过大，暂不合并。后续为堆叠 PR：[#2](https://github.com/wingpeng30/Tracefix/pull/2) A 安全 checkpoint／恢复；[#3](https://github.com/wingpeng30/Tracefix/pull/3) B `doctor --prepare`、独立公开测试复跑与 Skills 配置；[#4](https://github.com/wingpeng30/Tracefix/pull/4) C 可选只读 Serena MCP；[#5](https://github.com/wingpeng30/Tracefix/pull/5) D 普通仓库 Docker profile。各 PR 基于前一分支，不把未合并分支误称主线版本。

A 的 [CI #25](https://github.com/wingpeng30/Tracefix/actions/runs/36532867162) 成功；B 修复后的 [CI #27](https://github.com/wingpeng30/Tracefix/actions/runs/36539596976) 成功。C 的 [CI #33](https://github.com/wingpeng30/Tracefix/actions/runs/36545042878) 全部 job 成功，Linux 原始 MCP 身份与结果见 [`../experiments/2026-09-29-serena-mcp-zero-call.md`](../experiments/2026-09-29-serena-mcp-zero-call.md)。D 的 [CI #38](https://github.com/wingpeng30/Tracefix/actions/runs/36549319954) Linux 普通仓库 job 成功，两次运行、新容器复验、超时／断连／导入错误与清理证据见 [`../experiments/2026-09-29-ordinary-docker-zero-call.md`](../experiments/2026-09-29-ordinary-docker-zero-call.md)；最终 Windows 覆盖率门槛尚待当前 PR 最新提交 CI 确认。

本轮未调用供应商模型，不访问留出题，不改历史账本、评分或原始证据。公开演示由固定回放驱动，不能推断真实模型修复成功率。Windows Docker Desktop 本机 daemon 不可用；既有冻结 Docker 流程与 C 的隔离查询由 Linux CI 验证。B 的实时模型验收仍待冻结小额方案并取得付费授权。

D 已提供普通仓库独立 Docker profile，预构建镜像和依赖身份、无网络／非 root／只读根文件系统的容器、新容器独立验证，以及 Linux CI 的重复运行与故障清理。首版不支持恢复丢失容器；Windows Docker Desktop 未实测。下一步先确保 PR #5 最新提交的 Windows Python 3.11／3.12 覆盖率、Ruff、compileall、Diff 和 wheel 门槛通过，再评估堆叠 PR 的合理合并方式。付费真实模型小额验收仍需先冻结方案并取得明确授权。
