# TraceFix 文档索引

本目录保存代码演进、实验解释和后续任务记录。README 首页只保留当前使用方法与主要结果，
完整的历史和证据从这里进入。

## 项目历程

- [`development-history.md`](development-history.md)：从 V0 接口骨架到 V0.3.2 实验可追溯性
  A/B 的代码说明、设计取舍、验证结果和 Git 版本。

## 实验记录

- [`experiments/README.md`](experiments/README.md)：所有 Baseline、压力实验、回归实验和
  A/B 的统一索引与结论边界。
- [`experiments/v0.3.0-context-3k.md`](experiments/v0.3.0-context-3k.md)：3k 压缩压力
  实验、7/10 失败分析和修复启示。
- [`experiments/v0.3.1-agent-loop-regression.md`](experiments/v0.3.1-agent-loop-regression.md)：
  补丁协议与 Agent 收尾修复后的 10/10 回归。
- [`experiments/v0.3.1-context-32k-ab.md`](experiments/v0.3.1-context-32k-ab.md)：关闭压缩
  与生产 32k 阈值的真实 API 对照。
- [`tasks/v0.3.2-traceability-and-context-tasks.md`](tasks/v0.3.2-traceability-and-context-tasks.md)：
  运行溯源、实际请求视图和四道多文件任务的实现说明。
- [`tasks/v0.4.0-independent-verification-and-paired-eval.md`](tasks/v0.4.0-independent-verification-and-paired-eval.md)：
  独立验收、测试篡改检测与交替重复实验框架说明。
- [`tasks/v0.5.0-real-github-issue-tasks.md`](tasks/v0.5.0-real-github-issue-tasks.md)：
  三道 SWE-bench Verified 多源码文件任务、固定提交和隐藏补丁接口。
- [`tasks/v0.5.1-windows-ci-portability.md`](tasks/v0.5.1-windows-ci-portability.md)：
  Windows CI 的任务哈希与深层临时目录 Git 兼容修复。
- [`experiments/v0.3.2-fixture-validation.md`](experiments/v0.3.2-fixture-validation.md)：
  多文件任务初始失败、标准补丁和隐藏测试的离线验收记录。
- [`experiments/v0.4.0-paired-context-32k.md`](experiments/v0.4.0-paired-context-32k.md)：
  24 次交替配对实验、未触发 32k 的否定性结论与下一功能决策。
- [`experiments/v0.5.0-real-issue-32k-prescreen.md`](experiments/v0.5.0-real-issue-32k-prescreen.md)：
  三道真实 Issue 的 32k 单次预筛选、0/3 入选结论和 Repo Map 功能决策。
- [`../benchmarks/experiments/v0.5.0-real-task-fixture-validation.json`](../benchmarks/experiments/v0.5.0-real-task-fixture-validation.json)：
  三个真实任务固定提交、补丁可应用性、base 失败/gold 通过和测试环境指纹的机器可读记录。
- [`../benchmarks/experiments/v0.5.0-real-issue-32k-prescreen.json`](../benchmarks/experiments/v0.5.0-real-issue-32k-prescreen.json)：
  不含本机路径和模型正文的预筛选指标与下一功能决策。

机器可读、已脱敏的指标保存在 `benchmarks/baselines/` 和 `benchmarks/experiments/`。
完整工作区、模型消息和轨迹只保存在被 Git 忽略的本机 `runs/` 中。

## 留档规范

从当前版本开始，每项代码任务结束时至少记录：

1. 任务目标、实际改动、关键设计取舍和明确非目标。
2. 涉及的主要模块、公共接口和兼容性影响。
3. pytest、覆盖率、Ruff、compileall 或其他相关验证。
4. 若有实验，记录模型、任务、预算、对照条件、Token、费用、成功率和局限。
5. 对应 commit、tag，以及是否已经推送远程。

代码任务说明进入 `development-history.md` 或单独的 `docs/tasks/` 文档；实验解释进入
`docs/experiments/`，结构化数据进入 `benchmarks/experiments/`。聊天中的结论不能作为
唯一留档。
