# 2026-09-29 真实模型小批验收

本轮使用 `deepseek/deepseek-flash` 非思考、非流式 function calling，本地后端，零自动重试。用户授权上限为人民币 20 元及 8 次逻辑 Agent run，先到即停；实际执行 **8/8 次**，包含失败运行。37 个已完成模型请求，0 个未决请求。按执行时冻结的官方高峰价（未命中缓存输入 ¥2／百万 Token、输出 ¥8／百万 Token）计算的保守费用为 **¥0.33653**；这不是供应商实际扣费或账户硬限额。原始请求账本位于 `runs/live-2026-09-29/requests.json`，SHA-256 `86683ed2c15b92a64e02c7ae24c1580477a2d0f4e8785f45b685fbcc00528de5`。运行清单位于同目录 `runs.json`，SHA-256 `96ee6bf49a2fe23d92f3627cd8737cd8bf2572344236f9fc1d4374282a0ce05f`。目录受 `.gitignore` 排除，其中含完整模型请求视图、源码和工具输出，未推送 GitHub。价格和工具协议依据 [官方价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/) 与 [工具调用指南](https://api-docs.deepseek.com/guides/tool_calls/)。

| 序号／阶段 | 运行目录 ID | TraceFix 提交 | 源码提交 | 配置 SHA-256 | 结果 |
| --- | --- | --- | --- | --- | --- |
| 1 smoke | `20260929T145302Z-1c3e03a1` | `f6ff399` | `fce23161e4ee5ff730e4822b2e96fcedcf93b999` | `94493b1f39be83bce9441baed115b2ded0535fd25bc306086688f1de9af49761` | Token 预算停止；补丁有效，独立验证通过，但任务未正常结束 |
| 2 multi-baseline | `20260929T145623Z-694e2769` | `080da628` | `d4ceb97aee33151c74b147d443a2cb2057c61a4a` | `545e2093235764f6dfa2effd4db7323b8c7d17dc11f6e282dd1e3cbf36083292` | 完成，公开测试与独立验证通过；4 请求、7 配对工具调用 |
| 3 multi-skills | `20260929T145755Z-a4fdebde` | `080da628` | 同 2 | `57bfa0e537c19d7014b6a26c562cd57a6f666bec9df79950af00647d10edb324` | 完成、独立验证通过；实际 `load_skill` 和 `skill_activated` 各 1 次 |
| 4 multi-compaction | `20260929T145858Z-65f1bc65` | `080da628` | 同 2 | `eb5625126e087c47ed35563a23a24f886ac6bd238d4c67603a2a636385495455` | 完成、独立验证通过；未触发压缩 |
| 5 multi-resume | `20260929T150042Z-b2cd5196` | `36c04e26` | 同 2 | 同 2 | 完整工具批次后受控中断，`inspect` 可恢复；真实模型续跑后完成、独立验证通过 |
| 6 external | `20260929T150714Z-eb885358` | `36c04e26` | `933ebc9e0f0bb2c45db603786ab502925cf85418` | `0f96004523a63b56b450f64651df1c605a08f2a6fd7d60a0eb77c40a5068d15e` | Boltons 历史回归完成、独立验证通过；完整旧测试集受 Python 3.12 兼容性限制 |
| 7 compaction-recheck | `20260929T150901Z-b14b33e1` | `36c04e26` | 同 2 | `3df6e94f0aac8fdc291c1b0b7a38f3e35a715dc2140befeab2fc36935dee1529` | 完成、后补独立验证通过；未触发压缩，定位上下文固定范围错误 |
| 8 compaction-after-fix | `20260929T152105Z-e2f510f9` | `d7fcd041d8c9d075a44da51549fe05eb27f818bd` | 同 2 | 同 7 | 完成、独立验证通过；实际压缩 3 个完整批次，请求估算 4474→3797 |

每个 ID 位于 `runs/live-2026-09-29/runs/<ID>/`，包含原始 `result.json`、`trajectory.jsonl`、`patch.diff`、独立验证记录和 HTML 报告。首次 smoke 的补丁 SHA-256 为 `8397482f559dd41b0cfb24e44485d7db607d590f112528f7e23509d24c772282`；多文件任务 2–5、7–8 为 `4118325c94c9d2d5a491b6e841ad0adc20f438ac25f245d36804ca5e3bb04060`；外部任务为 `398fb28d3b78bdc66aebe5fffd8f49ca98019e57658810a0567f135ffc8b8bc1`。各运行的完整实现提交、模型请求／响应名称、usage、费用和配置身份以原始清单及请求账本为准。

真实运行揭示：缺失／非法 usage 原先可能回退为零，现改为明确格式错误并阻断后续付费请求；预算预留发生在发送前，未决请求不会被自动重发。压缩测试首次两次没有覆盖到机制；离线回放真实历史发现早期失败测试把后续所有批次固定在请求中，修正为仅固定最新失败测试批次后，第 8 次真实运行确实触发压缩。对应离线回归为 `tests/test_context.py` 和 `tests/test_live_budget.py`。

外部任务基于公开 BSD 许可 Boltons 16.1.1 源码 `56692b31e7b41adddbbed59bd77c0d6e276082b5`，增加 Python 3.12 `collections.abc` 导入兼容行与公开回归测试后冻结为上表源码提交。回归在故障版失败，在 16.2.0 `87eec49c562f88f4e91642e9f6d97cde2bf606f1` 参考版本通过；模型未见参考补丁。仅目标测试得到验证，旧版完整测试集不能在当前 Python 3.12 直接收集。模型补丁还改变 `count=1` 行为，可能是 API 回归，不能据此宣称完整正确性。

这 8 次只证明指定公开任务在指定环境中的闭环与机制调用；不能估计真实修复成功率，也不能说明 Skills 或压缩提高成功率。MCP、Docker 的本轮付费模型路径未执行；其零调用 Linux CI 证据单列。20 道留出题未访问，历史评分和账本未改。

工程门槛：[CI #50](https://github.com/wingpeng30/Tracefix/actions/runs/36588295467) 在 `d7fcd04` 的五个 job 均通过；Windows 3.11／3.12 各 760 tests、0 skipped，纯 pytest 综合覆盖率 12944/14379 = 90.02016830099451%，Ruff、compileall、Diff、editable、仓库外 wheel 及 Linux 冻结 Docker、普通 Docker、Serena MCP 全部通过。
