# 追加回归验证与 Boltons 派生诊断

本批基于主线 `b26340df5b9c6b6b3d07123f4e4ba4a85da1d927`，没有发送供应商请求，也没有修改 2026-09-29 的真实运行、原始补丁或评分。诊断使用 Python 3.12.5，原始结果在 `runs/live-2026-09-29/boltons-pairwise-derived-20260930.json`（SHA-256 `99c06777695d31aacc977376624d370b371a3f5ab537ae2de6707742d73a2724`），可用 `scripts/diagnose_boltons_pairwise.py` 复跑。该目录被 Git 忽略；GitHub 文档只保存路径、提交身份和结论。

Boltons 来源为 BSD 许可的 16.1.1 提交 `56692b31e7b41adddbbed59bd77c0d6e276082b5`。本轮冻结故障源码提交是 `933ebc9e0f0bb2c45db603786ab502925cf85418`，加入公开测试和 Python 3.12 导入兼容；模型补丁来自真实运行 `20260929T150714Z-eb885358`。上游 16.2.0 对照提交为 `87eec49c562f88f4e91642e9f6d97cde2bf606f1`，本机工作树有 Python 3.12 兼容改动，原始 JSON 保存了实际文件 SHA 和 Git 状态。诊断不将其当成原样上游提交。

| 场景 | 故障版 | 模型补丁 | 上游对照 | 判断 |
| --- | --- | --- | --- | --- |
| 空输入 | `[]` | `[]` | `[]` | 满足 |
| 单元素 | `[[1]]` | `[]` | `[]` | 模型与上游一致 |
| 四元素列表 | 非重叠 `[[1,2],[3,4]]` | 重叠三对 | 重叠三对 | 修复任务要求 |
| 生成器及 `pairwise_iter` | 非重叠、末尾不足二元组 | 重叠二元组 | 重叠二元组 | 模型与上游一致 |
| `count=1` | 只返回首组 | 模型忽略旧参数 | 上游删除参数并抛 `TypeError` | 任务未规定兼容性；不能据此断定回归 |

原公开测试只检查四元素列表的一个断言。它和新诊断说明指定行为得到修复，但不证明完整 Boltons API 或测试集通过。完整历史测试在 Python 3.12 上仍有独立兼容问题。本批新增的普通仓库离线示例使用自己的最小 fixture，含明确的负数哨兵约束；它用于验证 TraceFix 能发现“主目标通过、另一测试被补丁破坏”，不将该合成回归投射到 Boltons。

普通本地运行的新命令为：

```powershell
tracefix verify --run <run-dir> --regression-target tests/test_existing_behavior.py
tracefix report --run <run-dir>
```

多个 `--regression-target` 可重复提供。工具在原始提交和应用保存补丁后的两个新 checkout 中运行追加目标，并在补丁 checkout 复跑原目标。每次记录保存在 `regression-verifications/<id>/record.json` 与对应原始 pytest 审计中，绑定源码、配置、补丁、环境和目标。CLI 返回 0 仅表示原目标通过且所有追加目标为保持通过或得到修复；回归、仍失败和证据不完整返回 2。旧 `tracefix verify --run` 保留原格式与行为。Docker 普通 profile 仍仅支持原来的独立验证。

可复制的零调用反例：

```powershell
python examples/replay_ordinary.py --output runs/regression-example --regression-example
```

其输出应为 `regression_status: regression`，HTML 展示原目标通过、`tests/test_backward.py` 原始通过而补丁失败，原仓库 Git 工作树保持干净。示例输出目录需预先不存在。

实施验证（提交前工作树基线 `b26340d`）：聚焦真实 checkout 的 `tests/test_regression.py` 为 2 passed；`tests/test_report.py tests/test_onboarding.py` 为 29 passed；Ruff、compileall 和 `git diff --check` 通过。仓库外 wheel `tracefix_agent-0.8.4-py3-none-any.whl` 的 SHA-256 为 `49252ee921c2ae929bef2135b8ac486ffcfb228cc1c5e24fab226dcca9fbefae`；隔离虚拟环境确认从 wheel 的 `site-packages` 导入，并完成离线修复、回归识别、报告、导出，补丁 SHA-256 `8397482f559dd41b0cfb24e44485d7db607d590f112528f7e23509d24c772282`。原始目录为 `runs/regression-wheel-20260930/`。

本机全量工程脚本在 `runs/quality-regression-20260930-final/` 长时间运行且没有完成 pytest 原始报告，本轮停止该次本机尝试；它**不计作全量通过**。最终 Windows 双版本、精确覆盖率及 Linux 门槛以本批 PR 的 GitHub CI 原始结果为准。
