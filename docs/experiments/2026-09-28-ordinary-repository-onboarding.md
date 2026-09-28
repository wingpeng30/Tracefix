# 普通 Python 仓库运行闭环（2026-09-28）

## 身份与范围

- 起点：`b375b9efadc93dad2d3d2fc543cce2f50a293628`，代码提交 `d8df46ddc8792e8f5114c5c2abf2bd8df01d395d`，分支 `codex/p0-checkpoint-p1-review`。本机工程检查在提交前同一源码内容的脏工作树上执行；提交后的干净 PR 检出需由 CI 单列判定。
- 本机：Windows 11，Python 3.12.5；普通本地后端；零供应商调用。`E:\TFP` 为已有的隔离 pytest 临时根，本机 Docker daemon 不可用。
- 合成普通仓库：`examples/replay_ordinary.py` 每次生成新的干净 Git 源、测试和独立 checkout，模型为 `offline/replay`。它是录制的 LiteLLM 形态响应，不是实时模型自主修复。

## 实际修改

- `doctor` 零调用预检、可选 TOML 配置及 CLI/环境/配置优先级；跨平台 `runs` 默认目录。
- 普通运行可指定公开测试目标和源码导入探针，在模型构造前拒绝测试解释器或导入身份错误；产物目录落在源仓库内时会在创建文件前拒绝。
- 保存落盘补丁 SHA-256，`export` 校验并复制补丁；报告将缺失费用标为未记录，展示执行环境与错误来源。
- 普通仓库回放、wheel CI 验证和使用说明；系统能力矩阵另见 `docs/reviews/2026-09-28-system-review.md`。

## 本机原始证据与限制

- 定向旧回归：53 passed，原始终端记录对应 `E:\TFP\onboarding-new-20260928-1`；新增行为测试第一次 5 passed，后续回放与报告调整的 2 项复验通过。回放源目录 `runs/ordinary-replay-20260928-b/`，6 次录制响应请求，结果 `completed` 且公开测试 `verified`；导出文件 SHA-256 为 `8397482f559dd41b0cfb24e44485d7db607d590f112528f7e23509d24c772282`，源仓库 Git 状态为空。
- 本机默认沙箱中的两次全量 pytest 在临时根创建时遭遇 `PermissionError`；这些运行的低覆盖率无效，不作为代码质量结论。原始失败记录为 `runs/engineering-onboarding-20260928/` 和 `runs/full-onboarding-20260928/`。
- 最终候选本地 wheel 构建通过，文件在 `runs/ordinary-wheel-final-20260928/`，SHA-256 `b06f8f6de46e4c65d1d69659245a82f9981a54bebcc2bfe2b7ceda6273fba021`；初版 wheel `28430f865daae129eaa808d36ac659420400447bea00f72994298e727d2b9b04` 仅为中间检查。ZIP 核对包含 `tracefix/cli.py`、`tracefix/onboarding.py`、`tracefix/report.py`。仓库外安装与普通回放由提交后 Windows CI 再判定。
- 使用获批执行权限的统一工程检查在 `E:\TFP\onboarding-engineering-approved-20260928` 保存原始结果；Windows 双版本及 Linux Docker CI 仍待提交后验证。
- 该中间全量检查实际执行 **659 passed**，但精确覆盖率为 **89.29509970629154%**，未达 90%；Ruff、compileall 和 Diff 通过。针对新增配置、环境和导出边界补测后，25 项定向测试通过；仅在**复制的**全量 `.coverage` 上追加这些测试，诊断覆盖率为 **90.31535013139589%**。原始全量数据保持不变。
- 最终单次本机工程检查位于 `E:\TFP\onboarding-engineering-final-20260928`：**678 passed，0 failed，0 skipped**；精确覆盖率 `(9072 + 2622) / (9790 + 3148) = 90.38491266038028%`。pytest、Ruff、compileall、Diff 返回码均为 0，`summary.json` 中 `accepted=true`。原始 `summary.json` SHA-256 `30fc533439eef097002f6efc3dca84c7ea155dc46398195c090d1fd603af21f1`，`pytest.log` SHA-256 `3ba34a20892ebf6c3341d8d41054fc9584c5fbf6602c4cd4aa91f4fb9a330abb`。

本轮未调用供应商，不触碰 20 题留出集、历史账本或评分。公开测试通过不能推断真实修复成功率；本地执行仅适用于受信任仓库。下一批优先设计可验证源码、配置和未确定工具调用边界的任务 checkpoint。
