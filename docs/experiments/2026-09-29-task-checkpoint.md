# A 批：本地任务 checkpoint 与安全恢复

日期：2026-09-29。基线 `1bc3e50a3dd9ffd61d1b14061c32fc6949aa12a1`，计划提交 `963b644`；最终代码提交和 PR CI 以推送后补记。执行环境为本机 Windows 11、Python 3.12.5、普通本地后端；模型为 `offline/replay` 录制响应，供应商调用为零。未访问留出题，也未修改历史评分或账本。

本批实现版本化原子 checkpoint、OS 释放的运行锁、完整历史与 Agent 运行期记忆恢复、Skills 加载额度恢复、源码／配置／工具／解释器及依赖身份预检、轨迹前缀核验及未知调用阻断。恢复继续使用原 checkout，轨迹追加保存，旧结果另存。运行报告增加恢复段与最近测试事实。公开测试证据绑定测试前后产品 Diff；pytest 虽返回零但修改源码时不授予有效验证。

支持边界：只支持普通本地运行、同机原 checkout 和确定完成的工具批次。Docker、环境配方及自定义测试环境变量不生成此版会话快照。快照前缀之后若存在模型／工具执行事件，自动恢复拒绝。原始运行与续跑的合成成功只证明 harness 行为，不能推断真实模型修复能力。

已获得的原始结果：

- `python examples/replay_resume.py --output runs/replay-resume-b-20260929` 返回 0；初始 `interrupted`，恢复后 `completed` 与 `verified`，6 次离线模型请求；报告和导出补丁生成，源仓库状态干净。结果位于 `runs/replay-resume-b-20260929/`。
- `python -m pytest tests/test_recovery.py tests/test_checkpoint.py tests/test_report.py tests/test_skills.py tests/test_builtin_tools.py -q -x -p no:cacheprovider --basetemp E:\TFP\pytest-recovery-final-subset-20260929`：100 passed、退出 0。随后新增任务事实请求锚点，定向 `test_minimal_agent.py`、`test_recovery.py`、`test_context.py` 为 41 passed、退出 0；完整最新代码以最终工程门槛为准。
- `python -m ruff check src/tracefix tests/test_recovery.py tests/test_checkpoint.py examples/replay_resume.py` 与对应 `compileall` 均返回 0。
- 中间全量工程进程 `E:\TFP\engineering-recovery-a-20260929` 在代码更新后手动停止，不作为验收。`E:\TFP\engineering-recovery-final-20260929` 中 686 项测试通过，但覆盖率仅 89.47%，工程门槛未通过。随后新增 checkpoint 格式、未知调用及 Skills 恢复故障测试，修复 Windows 换行造成的 Skills 内容哈希误判。
- 最终本机工程检查：`python scripts/check_engineering.py --output E:\TFP\engineering-recovery-gate2-20260929 --python python --diff-base 1bc3e50a3dd9ffd61d1b14061c32fc6949aa12a1`；原始 `summary.json`、`pytest.log`、`coverage.json`、JUnit、Ruff／compileall／Diff 日志均在该目录。退出 0，`accepted=true`，708 passed，纯 pytest 分支覆盖率 90.06088168414875%；Ruff、compileall、Diff 退出码均为 0。运行时工作树未提交，代码提交 SHA 须以最终提交为准。
- GitHub CI #24 对 `33885c7`：Windows 3.11／3.12 均 708 passed、0 skipped，editable/wheel smoke 均通过，Linux Docker job 通过；但两版覆盖率均为 89.94351940145236%，低于 90%，因此整体失败。原始日志及工程 artifact 位于 Actions run 36530202786。随后增补进程锁重入、损坏会话及 checkpoint 内部状态拒绝测试，并修复缺失任务记忆字段时检查接口返回泛化错误的问题；后续门槛与提交结果另行记录。
- 修复后的本机门槛：`python scripts/check_engineering.py --output E:\TFP\engineering-recovery-gate3-20260929 --python python --diff-base 1bc3e50a3dd9ffd61d1b14061c32fc6949aa12a1`；`summary.json` 中 `accepted=true`，711 passed，纯 pytest 分支覆盖率 90.17090882417664%，Ruff、compileall、Diff 均退出 0。Windows 3.11／3.12 的最终 CI 仍需新提交运行证明。

下一步：推送 A 批并确认 Windows 双版本 CI、仓库外 wheel 安装；再进入 B 批的统一配置、准备预检及独立验证。真实模型验收仍需独立付费方案和授权。
