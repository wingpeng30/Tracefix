# 2026-09-27 三类环境问题复核

## 范围与结论

本轮按 Windows 工程测试、pytest/Requests 配方、Sphinx 顺序问题的顺序复核。未调用供应商、未启动正式实验、未使用留出题，也未改写历史评分、协议或账本。原始冻结输入未修改；本轮证据及测试输出位于 `E:\TraceFixRunsActive`。

三类问题的结果不同：Windows 全量测试现在能执行完，但项目 coverage 未达到 90% 门槛；Requests 与 Sphinx 的目标环境复验稳定；pytest-10081 的冻结目标节点稳定，但整文件诊断仍有一个非目标 warning 失败，因此没有授予整文件环境资格。2026-09-27 的后续正式 Harness 对照进一步确认，base、gold 与保存补丁都复现该 async warning；同时官方 pytest-10081 issue 节点在 base 失败、gold/保存补丁通过，各重复两次。详见下方“后续 pytest 整文件矩阵”。

| 范围 | 复现与修正 | 重复结果 | 当前资格 |
| --- | --- | --- | --- |
| Windows 工程运行 | 旧 D 盘 pytest 临时目录曾报拒绝访问。E 盘探针与 `tmp_path` 最小测试此前各重复两次通过。本轮还复现了长路径：失败 workspace 为 257 字符，`.git` 路径为 262 字符；Git 虽能看到 `.git` 目录，仍报告不是仓库。改用短路径 `E:\TFP` 同时承载 `TEMP`、`TMP`、pytest `--basetemp` 和 cache。 | 最终版本全量测试 **516 passed、0 failed**，用时 622.37 秒；G6 首次 pytest-only 运行 502 项覆盖率为 83.462867%，最终 516 项运行使用 `--cov-append`，其 pytest-only 精确值未单独导出。按既有口径追加同版本的验证反馈、详细消融、三题正式 Docker Runner/新容器验收及只读环境盘点后，合并精确 coverage **89.32642487046633%**（语句 91.6837315%，分支 81.8996416%），仍低于 90%。 | 功能测试通过；G6 工程门槛未通过，唯一阻塞为精确综合 coverage 不足 90%，不是断言或 setup 失败。 |
| pytest-10081 | 冻结 tox 矩阵包含 Python 3.10；旧镜像使用 Python 3.11，不符合该任务矩阵。新镜像将任务解释器切到 Python 3.10.21、pytest 7.1.2，并固定 pluggy 1.0.0；控制进程仍为 Python 3.11。 | 正式 Runner 零模型调用。冻结目标节点 base 两次均为预期断言失败，gold 两次通过；保存补丁两次通过。四个全新验收容器均无挂载，严格审计通过。完整公开 `testing/test_unittest.py` 在两个独立 Agent 容器均为 61 passed、9 skipped、1 failed。相同失败节点是 `test_plain_unittest_does_not_support_async`：子进程中的未 await coroutine warning 被 pytest 收集为 `PytestUnraisableExceptionWarning`，与任务目标节点不同。固定 pluggy 后该整文件失败仍存在，未关闭 warning 或改断言。 | 目标节点资格通过；整文件资格未通过。剩余 warning 失败的 base/gold 整文件对照尚未形成可信结果，不能称配方完整稳定。 |
| Requests-1766 | 冻结测试调用使用旧式 `pytest.raises` 参数，需保留兼容版本；任务环境固定 Python 3.9.21、pytest 4.0.2、attrs 18.2.0 和本地 httpbin 0.10.2。HTTP/HTTPS 分别在容器内健康检查，HTTPS 使用固定测试 CA；移除宿主代理继承。增加 `PYTHONDONTWRITEBYTECODE=1` 以避免运行期 `.pyc` 造成依赖指纹变化。 | 两个独立 Agent 容器的 `test_requests.py` 诊断均 90/90 通过；formal Runner 的 base/gold 资格与保存补丁各两次严格复验均通过。镜像 `sha256:c35e52584fb34cbb6eef9fd2ed073e166beec0467b37882938d6f8292516579e`，依赖指纹稳定，模型调用 0。 | 三题范围内目标测试与独立验收通过；结果只适用于固定容器身份与该测试文件。 |
| Sphinx-10449 | 原 Python 3.11 不在冻结 tox 的 `py36`–`py310` 矩阵中。3.11 下 default-options 节点在 base、gold、产品补丁上均稳定出现同一 `__weakref__` 描述差异；同输入单节点/整文件对照未显示顺序污染。新镜像使用 Python 3.10.21、Sphinx 5.0.0 和固定 docutils 0.18.1。 | 新 Python 3.10 容器中保存补丁整文件 31/31 连续两次通过；base 与 gold 的 default-options 节点各两次通过。正式 Runner 的资格与补丁严格复验共四个新容器通过，Agent 整文件诊断 30/31（缺少验收专用测试补丁），不把它冒充为独立验收失败或全文件通过。 | 目标节点和保存补丁整文件重复结果稳定；仅限 Python 3.10 固定配方。 |

## 版本与证据身份

## 后续 pytest 整文件矩阵（2026-09-27）

使用正式 Harness 的 `validate_real_task_behavior` 与 `validate_agent_patch_strict` 准备逻辑，固定 pytest 冻结镜像、Python 3.10.21、pytest 7.2.0.dev 与 pluggy 1.0.0。所有 18 格均注入同一验收测试补丁；base、gold 与 SHA-256 `42a3091ea0436528c8ff5b90f3a577138e139f1689022a9848c441aaad99d857` 的保存补丁各对官方 issue 节点、`test_plain_unittest_does_not_support_async` 和完整 `testing/test_unittest.py` 重复两次。执行源码快照 SHA-256 `4987d501a9472ab1d82ee4e85a1e23fab885676e68f99e16e7010980690a1188`，工作区 HEAD 与跟踪差异哈希见 E 盘 attempt 05 复核文件。容器镜像、manifest、派生配方和原始归档 SHA 均写在 `g1-matrix-review.json`。

- 官方 issue 节点 `testing/test_unittest.py::test_pdb_teardown_skipped_for_classes[@unittest.skip]` 两次一致：base `assertion_failed`，gold `passed`，保存补丁 `passed`。这证明产品补丁在其任务节点上通过严格资格。
- async 节点两次重复均在 base、gold、保存补丁中失败，完整栈为未等待 coroutine 触发 `RuntimeWarning`，pytest 的 unraisable-exception hook 将其作为 `PytestUnraisableExceptionWarning` 记为子测试失败。
- 公开文件两次重复的 base 各有 2 个失败、9 个跳过；gold 和保存补丁各有 1 个失败、9 个跳过。gold 修复了验收补丁新增的 PDB 类跳过测试；余下失败都是上述 async warning。依赖指纹在每个变体前后未变，模块从当前 checkout 导入。
- 上游 `testing/test_unittest.py::test_plain_unittest_does_not_support_async` 的注释和断言原本就要求子进程输出未 await 的 RuntimeWarning 后“1 passed”。当前冻结环境实际将子测试报成 1 failed。保存产品补丁只改 `TestCaseFunction.runtest` 的 PDB teardown 跳过判断，且官方目标 gold/补丁均通过，因此该 async 失败与补丁无关；可确认是当前固定环境下上游测试与 unraisable-warning 行为的交互。没有做 Python 点版本单变量对照，故不进一步断言该差异由 Python 3.10.21 单独引起。
- 全文件行中的 8 个跳过测试只有 setup=skipped、teardown=passed、没有 call，这是 pytest 的合法跳过阶段序列。当前通用证据解析器要求每个收集节点都有 setup/call/teardown，因此将整文件执行审计标记为 `audit_available=false`。原始 execution/collection 审计、JUnit、源码导入记录均完整；独立复核对阶段、JUnit 计数、退出码、输入身份和哈希逐格交叉核对，18/18 诊断证据完整。不得把该独立诊断完整性改写成正式整文件资格通过。
- attempt 03 的符号链接导出失败，attempt 04/05 用容器内 tar 解决。attempt 05 自动摘要还因加入第 3 组选择器后仍期待 4 条命令而误报 `all_commands_completed=false`；6 条容器命令实际均返回 0，保留的 `g1-matrix-review.json` 以原始 host log 独立核实此项，并记录正式解析器的 skip 阶段限制。

证据根：`E:\TraceFixRunsActive\goal-container-qualification-20260927\g1-pytest-matrix-05`。attempt 04/05 均保留，不覆盖。attempt 05 的 `g1-matrix-review.json` 汇总每格 JUnit/审计 SHA、状态和依赖指纹；原始 1.28 GB tar、逐文件哈希清单、host log 和 `matrix-summary.json` 也保留。该结果只更新环境资格，不代表模型真实修复能力变化。

- pytest-10081 镜像：`sha256:1e2488a5c0e112771dec47fb1d07bd405c8c68e6973d1e91a8d85cf87c384c64`；Dockerfile 为 `docker/reverify-pytest310.Dockerfile`。固定 pluggy 1.0.0 后正式 Runner 仍未通过整文件诊断，因此这次锁定只提升了运行身份确定性，没有消除该单项失败。
- Requests-1766 镜像：`sha256:c35e52584fb34cbb6eef9fd2ed073e166beec0467b37882938d6f8292516579e`；Dockerfile 为 `docker/reverify-requests-v2.Dockerfile`。
- Sphinx-10449 镜像：`sha256:ff6e2792ee57fb20693939326b3d463915c72466a1f8a383689478935961acdc`；Dockerfile 为 `docker/reverify-sphinx310.Dockerfile`。
- pytest 输入 manifest：`735746a19a85f9418f73858eac690d1f78fa56854879ddce0f4b10277d64f0c1`；Requests：`0f51ff4aa4386482160b42aedf77ca02920f5fe77a43410b79c22aa42cac804e`；Sphinx v4 派生输入：`8dcbc6831af13c452cb91e260c7f5b07b501a1967d00391b00087082179b1c2e`。Runner 报告均标记输入 manifest 校验通过。
- pytest 新 Runner 摘要：`E:\TraceFixRunsActive\environment-recovery-20260927\formal-runner-pytest-pluggy100\docker-runner-e2e-summary.json`。
- pytest 整文件第二次重复摘要：`E:\TraceFixRunsActive\environment-recovery-20260927\formal-runner-pytest-repeat\docker-runner-e2e-summary.json`；重复结果与首次相同，补丁 SHA 相同。
- Requests 与 Sphinx Runner 摘要分别位于 `E:\TraceFixRunsActive\environment-recovery-20260926\formal-runner-requests-v5\docker-runner-e2e-summary.json` 与 `E:\TraceFixRunsActive\environment-recovery-20260926\formal-runner-sphinx-py310-20260926\docker-runner-e2e-summary.json`。
- Requests 整文件第二次重复摘要：`E:\TraceFixRunsActive\environment-recovery-20260927\formal-runner-requests-repeat\docker-runner-e2e-summary.json`。
- Windows 全量 pytest 日志、JUnit、Coverage JSON/XML 和退出码位于 `E:\TraceFixRunsActive\environment-recovery-20260926\final-engineering-check-v3-shortpath`。首次错误参数启动另存于同目录，退出码 4、收集 0 项，不计作测试结果。
- 短路径四用例复验日志位于 `E:\TraceFixRunsActive\_pytest_env26`。本轮执行身份为 `laptop-sqalhb3l\pengzixuan`、Python 3.12.5、pytest 8.4.2；`E:\TFP` 所有者为该账户，Authenticated Users 具备 Modify。运行前 E 盘剩余约 121.05 GiB、D 盘约 33.83 GiB；均高于 10 GiB 构建阈值。

有一份额外的 pytest 整文件 base/gold/补丁手工矩阵未能完成：它省略了项目 `setup.py --version` 生成的 `_pytest._version.py` 准备步骤，且 gold patch 应用阶段失败，因此不把它计为测试结论。已有 formal Runner 对冻结目标节点的 base/gold/补丁资格结果有效；整文件 warning 的 base/gold 对照仍是下一步需要补齐的证据。Agent 整文件 warning 在两个不同 Runner 试次中重现，足以标记为稳定的未解决诊断，不足以判定它对 base/gold 的归属。

## 工程检查与后续

最终代码状态的定向 `tests/test_docker_backend.py` 与 `tests/test_real_environment.py` 为 23 passed；工程源码与脚本范围 Ruff、`compileall src scripts`、`git diff --check` 均通过。501 项全量 pytest 是镜像摘要更新前的全套执行记录；镜像映射更新后，相关 23 项定向回归及 pytest formal Runner 均通过，未再花 10 分钟重复全套。覆盖率值保持原样报告，没有追加无关执行补足。

三个环境问题的状态：Windows 临时目录权限与长路径已通过短路径执行方案解决；Requests 与 Sphinx 三题配方通过固定身份的目标范围重复验收；pytest-10081 的公开整文件仍有一个 warning 失败，需在正确运行准备下补做 base/gold/保存补丁完整文件对照。不要因此扩大到其他任务，也不要启动付费实验。约 1M token 请求资格仍由独立计数契约门槛控制，本轮未测试大上下文请求。

## 2026-09-27 G5 恢复复验与 G6 最终工程结果

G5 的 G4 故障复现和首轮修复详见 [`2026-09-27-docker-fault-injection-protocol.md`](2026-09-27-docker-fault-injection-protocol.md)。修复后的只读检查在真实现场双重复：Docker 不可达 `docker_unavailable`、在线 daemon 下容器缺失 `container_missing`、完整身份但停止 `container_stopped`、journal 收到结果而 trace 未落盘 `result_received_not_persisted`。未知工具调用始终保留 unknown 且不重放；完成态旧 F08 证据中 4 个结果在轨迹里缺失，因此检查器拒绝复用。三题正式零模型请求 Runner 各跑一轮；每题目标资格和保存补丁各在两个新冻结镜像容器通过，严格审计、无挂载和 Agent-only sentinel 隔离通过。原始报告位于 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g5-recovery-repeat-02` 与 `g5-formal-reruns`。这些控制流/题目资格结果不是模型修复成功率。

G6 最终工作区全量 pytest **516 passed、0 failed**，覆盖率门槛令 pytest 退出码 1。Coverage.py 原始范围与 `fail_under=90` 未改。该工作区代码版本下，累计数据包括全量 pytest、有效的 `p2-diagnose --validation-feedback`、`p2-diagnose --detailed-ablation`、三题 `docker_runner_e2e` 和只读环境盘点；这些入口均在同一工作区代码版本执行，离线分析只读旧开发集并写入新 E 盘目录。合并精确覆盖率为 **89.32642487046633%**，语句覆盖率 91.6837315%、分支覆盖率 81.8996416%，严格低于门槛；未通过阈值，不宣称工程门槛完成。独立 pytest-only 值只有 G6 首次 502 项运行得到 83.462867%；最终 516 项使用 `--cov-append`，未单独导出该轮 pytest-only coverage。

有一条额外的历史验证反馈诊断因输入批次没有 48-position validation-closure 协议而在分析前拒绝，归类为诊断准备失败，不计作有效诊断或 coverage 证据。为 Docker bridge 超时 unknown、握手身份拒绝、清理 run-label 身份校验和准备阶段输入/镜像/source-tag/保留路径保护增加行为回归；`tests/test_docker_backend.py` 最终 21 passed。最终静态检查覆盖 129 个跟踪 Python 文件：Ruff 0、compileall 0、`git diff --check` 0。Execution HEAD、脏 diff/status 哈希和相关源码文件哈希见 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g6-engineering-final2\execution-identity.json`；完整 pytest/JUnit/coverage/静态检查工件同目录。G6 因精确覆盖率短缺 0.6735751295 个百分点尚未验收，Goal 停留 G6；不得降低阈值或使用无关入口凑覆盖率。

## G6 Docker Runner 终态持久化复跑

新增 Docker Runner 终态持久化回归后，规范路径下全量 pytest 为 517 passed，0 failed，589.73 秒。`TEMP`、`TMP`、basetemp、pytest cache 均固定到 E:\TFP；未产生路径警告。Coverage.py 精确值为 89.33506044905009%，语句 91.68373151308305%、分支 81.93548387096774%，仍未达到 90% 门槛；pytest exit 1 仅因 coverage。相关新测试验证 `result.json` 持久化早于终态 phase，但既有合并数据已覆盖此生产行，覆盖总数未增加。Ruff、compileall 与 `git diff --check` 通过。完整证据位于 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g6-runtime-durable-phase-final`；G6 保持 in progress，下一步审查未覆盖分支中可独立验证的行为场景。

## G6 pytest-only coverage

为最终 517 项版本另用全新 `COVERAGE_FILE` 运行纯 pytest：517 passed，604.11 秒，pytest exit 1 仅因 coverage。纯 pytest 精确总覆盖 84.4041450777202%，语句 87.13310580204778%、分支 75.80645161290323%。TEMP/TMP/basetemp/cache 均位于 E:\TFP。此数值独立于同版本合并有效 G6 入口数据 89.33506044905009%；两者均低于 90% 门槛。完整日志、JUnit、coverage JSON 和退出码位于 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g6-pytest-only-gate-final`。

## G6 最终门槛与 G7 审计补充（2026-09-27）

稍后同版本最终结果取代上述早期 G6 状态：全量 pytest **543 passed、0 failed**；Coverage.py 原始 `fail_under=90` 未变，同一工作区版本下 pytest 与可归属的正式 Harness/只读诊断入口精确合并覆盖率 **90.00863557858376%**（8108/8790 语句，2315/2790 分支），门槛通过。独立 pytest-only 为 **85.77720207253886%**，不能与合并口径混称。Ruff、129 个跟踪 Python 文件 compileall、`git diff --check` 均为退出码 0。最终日志、coverage 与静态检查见 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g6-coverage-gate-final`；pytest-only 见同级 `g6-pytest-only-gate-final`。

G7 复核补充：G5 Sphinx Runner 的 `docker-run-state.json` 显示输入根为 `environment-recovery-20260926\inputs-v3`，manifest SHA-256 `2ce71e278c3d50905775b92bd717317dd5acd2a01fda97d0a111d315ca2884a1`；G0 锁定的上一份正式 Sphinx 验收来自 `inputs-v4`，manifest `8dcbc6831af13c452cb91e260c7f5b07b501a1967d00391b00087082179b1c2e`。清单中的文件哈希逐项对照后，只有 recipe JSON 不同：G5 为 Python 3.11/Windows 元数据，基线为 Python 3.10/Linux 元数据；task/source bundle/problem/candidate/gold patch/test patch/product patch/protocol 哈希相同。G5 Agent 容器及其四个独立验收容器均使用冻结 Sphinx Linux 镜像 `sha256:ff6e…`，且无 build steps；源码 commit 也相同。因此 G5 的真实执行可支持固定镜像上的资格/保存补丁验收，但不是完全相同的 manifest/recipe 复跑，不能作为从该 recipe 重建环境的证据。原报告、输入和现场保持原样，差异已纳入审计限制。

总资格边界不变：pytest-10081 官方 issue 节点 base 两次失败、gold/保存补丁两次通过；完整公开测试仍两次复现 async/unraisable warning 失败，整文件资格不合格。Requests-1766 `test_requests.py` 为 90/90 重复通过；Sphinx 保存补丁整文件在 Python 3.10 冻结镜像为 31/31 重复通过。Docker 断连、容器缺失/停止、身份错配、结果未持久化及中断恢复按证据分别分类；unknown 调用不重放。上述 Runner 使用脚本化 Agent、零供应商请求，不能推断真实模型修复成功率变化。Goal 全程没有付费实验、留出题或历史账本/评分改写。
