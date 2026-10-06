# 当前证据索引

面试用结论入口见 [interview/README.md](interview/README.md)。本次精简归档的精选原始证据
集中在 `E:\TracefixExperiments\interview-archive-20261006`，来源路径与 ZIP 内路径、哈希及
实际删除/跳过项以该目录的 manifest 和执行报告为准。旧路径可能已被精简；历史报告和
评分不改写，不再承诺所有旧实验能够直接原样 resume。

不同证据回答不同问题；安装包版本号不是全部功能的验收身份。

| 能力 | 原始身份及结论 | 范围 |
| --- | --- | --- |
| 真实模型调用 | [2026-09-29 小批验收](experiments/2026-09-29-live-model-acceptance.md)：8 次逻辑运行、37 个完成请求 | 保留失败与预算停止；小样本不能推断总体成功率，额度已耗尽 |
| 结束前回归反馈 | [PR #9](https://github.com/wingpeng30/Tracefix/pull/9)，head `cb5571815a09f831cde0fd1f648059b6fdff6877`，[CI #73](https://github.com/wingpeng30/Tracefix/actions/runs/36701264794) | 录制响应与真实 pytest，不是模型效果比较 |
| 安装包内闭环 | `tracefix-reproduce --scenario regression-feedback`；新运行的 `reproduction.json`、轨迹及独立记录保存实际身份与产物哈希 | 本地，模拟 usage，费用未知；不支持 Skills/Docker 组合 |
| Serena MCP | [Linux 零调用记录](experiments/2026-09-29-serena-mcp-zero-call.md) | 真实隔离服务查询，模型离线；不证明提高修复率 |
| 普通 Docker | [Linux 零调用记录](experiments/2026-09-29-ordinary-docker-zero-call.md) | Windows Docker Desktop 未实测，不支持容器丢失后的会话恢复 |

完整运行目录与未审查的模型请求不发布。公开文档保留可复制命令和证据身份；本地原始结果继续保留。

安装包入口 head `5a518067889031222da7d2350a452c631883cd5d` 的 [CI #77](https://github.com/wingpeng30/Tracefix/actions/runs/36966132365) 五 job 通过，已在 PR #10 合并。公开 more-itertools 单任务资格、失败记录及当前平台范围见 [本批记录](tasks/2026-10-02-package-and-public-task.md)；资格通过不等于模型效果通过。

新增解析与资源生命周期任务的契约、纠正参考、失败记录及资格入口见
[三类型任务记录](tasks/2026-10-02-three-public-task-types.md)。Markdown 已通过
[PR #12 / CI #86](https://github.com/wingpeng30/Tracefix/actions/runs/36975942940)
并合并；Click 的最终证据以独立 PR #13 的精确提交 CI 为准。所有模型响应均离线，
不得将纠正参考的通过写成原上游修复提交通过，也不得据此声称修复成功率提高。
