# 2026-09-29 Agent/harness 产品交接

当前路线以 [`../tasks/2026-09-29-agent-harness-completion.md`](../tasks/2026-09-29-agent-harness-completion.md) 为准。原 PR #1 保存普通仓库首批，CI 通过但相对旧 `main` 变更过大，暂不合并。后续为堆叠 PR：[#2](https://github.com/wingpeng30/Tracefix/pull/2) A 安全 checkpoint／恢复；[#3](https://github.com/wingpeng30/Tracefix/pull/3) B `doctor --prepare`、独立公开测试复跑与 Skills 配置；[#4](https://github.com/wingpeng30/Tracefix/pull/4) C 可选只读 Serena MCP。各 PR 基于前一分支，不把未合并分支误称主线版本。

A 的 [CI #25](https://github.com/wingpeng30/Tracefix/actions/runs/36532867162) 成功；B 修复后的 [CI #27](https://github.com/wingpeng30/Tracefix/actions/runs/36539596976) 成功。C 的 [CI #32](https://github.com/wingpeng30/Tracefix/actions/runs/36544457821) Linux MCP job 已用真实隔离 Serena 服务成功调用两个符号工具，原始身份与结果见 [`../experiments/2026-09-29-serena-mcp-zero-call.md`](../experiments/2026-09-29-serena-mcp-zero-call.md)；最终 C 提交及其双版本质量检查应按最新 PR CI 更新。

本轮未调用供应商模型，不访问留出题，不改历史账本、评分或原始证据。公开演示由固定回放驱动，不能推断真实模型修复成功率。Windows Docker Desktop 本机 daemon 不可用；既有冻结 Docker 流程与 C 的隔离查询由 Linux CI 验证。B 的实时模型验收仍待冻结小额方案并取得付费授权。

下一批 D 是**普通仓库的独立 Docker profile**，不能放宽冻结任务白名单冒充支持。需要预构建镜像／依赖身份、容器无网络与非 root 约束、独立验证容器、超时／断连／清理证据，以及 Linux CI 重复运行。首版不承诺恢复丢失容器。实施前读取当前代码、CI 与进程状态；保留未跟踪历史材料并逐文件提交。
