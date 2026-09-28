# 2026-09-28 干净 checkout 工程门槛与零调用复现

## 身份与范围

- TraceFix 候选提交：`e72139ddd39b1b78ca07e7cb99de6ca16700bb85`。
- 验收 checkout：`C:\Users\PengZixuan\.codex\worktrees\tracefix-gate-clean\Tracefix`，测试开始和结束时均为干净 tracked tree。
- 平台：Windows x64，Python 3.11.16 和 3.12.5；独立哈希锁虚拟环境。结果分别写入 `E:\TFP\tracefix-engineering-py311-e72139d` 与 `E:\TFP\tracefix-engineering-py312-e72139d`。
- 本次没有供应商模型调用、付费实验、留出题访问、MCP 接入或历史评分/账本/原始证据修改。

## 工程验收

使用 `scripts/check_engineering.py` 分别对两个解释器执行全量 pytest、Ruff、py_compile 和 diff 检查。pytest 使用独立 coverage 数据，不合并历史运行；退出码和验收结论分别记录。两版结果相同：632 passed、0 failed、0 errors、0 skipped；coverage 为 8665/9383 条语句和 2474/2992 个分支，合计精确值 `11139/12375 = 90.0121212121%`。pytest、Ruff、compileall 与 `git diff --check HEAD^` 均返回 0，两个报告 `accepted=true`。

原始结果：

| Python | 汇总 | pytest 日志 | JUnit | Coverage JSON / XML |
|---|---|---|---|---|
| 3.11.16 | `E:\TFP\tracefix-engineering-py311-e72139d\summary.json` | `pytest.log` | `pytest.junit.xml` | `coverage.json` / `coverage.xml` |
| 3.12.5 | `E:\TFP\tracefix-engineering-py312-e72139d\summary.json` | `pytest.log` | `pytest.junit.xml` | `coverage.json` / `coverage.xml` |

SHA256：3.11 `summary.json` 中 pytest log `2614bdbf8106e8b27db782fb6d2fcb55e4c91b46a3b11b3b34bb9f955d40500d`、JUnit `6d9115f49951bfc9701ccc6b873256d65c9a25a6710bfeea23b85d6a258a166b`、coverage JSON `ccd9aec8f954672186fde0ceebe692c47a17dd52518d04e925852daf67bb3efe`；3.12 pytest log `933f966885c371d48544fad9088d3d169e4499b0848eea3d6d12db29a8095cea`、JUnit `9314c399c6a8abddcfba3ba723256d65c9a25a6710bfeea23b85d6a258a166b`、coverage JSON `f8ff628b7d9e399057f60354708da1737f12bf2499bdd60c1b00487afec18015`。权威完整哈希清单以各自 `summary.json` 为准。

过程中的两个候选运行都保留在 `E:\TFP`，没有覆盖或合并：`29fb2f8` 为 89.97979798%，`c5f762e` 为 89.99595960%，均低于门槛；最终只依据 `e72139d` 报告通过。

## 干净安装与本地合成流程

editable baseline 与 Skills-only 结果位于 `E:\TFP\tracefix-editable-smoke`；wheel baseline 与 Skills-only 结果位于 `E:\TFP\tracefix-wheel-smoke`。wheel 安装环境为 `E:\TFP\tracefix-wheel-py311`，导入位置在其 `Lib\site-packages\tracefix`，通过仓库外运行并清空 `PYTHONPATH`。wheel SHA256 为 `6f654faafa959597b9ffd8c2bf1497956ec430d8216200169725e4107fd80132`。最终提交相对 wheel 构建时的源码仅新增测试和 README/工程脚本一行调整，`src/tracefix` 运行实现未改变；wheel 结果支持包内资源与入口可用，但它不是从 `e72139d` 重新构建的 wheel。

editable 与 wheel 两种安装的 baseline/Skills 两臂均完成；每臂均运行一个合成 pytest 测试并生成 app.py diff、result 和 JSONL trajectory。脚本模型请求分别是 6/7；供应商客户端构造、供应商请求和网络连接尝试分别均为 0。Skills-only 激活 `tracefix-debugging` v1.0.0，SHA256 `2275795589fa8bfc5fbad4f8d1666e2d3af95ad85413921ac788a52d39169551`。结果证明的是合成控制流和零调用保护，不证明定位质量或真实修复成功率提升。

Docker backend 的公开合成 prepare 测试以 mock Docker CLI 和 bridge 响应，同时真实生成 Git fixture source archive；检查了完整镜像 ID、固定合成身份、Skills 目录、repo map、`--network none`、无挂载以及异常/正常清理。它不是 Docker daemon 端到端执行。

## 尚未完成与下一步

- 当前机器没有可用的 Docker daemon；推送后需在 Linux Docker CI 运行 baseline/Skills 两臂，核实镜像实际源码与 Skills 哈希、轨迹和容器清理。只有该 CI job 通过，才能报告真实 Docker bridge smoke 通过。
- GitHub Windows CI 保留 Python 3.11/3.12 工程门槛以及 editable/wheel smoke；Linux Docker smoke 支持 PR、main 和 `workflow_dispatch`。本地尚未获得远端运行结果。
- Requests 历史 TLS 容器复验、pytest-10081 Python 点版本诊断、Sphinx 基线 recipe 重建和完整 Token 契约均留待后续任务。

## 验收结论

本次工程门槛和公开本地零调用控制流通过；wheel smoke 与源码版本关系按上文说明。Docker 流程已接入 CI，但远端 smoke 待验证。没有获得离线定位效果或独立真实修复成功率的对照证据，不对成功率作提升结论。
