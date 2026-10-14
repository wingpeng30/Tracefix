# 有类型、按来源适用的仓库知识记忆

此页说明候选分支 `codex/harness-foundations` 上的 G1 实现。该能力由 `--memory` 或 TOML
中的 `memory = true` 显式启用；默认关闭。阶段验收状态、提交和限制以[当前状态](current-status.md)
为准。本功能不引入向量数据库或新的联网模式。

记忆存放在目标源码仓库之外。Git worktree 根据可信 common-dir 共享逻辑仓库命名空间，但每个
checkout 分别评估源码适用性；独立 clone 默认隔离，不按 remote URL 合并。

```toml
[run]
repo = "../my-project"
task = "修复分页"
test_target = "tests/test_widget.py"
source_import = "widget"
output_dir = "./runs"
memory = true
memory_dir = "./memory"
```

`memory_dir` 必须位于目标源码仓库之外。运行时记忆提炼使用现有反思适配器，最多每轮一次，受任务
累计预算约束；预算不足时不派发，未知响应不自动重发。`tracefix memory` 的查询和管理命令不调用模型。

也可直接从命令行启用：

```powershell
tracefix run --repo ../my-project --task "修复分页" --test-target tests/test_widget.py `
  --source-import widget --memory --memory-dir ./memory
```

## 记录、证据与适用性

每条知识分开保存内容类型、发布状态、证据等级和当前 checkout 的适用性：

| 轴 | 值 | 含义 |
|---|---|---|
| 知识类型 | `project_fact`、`decision`、`procedure`、`negative_result` | 项目事实、用户决策、已验证流程、带失败条件的负结果 |
| 发布状态 | `candidate`、`active`、`disabled`、`superseded` | 版本链中的生命周期；显式停用不会因源码恢复而解除 |
| 证据等级 | `observed`、`user_stated`、`source_cited`、`unsubstantiated` | 可核对的机器观察、带宿主收据的用户陈述、只有可追溯来源但不足以机器证明、未充分支持 |
| 当前适用性 | `matched`、`stale`、`missing`、`unknown` | 对本次 checkout 的文件、依赖和环境匹配结果 |

通用 `USER` 消息角色本身不代表可信用户输入。宿主为直接用户消息生成绑定消息 ID、轮次和原文哈希的
收据；Repo Map、Skill、工具结果和模型生成文本不能伪造该收据。机器观察从有界、不可变的
`EvidenceSnapshot` 验证。模型总结、推断或泛化即使带源码引用也只保留为候选，不能仅凭自由文本升级为
机器事实。
`negative_result` 仅从最终成功任务中有具体调用和失败条件支撑的失败尝试提取；失败终止的任务不会自动学习。

项目事实绑定已修复源码的 postimage；流程前提和负结果按适用路径绑定前置源码。依赖清单和环境指纹
分别记录，不将补丁哈希误作文件树哈希。当前按整文件内容和明确环境字段判断；符号级失效尚未实现。
普通代码改变后，记录只会在该 checkout 变为 stale 或 missing，不会改变其他 worktree 的发布状态。

自动召回只包含同时满足 `active`、证据等级为 `observed` 或 `user_stated`、且当前 checkout 为
`matched` 的记录。确定性检索最多选择 3 条，正文最多 8 KiB UTF-8；路径、错误签名和关键词权重固定为
8、4、1，同分按记录 ID 排序。中文使用字符二元组，拉丁文本使用规范化词项。结果附带选择理由、版本、
证据、来源身份和失效原因。

每次运行保存不可变召回 bundle。恢复读取原 bundle，不重新查询活库；源码变化时只追加适用性提醒，
不重写历史请求。召回以低信任参考消息进入模型上下文，不授予权限，也不重复经 Skill 注入。
读取过的旧知识本身不构成当前源码已读取或测试通过的证据。

## 管理与 v1 迁移

管理命令要求指定仓库和记忆目录，例如：

```powershell
tracefix memory list --repo ../my-project --memory-dir ./memory
tracefix memory show --repo ../my-project --memory-dir ./memory --key pagination
tracefix memory migrate --repo ../my-project --memory-dir ./memory --dry-run
tracefix memory migrate --repo ../my-project --memory-dir ./memory
tracefix memory disable --repo ../my-project --memory-dir ./memory --key pagination
tracefix memory rollback --repo ../my-project --memory-dir ./memory --key pagination --version 1
```

`--dry-run` 仅适用于迁移，报告预计规范化结果和冲突，不写入存储。v1 读取只做只读归一化：不补造历史
源码哈希、preimage、用户收据或机器证据；旧记录保留为 legacy/source-cited，不能自动召回。
显式迁移或首次 publish、disable、rollback 会在仓库锁内校验旧文件哈希，将迁移收据和本次操作写入同一
v2 校验信封并原子发布；原 v1 文件保留。损坏 v2、身份冲突、内容变化或写入失败均拒绝写入，不会回退到
v1 继续更新。版本冲突需要明确 `supersedes` 关系；停用需由显式回滚解除。索引有 16 MiB 上限，超限写入
被拒绝，已有版本保留。

## 离线演练与边界

可在安装包中运行录制客户端演练，不发送供应商请求：

```powershell
tracefix-reproduce --scenario memory-experience --output ./memory-demo
```

演练检查存储、适用性、固定快照、提炼与后续读取控制流。它不证明真实模型提炼质量或修复成功率。
记忆不会替代测试审计、预算、权限、验证门或最终 Diff 检查。完整 G1 阶段是否完成以固定状态入口的
双 Python 与真实 Docker/MCP CI 结果为准；G2 工具产物、G3 TaskState、G4 统一能力策略尚未由本功能实现。
Docker 下的记忆/连续对话沿用现有 recovery 依赖；本阶段验收该恢复链，不代表 G4 的 Skills、Serena、
工具产物和最终回归组合已经实现。
