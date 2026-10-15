# 有界工具产物

候选分支 `codex/harness-foundations` 的 G2 为测试命令输出和部分结构化工具结果增加了可取回的证据存储。
阶段验收身份是实现提交 `075bfae7d921b0d4ca7729b5a69f5c5218e89a10`，tree
`2d37ed9dc063f1a819104c6a640b5d88c0acdac5`，CI #152；后续实现状态以[当前状态](current-status.md)为准。

本能力随 `harness_foundations_enabled` 组合开关启用，默认关闭。CLI 示例：

```powershell
tracefix run --repo ../my-project --task "调查测试失败并修复" --harness-foundations
```

TOML `[run]` 可设置 `harness_foundations_enabled = true`；Python API 可在 `RunConfig` 中设置
`harness_foundations_enabled=True`。普通 Docker 使用该开关时还必须开启 `docker_recovery_enabled`
（CLI：`--docker-recovery`），这样容器重建后才能恢复产物索引、内容及累计查询配额。memory 仍由独立
`memory_enabled` / `--memory` 开关控制。

`run_tests` 和 `run_validation` 在输出进入面向模型的展示截断前同步 drain stdout/stderr。存储保存原始字节供本地
审计，并从中生成不可变 UTF-8 检索视图；模型只能读取经过有限模式及已配置秘密值脱敏的检索视图。manifest 保存
来源身份、raw/view 哈希、长度、脱敏策略身份和完整性状态。读取产物只证明它是历史工具输出，不证明当前源码内容、
测试资格或任务完成。

默认边界如下：

| 项目 | 上限 |
|---|---:|
| 单产物 raw 与检索视图 | 各最多 8 MiB |
| 单 run 总存储 | 64 MiB |
| 单 run 产物数 | 128 |
| 单页读取 | 4,096 个 Unicode 字符 |
| 每会话读取/搜索次数 | 64 |
| 每会话返回文本累计量 | 128 Ki 字符 |
| 字面搜索结果 | 最多 20 条，每条摘要 160 字符（工具参数允许 32–512） |

工具目录包含 `read_tool_artifact(artifact_id, stream, offset, limit)` 与
`search_tool_artifact(query, max_matches)`。`artifact_id` 是不透明 run-local ID；模型不能指定宿主路径、其他 run ID
或正则表达式。offset 按 Unicode 字符计算。结果带来源源码身份、完整性、截断位置和 EOF；如果磁盘配额耗尽或 drain
出错，仍会继续消费进程管道，但产物标为 incomplete，不能据此声称日志完整。查询额度随 run manifest 保存，并随
ordinary Docker recovery 一起恢复；压缩、连续对话或容器重建不会重置。

在 G2 启用时，`search_code`、`read_file`、`get_git_diff` 的结构化返回也会归档并附 locator，避免内容被上下文
裁剪后失去读取入口。raw 字节不返回给模型；发现记录篡改、跨 run 引用、路径越界、链接文件或额度不一致时拒绝读取和恢复。

此功能目前属于候选分支的已验收实现，不代表已发布包默认开启。完整 CI 证据和限制见[开发记录](development-history.md#2026-10-15-harness-foundations-g2-工具产物)。
