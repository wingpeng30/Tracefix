# 连续对话（显式启用）

`tracefix chat --config run.toml` 创建会话并执行配置中的初始任务；每轮结束后输入下一条要求。
输入 `:exit`、`:quit` 或 EOF 离开。`tracefix chat --run /path/to/run` 从保存的会话重新进入。
`tracefix continue --run /path/to/run --message "纠正上一轮要求"` 执行一轮后退出。
Python API 为 `TraceFixRunner.continue_turn(run_dir, message)`。
普通 `run` 默认仍是单任务，TOML `[run] conversation = true` 可显式保存为连续会话。

完整完成的轮次进入 `awaiting_user`；安全暂停进入 `paused`。
`resume` 延续中断轮次，`continue` 追加一轮明确用户消息。
有未决请求、未提交工具批次、损坏归档或身份变化时拒绝自动继续。
`inspect --run ... --json` 分别列出 `resumable` 与 `continuable`，两者含义不同。
进程锁保证同一会话不能同时追加要求。

各轮共用工作区、完整历史和累计预算，追加消息不扩大额度。
等待输入及进程退出期间不计入执行时间；探索阶段、失败提示及结束验证重新开始。
历史测试保留，但每轮的新改动需要对应的新测试证据。
每轮不可变产物位于 `turns/000N/<attempt>/`，`latest.json` 绑定补丁、轨迹、配置、
验证和结果哈希；根目录结果展示最新轮次及累计资源。
启用 `memory = true` 后，每轮最多一次预算内经验提炼，固定 Skill 快照贯穿活跃会话。

会话格式为版本 2。旧版本单任务记录仍通过原入口读取与恢复，不自动转为连续会话。
当前 G2 仅支持本地普通仓库；Docker 支持属于后续 G3。
验收使用记录适配器和真实文件、pytest、独立进程；不证明真实模型决策和提炼质量。
