# 能力策略与本地／ordinary Docker 组合

能力策略始终执行，不受 `harness_foundations_enabled` 开关控制。工具参数不能指定可信调用来源或提升权限。
产物和TaskState仍由组合开关启用，知识记忆仍用独立memory开关。以下是本地候选代码的使用约定；
真实Docker组合验收状态以[当前状态](current-status.md)为准，不把模拟后端测试当作真实容器通过。

## 执行规则

`workspace_write`是默认执行profile，允许目录中获准的读、补丁和测试操作。
`inspect`允许读和有界会话状态更新，拒绝工作区写入与测试执行；不能靠将计划标为done跳过验证。
源码、Skill、记忆及工具文字都不能改变profile。

工具必须有执行侧能力描述，包括资源域、副作用、幂等性、位置和并发要求。没有描述的运行工具拒绝装配。
模型、验证门、独立验收、恢复和宿主生命周期分别使用固定来源与能力子集；模型不能调用内部控制名称。
拒绝发生在缓存返回、prepare、进程启动和测试计次之前，并返回稳定的reason_code和策略/目录hash。

`PreparedInvocation`把检查后的调用参数固定为快照，一次派发后禁止再次执行，包括结果未知或异常的情况。
进程启动回调只属于这一次调用、最多通知一次，结束后恢复原回调；参数拒绝或进程未启动不会消耗测试次数。
没有prepare接口的旧工具保持原execute协议，不强行添加prepare包装。

## 配置入口

CLI使用`--execution-profile inspect`或`--execution-profile workspace_write`，组合能力使用`--harness-foundations`。
TOML在`[run]`中写：

```toml
execution_backend = "local"
execution_profile = "workspace_write"
harness_foundations_enabled = true
memory = true
skills = true
skills_dir = "approved-skills"
```

Python API中，`RunConfig`校验字段形状，功能依赖由同一个纯resolver决定；运行、预检与读取恢复配置均使用它。
如需在启动前单独检查组合：

```python
from tracefix.features import resolve_features
from tracefix.runtime import RunConfig

config = RunConfig(
    repo="project", task="修复测试", execution_backend="local",
    harness_foundations_enabled=True, execution_profile="workspace_write",
)
features = resolve_features(config)
```

不支持的组合抛出`FeatureResolutionError`，错误码为`feature_not_supported`，context包含稳定reason_code。
镜像、Docker服务和MCP SDK的可用性由执行预检检查，在创建模型前失败；不会联网安装、拉镜像或回退local。
Serena前提缺失使用`mcp_prerequisite_unavailable`，context的reason_code区分SDK缺失、Docker CLI缺失、
镜像探测异常和镜像不可用；不会把已知环境缺失归为未知运行错误。

## 后端组合

| 能力 | local | ordinary Docker |
|---|---|---|
| 有类型知识记忆、连续对话 | 支持 | 要求docker recovery |
| 工具产物、TaskState | 组合开关启用 | 组合开关及docker recovery |
| 自定义Skills | 获准目录 | 校验并固定快照，不挂载live目录；恢复复用原快照 |
| Serena三项只读查询 | 固定镜像、SDK、Docker服务 | 同样要求；通过受限exporter提供当前源码快照 |
| baseline和最终回归 | 本地验证适配器 | baseline每目标新容器，最终验证使用当前任务容器；要求ordinary recovery |
| 独立补丁验收 | 新checkout | 新ordinary容器，应用交付补丁并检查主目标及全部追加目标 |

Serena每次查询都重新获取源码快照。先过滤路径再读正文，限制5000文件/100 MiB，检查大小、hash和导出期间变化；
MCP只挂载临时快照readonly，不取得任务容器、宿主记忆库或Docker socket。
Skill正文及允许的Markdown references固定在只读执行器bundle中，拒绝链接/junction/特殊文件和越界内容。

验证执行通过`ValidationBackend`协议，local和Docker适配器负责取得与释放隔离资源；预算和通过资格仍由原Agent状态与审计决定。
恢复复用原baseline，不重跑后覆盖旧证据。策略、capability目录、executor目录、agent目录及供应商ToolSpec身份分别固定，
缺字段或身份漂移拒绝恢复。新增策略版本不会自动迁移旧检查点。

## 零供应商调用复现

本地回放可运行`python -m tracefix.reproduction --scenario regression-feedback --output NEW_DIR`；
基础、记忆、连续对话和恢复回放见[工具产物](tool-artifacts.md)及现有reproduction入口。
完整Docker组合使用已准备好的两个镜像：

```text
python examples/replay_artifact_recovery.py --composite --image-id TASK_IMAGE_ID --mcp-image-id SERENA_IMAGE_ID --output NEW_DIR
```

该脚本验证记忆、固定Skill、日志中段取回、TaskState、源码变化后的Serena查询、销毁容器/新进程恢复、第二轮输入、
最终验证及独立验收。模型响应使用脚本替身，供应商调用数必须为0。真实组合不能在缺少Docker服务时标为通过。
workflow已为wheel和editable分别安排完整组合和产物归档，但本轮只修改本地文件，不上传或启动CI。
