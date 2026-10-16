# 分层仓库规则（G6候选）

本功能让模型在访问文件前先看到相关开发约定。默认关闭，当前阶段的验收边界以[当前状态](current-status.md)和[G6记录](tasks/2026-10-16-g6-progress.md)为准。

## 启用

CLI给普通任务增加`--repository-rules`。Python API使用`RunConfig(repository_rules_enabled=True, ...)`。
TOML示例：

```toml
[run]
repo = "D:/demo"
task = "修复分页测试"
execution_backend = "local"
repository_rules_enabled = true
```

ordinary Docker另外需要`docker_recovery = true`和准备好的固定镜像身份，以及原有的测试目标、源码导入配置。不会回退到本地。
规则可以单独启用；需要TaskState与工具产物时，另开`harness_foundations_enabled = true`。
会话启动后不能切换规则模式。开启时不注入启动Repo Map源码摘要，以保证相关源码先经过规则检查；关闭时保留原来的工具schema和入口。

## 放置规则

在仓库根和相关子目录放置`AGENTS.md`。例如访问`src/web/page.py`时，按顺序读取根、`src/`、`src/web/`下存在的`AGENTS.md`。
不会自动读取宿主仓库外面的父目录。

路径规则放在`.tracefix/rules.toml`，正文限于`.tracefix/rules/`：

```toml
schema_version = 1

[[rules]]
paths = ["src/web/**/*.py", "tests/web/**/*.py"]
file = "web.md"
```

对应`.tracefix/rules/web.md`写开发约定。先加载祖先规则，再按配置声明顺序加载路径规则。glob支持`*`、`?`以及作为完整路径段的`**`，不支持正则、脚本或任意include。

规则不能授权联网、任意shell、额外预算或跳过验证；用户要求和执行策略继续决定这些权限。

## 访问和搜索

遇到模型尚未收到的规则，工具返回`rules_pending`，相关读写暂不执行。整批工具结果配齐后，宿主追加完整规则；模型下一次请求确认包含它们后，再重试操作。
同一批里先读文件再打补丁也必须等待。补丁会先检查全部新旧路径，再读取原文件。

搜索按文件推进，遇到新规则就停在该处。返回的`continuation.arguments`可直接作为下一次`search_code`参数，其中cursor是opaque ID。
不得修改原查询后继续使用旧cursor。候选集合、已访问规则或文件中段内容改变时会拒绝继续，需开始新查询。
搜索返回安全结果不代表已经完成整次搜索，应同时查看`rules_pending`、`eof`、`truncated`。

Serena源码结果需要可核对的来源路径；新作用域的正文也会等待规则送达，不会通过MCP绕过文件规则。

## 限制和恢复

- 单份正文16 KiB，一次完整适用集合32 KiB、最多16份；损坏或超限时拒绝，不截断后冒充完整规则。
- 安全路径检查拒绝遍历、链接、junction、特殊文件及内部证据路径。Diff访问规划读取Git名字元数据，包含已删除路径，不提前生成Diff正文。
- 搜索保留最多128个cursor；关闭、压缩、追加对话和恢复都不能重置该额度。
- 最近4份完整规则受上下文保护。较旧正文退出上下文后，再访问相关范围需要重新送达完整规则。
- 检查点保存正文、版本、送达收据和游标；恢复核验历史引用，不能用活目录重新生成旧状态。TaskState只保存规则引用。
- 显式规则修改/删除按新版本处理。宿主外部修改导致既有恢复身份变化时，仍按原恢复约定拒绝，不能靠重新摘要修补。

## 零供应商回放

在源码目录使用全新输出路径：

```powershell
python examples/replay_repository_rules.py --output runs/rules-local
python examples/replay_repository_rules.py --backend docker --image-id sha256:<ordinary-image> --mcp-image-id sha256:<serena-image> --output runs/rules-docker
```

脚本使用录制形状的本地客户端，经过生产LiteLLM适配器；暂停和恢复发生在两个宿主进程，Docker模式还会替换任务容器。
覆盖根/嵌套/路径规则、文件中段分页、补丁及真实pytest；可选Serena核对补丁前后源码。它证明控制流，不是模型自主修复能力或付费成绩。
当前代码回放结果、失败记录和安装方式验收状态见G6记录，不能借旧G5/G4证据称新版本全部通过。
