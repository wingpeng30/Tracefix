# 真实 GitHub Issue 任务集

这里保存 TraceFix 的第一组真实仓库任务。它们不是人工拼接的重复文本，而是从
[SWE-bench Verified](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified) 选出的
多源码文件修复实例。每题固定上游仓库、base commit、Issue、开发者补丁和测试补丁。

## 当前任务

| ID | 上游 Issue | gold 修改 | 隐藏测试文件 | 主要信息差异 |
|---|---|---:|---:|---|
| `pytest-dev__pytest-8399` | [pytest #8399](https://github.com/pytest-dev/pytest/issues/8399) | 2 个源码文件 | 2 | xunit 与 unittest 两套生成 fixture 的路径必须保持同一私有命名规则 |
| `pylint-dev__pylint-8898` | [Pylint #8898](https://github.com/pylint-dev/pylint/issues/8898) | 3 个源码文件 | 1 | 参数转换、工具函数和公共导出必须同时调整，且不能拆开量词中的逗号 |
| `sphinx-doc__sphinx-9461` | [Sphinx #9461](https://github.com/sphinx-doc/sphinx/issues/9461) | 3 个源码文件 | 4 | Python domain、autodoc documenter 与反射工具必须共同识别 class property |

`problem.md` 是去除 Issue 模板噪声、长环境回溯或重复段落后的忠实节选，清单以
`problem_statement_kind=curated_excerpt` 明确标记，不冒充数据集逐字副本。`gold.patch` 和
`test.patch` 保留 SWE-bench 实例内容；三个文件都有 SHA-256，任何换行或内容漂移都会使
加载失败。

## 隐藏验收边界

- Agent 只应收到固定 base commit 的仓库和 `problem.md`。
- `gold.patch` 是分析与可解性证明，绝不能复制到 Agent 工作区或模型上下文。
- `test.patch` 只允许在 Agent 结束后由评测器应用。
- 当前机器没有 Docker；本地 Python 3.9 兼容 harness 已证明三题的 `FAIL_TO_PASS` 均在 base
  失败并在 gold 后通过。它不是官方 `PASS_TO_PASS` 全回归，也不是 Agent 成功率结果。
- 每题列出的 7 个 `related_context_files` 只用于实验后衡量有效文件覆盖，不会提示给 Agent。

## 校验

离线校验文件、SHA-256 和补丁目标：

```powershell
.\.venv\Scripts\python.exe -m tracefix.cli validate-real-tasks `
  --tasks benchmarks/real_tasks
```

本地行为验收（不调用模型）：

```powershell
.\.venv\Scripts\python.exe -m tracefix.cli validate-real-behavior `
  --tasks benchmarks/real_tasks `
  --source-root runs/real-task-validation `
  --test-env-root runs/real-task-envs-v2
```

32k 单次预筛选（每题调用真实模型一次完整 Agent 轨迹）：

```powershell
.\.venv\Scripts\python.exe -m tracefix.cli real-prescreen `
  --tasks benchmarks/real_tasks `
  --source-root runs/real-task-validation `
  --test-env-root runs/real-task-envs-v2
```

只有预筛选 JSON 中至少三题 `eligible_for_paired=true` 时，才允许把该文件传给
`real-paired-eval`。这个硬门槛避免在没有发生压缩的信息轨迹上做昂贵的伪对照。

联网克隆三个上游提交并执行 `git apply --check`：

```powershell
.\.venv\Scripts\python.exe -m tracefix.cli validate-real-tasks `
  --tasks benchmarks/real_tasks `
  --with-checkout `
  --checkout-dir runs/real-task-validation-new
```

检出目录必须尚不存在。运行产物在 `runs/` 下，不提交 Git。若本机全局 Git 代理已失效，
应先修复代理配置；TraceFix 不会擅自绕过用户代理。

本次固定验证结果见
[`benchmarks/experiments/v0.5.0-real-task-fixture-validation.json`](../experiments/v0.5.0-real-task-fixture-validation.json)。

## 数据与许可证

任务元数据来自 SWE-bench Verified，源码和补丁仍受各自上游项目许可证约束。详情见
[`NOTICE.md`](NOTICE.md)。TraceFix 根目录的 MIT 许可证不覆盖这些第三方任务材料。
