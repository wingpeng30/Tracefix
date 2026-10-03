# 仓库级经验 Skills

经验记忆默认关闭。它将有工具及测试证据的已完成任务提炼为仓库级操作经验，
在后续新任务中确定性召回，使用既有 `load_skill` 协议按需加载。
存储位于目标源码仓库外，不读取或执行目标仓库中的自定义指令。

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

也可向普通 `tracefix run` 添加 `--memory --memory-dir <目录>`。
省略目录时默认使用产物根目录的 `memory`；同一源仓库绝对路径共享经验，其他仓库独立。
首批仅支持本地后端；Docker 经验快照传递在后续阶段实现。

每轮最多一次提炼请求，使用同一模型适配器，并计入任务累计步骤、输入、输出、费用和时间预算。
预算不足或最终补丁未经有效测试时跳过；网络超时等未知结果不会自动重发。
修复结果和提炼结果分别报告，提炼失败不会把已通过测试的修复误写成未通过。

自动启用只接受实际成功工具调用支持的已观察操作顺序，测试证据须绑定最终源码。
操作模板为读取、搜索、聚焦补丁、相关测试和最终 Diff 检查；模型自由生成的建议和
未验证泛化只保存为候选。经验总结保留供查看，不直接作为运行指令注入。
历史案例不证明对其他源码版本的普适正确性，加载时仍须检查当前源码与测试。

最多检索三个匹配任务关键词且适用文件仍存在的经验；与内置或显式批准 Skills 一并保存
到运行内固定快照。存储的后续更新不改变活跃运行的指令身份。
相同内容去重，同类修改要求明确引用被替代版本的内容哈希；无明确继承的冲突会暂停启用。
旧版本保留，可显式停用与回滚已验证版本：

```powershell
tracefix memory list --repo ../my-project --memory-dir ./memory
tracefix memory show --repo ../my-project --memory-dir ./memory --key pagination
tracefix memory disable --repo ../my-project --memory-dir ./memory --key pagination
tracefix memory rollback --repo ../my-project --memory-dir ./memory --key pagination --version 1
```

完整离线、独立进程复现（安装包中可用）：

```powershell
tracefix-reproduce --scenario memory-experience --output ./memory-demo
```

第一进程使用注入录制客户端驱动真实工具修复和提炼；第二进程从新任务检索、实际加载经验、
运行真实 pytest 并导出补丁。两次补丁均在新 checkout 独立复验，同时验证停用与回滚。
该入口阻断供应商模块导入与 socket 连接，usage 明确为模拟。
证据验证的是工程闭环，不证明真实模型经验提炼质量或自主修复成功率。
