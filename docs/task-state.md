# 结构化任务状态

G3 在候选分支 `codex/harness-foundations` 引入有界 TaskState。阶段验收绑定源码提交
`9d52583ede2b4d56b12c18794543e53afc20e1b1` 与 CI #156；当前 G4 组合仍在实施中。

启用方式与工具产物共用 foundations 组合开关：CLI 使用 `--harness-foundations`，TOML `[run]`
使用 `harness_foundations_enabled = true`，Python `RunConfig` 使用
`harness_foundations_enabled=True`。普通 Docker 还要求 `docker_recovery_enabled=True` / `--docker-recovery`。

canonical 状态以 revision 单调递增，并限制为 256 KiB；用于上下文的确定性摘要最多 8 KiB。状态记录：

- 由宿主输入收据证明来源的用户需求，以及运行配置和用户需求提供的验收条件。
- 带消息/调用/来源哈希的观察，分为 `valid`、`historical`、`stale`。观察是有来源的工作记录；是否具有测试资格仍由现有验证门决定。
- 计划、假设、问题和阻塞项，以及记忆、Skill 和 artifact 的来源引用。

`get_task_state` 返回摘要和证据引用。`update_task_state` 使用 `expected_revision` 做全批校验，最多16个操作，单条自由文本最多1000字符；模型可新增或改变计划状态，并维护假设、问题和阻塞项。冲突、非法字段或超限整批拒绝。用户原始需求、来源身份、观察、测试结果、预算、权限和完成状态由宿主维护，模型无法编辑。计划项设为 `done` 不会跳过真实测试、diff 和 finish gate。

完整工具批次写入消息历史后才更新 TaskState。上下文通过版本化 `task_fact_anchor` 注入摘要；相同 digest 不会重复注入。checkpoint 保存 canonical 状态、状态哈希和其引用的历史前缀；恢复逐项检查已提交用户收据、工具结果、artifact、Skill 与 memory 引用。引用无效或状态损坏时拒绝恢复，不通过重新生成摘要继续。

本机与 CI 边界以及原始验收证据见[开发历程中的 G3 记录](development-history.md#2026-10-15-harness-foundations-g3-taskstate-阶段门通过)和[固定当前状态](current-status.md)。该文档描述的 G3 行为对应上述精确源码提交；后续文档提交本身未重新运行全工程门。
