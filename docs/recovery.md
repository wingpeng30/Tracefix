# 本地任务的检查与恢复

普通本地运行会在每个完整模型／工具批次后保存 `session.json` 与 `checkpoint.json`。快照包含完整对话、Agent 阶段及预算、任务事实、Skills 加载状态和源码／配置／工具身份。轨迹事件在操作前写入并落盘；恢复继续写入同一轨迹，先前的 `result.json` 另存为 `result-before-resume-<序号>.json`。

首版仅支持同一台机器上原运行目录、源码仓库和 checkout 均仍存在的普通本地运行。环境配方、自定义测试环境变量和 Docker 会话目前不生成可恢复快照。快照保存的配置不包含供应商密钥；恢复时重新从进程环境或配置的 `.env` 读取。不要把运行目录当成公开资料，它可能包含源码和模型消息。

```powershell
tracefix inspect --run runs/<run-id> --json
tracefix resume --run runs/<run-id>
tracefix report --run runs/<run-id>
tracefix export --run runs/<run-id> --output fix.patch
```

`inspect` 不构造模型。恢复前检查当前 TraceFix Python 实现、源码提交、配置、测试解释器及依赖指纹、工具 schema、Skills 内容、Repo Map、checkout 补丁以及完整轨迹前缀。恢复命令对运行目录加进程锁，拒绝已完成的运行、损坏或过期快照、未配对消息，以及 checkpoint 后存在未提交模型／工具事件的运行。模型请求或修改工具的结果未知时不会自动重发或重放。若恢复被阻断，可保留原始产物分析，随后发起新运行；首版没有强制越过检查的选项。

离线复现完整过程：

```powershell
python examples/replay_resume.py --output runs/recovery-demo
```

示例使用录制的 LiteLLM 形态响应，在完成一次读取工具批次后故意中断，随后通过实际 `TraceFixRunner.resume` 继续补丁、pytest、Diff、报告和导出。它不发送供应商请求，不证明真实模型自主修复成功率。`run_tests` 在测试前后核对产品 Diff；测试虽然返回零但修改了源码时，该次测试不会被标为有效通过。
