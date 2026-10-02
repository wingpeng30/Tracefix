# 当前证据索引

不同证据回答不同问题；安装包版本号不是全部功能的验收身份。

| 能力 | 原始身份及结论 | 范围 |
| --- | --- | --- |
| 真实模型调用 | [2026-09-29 小批验收](experiments/2026-09-29-live-model-acceptance.md)：8 次逻辑运行、37 个完成请求 | 保留失败与预算停止；小样本不能推断总体成功率，额度已耗尽 |
| 结束前回归反馈 | [PR #9](https://github.com/wingpeng30/Tracefix/pull/9)，head `cb5571815a09f831cde0fd1f648059b6fdff6877`，[CI #73](https://github.com/wingpeng30/Tracefix/actions/runs/36701264794) | 录制响应与真实 pytest，不是模型效果比较 |
| 安装包内闭环 | `tracefix-reproduce --scenario regression-feedback`；新运行的 `reproduction.json`、轨迹及独立记录保存实际身份与产物哈希 | 本地，模拟 usage，费用未知；不支持 Skills/Docker 组合 |
| Serena MCP | [Linux 零调用记录](experiments/2026-09-29-serena-mcp-zero-call.md) | 真实隔离服务查询，模型离线；不证明提高修复率 |
| 普通 Docker | [Linux 零调用记录](experiments/2026-09-29-ordinary-docker-zero-call.md) | Windows Docker Desktop 未实测，不支持容器丢失后的会话恢复 |

完整运行目录与未审查的模型请求不发布。公开文档保留可复制命令和证据身份；本地原始结果继续保留。
