# Docker 基础迁移与官方计数器离线核验（2026-09-26）

本轮没有模型请求、正式实验或留出题执行；历史评分、账本与原始补丁未改写。执行代码为 `D:\Tracefix` 的 HEAD `5258dc44` 加本轮工作区改动；开始快照见 `runs/docker-foundation-20260926-v1/initial-working-tree.patch`，冻结原始输入见 `runs/evidence-rescue-20260926-v1/source-manifest.json`。本文记录的是派生验证，不是新的 Agent 修复成功率。

## 官方计数器

官方源码固定提交为 `8cadfede7063c896b944e7bae05daa3549ae97ea`；转换模板与编码实现哈希沿用同日 `tokenizer-qualification.json` 的只读记录。

在独立 Linux 镜像 `sha256:b64c722bfe44115de156f35079f6fe81ef4772a3dfe6fe5df0964d16607f79d0` 中安装 `deepseek-recipe==0.1.1`， wheel SHA-256 为 `7818802935becc76ac9b81af6b4a1e5390ec65355cecefd1fd4790f51845c1c5`，tokenizer SHA-256 为 `81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99`。基础 Python 3.11 镜像按 Dockerfile 中的摘要固定。实际运行断网，旧 Windows 路径经显式映射到只读 `/input`；未映射、越界或缺失路径拒绝。版本、完整依赖与安装 wheel 身份在 `runs/docker-foundation-20260926-v1/` 下保存。

诊断 `count-output/calibration/token-calibration.json` 有 793 个请求视图，其中 755 个与供应商完整 usage 配对、37 个未发送、另 1 个没有完整 usage。755 个配对的本地计数与供应商输入 usage 差值均为 0；旧字节上界/实际输入中位数 3.7233。37 个未发送请求不能据此判断可发送。文本、多轮工具调用、复杂 schema、中文、Unicode 和转义 fixture 可计数；未知模型、特殊标记字面量和非文本输入返回 `unavailable`。计数方法仍标为 `estimate`：历史一致性不能证明任意未来请求的 API 序列化契约，故**不授予硬边界资格**。新正式长上下文请求继续在付费调用前停机；未进行真实约 1M 请求。

## 三题 Linux 容器验收

按最早有证据问题且产品补丁非空的历史试次选择 pytest-10081 第 26 份、Requests-1766 第 3 份、Sphinx-10449 第 15 份。每题源码从 `inputs-v2/<task>/source.bundle` 在容器 Linux 文件系统恢复，不挂载旧 checkout；原补丁、任务、配方和协议有逐文件哈希。控制进程使用 Python 3.11；Requests 的冻结测试依赖使用独立 Python 3.9 环境。Requests 的 httpbin 服务仅在该无网络容器内启动并健康检查，测试子进程清除宿主代理变量；其他题亦断网。报告和审计证据写回新输出目录，以相对路径和 SHA-256 绑定。

| 任务 | 保存补丁 | 镜像 ID 前缀 | 两次 base | 两次 gold | 两次保存补丁 | 证据 |
| --- | ---: | --- | --- | --- | --- | --- |
| pytest-10081 | 26 | `2cc93cb7` | 断言失败 | 通过 | 通过 | `replays/pytest-dev__pytest-10081/run-2/report.json` |
| Requests-1766 | 3 | `14b0a27a` | 断言失败 | 通过 | 通过 | `replays/psf__requests-1766/run-2/report.json` |
| Sphinx-10449 | 15 | `f090d534` | 断言失败 | 通过 | 通过 | `replays/sphinx-doc__sphinx-10449/run-1/report.json` |

三题每轮节点集合一致，源码导入、依赖指纹、审计与 JUnit 完整，且没有跳过或 xfail；`replay-audit.json` 复核了 138 份复制证据的哈希。早期未合格的 `run-1` 输出保留：pytest 存档曾遗漏标签，导致版本生成偏差；Requests 曾误用 Python 3.9 运行 TraceFix 控制代码，触发 `StrEnum` 导入失败。修正后重新 staging 并运行，没有覆盖失败记录。

[SWE-bench 官方 Docker harness](https://github.com/SWE-bench/SWE-bench/blob/main/docs/reference/harness.md)采用 base/environment/instance 分层镜像；其[官方 Docker 指南](https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/docker_setup.md)要求大量磁盘空间。当前本机没有这三题的现成官方 instance 镜像；TraceFix 还要保持自己的固定测试选择、源码导入审计和严格验收口径。因此本轮使用冻结 TraceFix 配方与共用基础镜像，而未切换 SWE-bench 评分。镜像 ID、pip freeze、89 个安装 wheel 的哈希与 staged 输入清单保存在本轮目录。`flasgger` 构建 wheel 有时间相关字节差异；镜像 ID 是本轮实际执行身份，依赖 wheelhouse 本身尚不构成完全可重复构建保证。

## 状态、空间与下一步

源码提交依次为 pytest `da9a2b584eb7a6c7e924b2621ed0ddaeca0a7bea`、Requests `847735553aeda6e6633f2b32e14ba14ba86887a4`、Sphinx `36367765fe780f962bba861bf368a765380bbc68`；逐题配方指纹和输入文件哈希见各自 `inputs-v2/<task>/input-manifest.json`。

`run-identity.json` 绑定执行时 HEAD、输入清单、镜像身份文件、报告与诊断哈希；结束时再次核对冻结输入。

工程检查：新增定向回归 13 passed；完整 `pytest` 489 passed、无失败，用时 568.97 秒，但覆盖率门槛判定退出码 1。`coverage.json` 的全项目行与分支综合覆盖率为 **85.9106216437608%**，低于配置的 90%；未借无关执行凑门槛。正式源码、脚本和测试文件 Ruff 退出码 0；86 个 Python 文件逐项编译成功；`git diff --check` 退出码 0。受限 Windows 临时目录曾在 fixture 设置前产生访问拒绝，改以正常权限重跑同组定向测试通过；受限环境的全量运行主动中断，最终完整结果以前述正常权限运行计。冻结清单的 244 个文件结束时哈希均未变。

计数资格：**未取得硬计数契约**；阻碍是官方本地模板与实际 API 对任意输入的可证明适配范围，而非 Linux 安装条件。容器验收资格：**三题已取得**，仅限固定补丁和固定配方，不代表全部任务已经迁移。副本可清理性：**当前没有可确认删除的目录**；条件候选和剩余引用见 `../maintenance/docker-migration-cleanup-candidates.md`。Docker 数据文件位于 D 盘，所以这次迁移没有证明总空间节省。

下一轮先将 Agent 的修改与测试接口接入容器，并使独立验收在相同基础环境的新容器运行；验证路径隔离、费用前置停机和原目录不可访问恢复后，再逐题扩展。计数器需取得受限参数范围的官方序列化契约及异常输入边界证据，才能升级为硬计数；未满足前继续停止新正式付费请求。20 道留出题保持封存。
