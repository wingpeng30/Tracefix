# pytest-10081 原始 pytest 配置恢复与单题重验

基线 `f352e4b` 的 pytest 配置探测顺序由 `pytest.ini, pyproject.toml, tox.ini, setup.cfg`
改为优先 INI，目的是兼容留出集历史 pytest，但开发题 pytest-10081 同时包含 tox.ini 和
pyproject.toml。原 2026-09-17 P1 命令明确使用后者，其 addopts 中加载 `-p pytester`；
本轮离线 120 项模拟的该题 12 次验收改用 tox.ini，基础 pytester fixture 未加载，
`testing/conftest.py:133` 同名包装 fixture 因递归依赖在 setup 失败。缺失 call 阶段报告导致
`report_missing`，不是产品修复成败。原始失败示例：
`runs/ablation-rehearsal-final-20260922/simulation/trials/025.json`，旧现场保留。

最小修复只在 `benchmarks/real_recipes/pytest-dev__pytest-10081.json` 增加
`pytest_config: pyproject.toml`。没有修改测试目标、产品源码、优化、环境依赖和环境标记。
使用原 Python 3.11.16 解释器、原固定提交 `da9a2b584eb7a6c7e924b2621ed0ddaeca0a7bea`
完成唯一受影响题的独立 base/gold 重验：base `assertion_failed`、gold `passed`，
资格 `assertion_failure`；完整 node ID 与旧 P1 一致，三次环境观察的依赖指纹均为
`1586c22a191bde2d3c1b142328c4de5091ebdade37d3ea84a4cf95cab93da5df`，与旧 P1 相同。

实际命令：

```powershell
.\.venv\Scripts\python.exe -m tracefix.cli validate-real-behavior --tasks benchmarks/real_candidates --source-root runs/p2-formal-sources-20260920 --test-env-root runs/p1-revalidation-20260917/environments --task-id pytest-dev__pytest-10081 --recipes benchmarks/real_recipes --test-python runs/p1-revalidation-20260917/environments/pytest-dev__pytest-10081--rebuild-fe07ce2a47ce/Scripts/python.exe --output-dir runs/ablation-p1-recheck-20260922/behavior-validation
```

原始审计、JUnit、stdout/stderr、新 checkout 全部位于新目录
`runs/ablation-p1-recheck-20260922/behavior-validation/`。来源索引
`runs/ablation-p1-recheck-20260922/revalidation-provenance.json` 保存代码、配方、环境标记、
单题报告、原始日志和组合报告哈希。配置选择变化无需安装；运行 resolver 只检查所有权、任务和
依赖指纹，实际正常解析到原环境；环境的旧 recipe_hash 不被改写为新值。

组合文件 `runs/ablation-p1-recheck-20260922/behavior-validation-combined.json` 仅替换
pytest-10081 一条，其余 11 条旧 P1 记录逐对象不变；SHA-256 为
`58a302febbe8986e7350a6b60244a53ffb07eb89c5a4dcb23be95267fade6014`。新配方模型指纹为
`458de83ae88df1942e039f9fe3651e5eae1f86387ca9557de9edc8511025a055`。

这是环境/配置修复后的行为资格复核，不是 Agent 修复成功率；没有付费请求，也没有改写旧 120 项
模拟结果。下一步必须以新配方及组合 P1 证据冻结新协议，并在另一新目录执行完整模拟与恢复核验。
