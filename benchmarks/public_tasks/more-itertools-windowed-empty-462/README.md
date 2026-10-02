# more-itertools #462：可信空输入任务

来源：[issue #462](https://github.com/more-itertools/more-itertools/issues/462)、[PR #645](https://github.com/more-itertools/more-itertools/pull/645)。MIT 许可证随包保留。故障源码 `8a27da60d70fbfc348b124ce89733d8a7104977a`，参考修复 `db4630bc3175d6a884fb07092c645417c9e62d49`。参考仅提取 `more_itertools/more.py`；没有采用上游测试重写。

`task.md` 是 Agent 可见契约；`prepare-tests.patch` 只新增公开目标的 36 个组合。现有六个 `WindowedTests` 方法保留短非空输入填充、正常滑动、步长、异常和零宽度契约。`diagnostic-wrong.patch` 故意丢弃所有短输入，用于证明空输入目标本身不够。参考及诊断补丁均保留在 Agent checkout 外。

`manifest.json` 冻结来源、产品 Git blob、目标及任务包文件 SHA-256；LF 检出规则保证跨平台哈希一致。具体执行是否合格以新生成的 `qualification.json` 和精确提交 CI 为准，静态 manifest 不冒充执行结果。

先安装 TraceFix、Git、pytest；测试解释器需已具备依赖，脚本不安装、不联网获取源码、不调用模型：

```powershell
git clone https://github.com/more-itertools/more-itertools.git D:\task-sources\more-itertools
python scripts/qualify_public_task.py --source-repo D:\task-sources\more-itertools --test-python <Python绝对路径> --output "runs/public qualification"
```

上游 clone 只用作固定对象源，HEAD 可不同，脚本不修改它。输出必须是新目录，且不要放在上游仓库内。脚本创建并保留准备源码，单独记录派生任务提交及 base。完整矩阵：两次 base 为任务失败/回归通过，两次参考为全部通过，诊断错误补丁为任务通过/回归失败。收集失败、超时、源码/环境变动、输出截断或测试数量变化均不能通过资格；失败时保留原始记录，退出非零。

生产 Runner 参考补丁回放最后执行验收门、独立验证、报告及导出。它验证协议和证据路径，不是模型自主修复，也不计入任何真实模型成功率。所有 usage 是模拟值，供应商费用未知。当前仅资格化这个任务，不宣称已建立多类型任务集。
