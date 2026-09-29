# Docker 容器断连与恢复故障协议

日期：2026-09-27。Goal：`tracefix-container-qualification-20260927`，节点 G3。此文件和 E 盘 JSON 在故障注入前登记；协议哈希见 JSON 自身摘要。当前状态：G3 已预注册，G4 已执行，G5 修复与复验进行中。

## 代码现状

`src/tracefix/docker_backend.py::_BridgeSession.call` 按 `dispatch_started → request_sent → process_started → result_received` 记录 JSONL 事件；异常记录 `outcome_unknown`。事件追加并 flush，但 `result_received` 只保存成功位，不持久化完整工具结果。当前会话没有重新附着接口。

`docker-run-state.json` 记录 run/task/container/image/source/recipe/input-manifest 与阶段。`scripts/inspect_docker_run.py` 只读检查固定输入、容器 ID/名称、运行标签、镜像及挂载，标示 unknown call 不重放、automatic resume=false。G4 发现 Docker 不可达和明确 no-such-object 原先都落入同类 `container_inspect_succeeded=false`；检查器不报告容器停止态；journal 的 `result_received` 被直接当作 completed，即使轨迹没有持久化该返回值。G5 正在分别分类并 fail closed。

`TraceFixRunner` 中断处理会尝试收集 diff 和导出证据。G4 确认阶段先于 `result.json` 落盘。G5 已把 terminal phase 写入移到 result 落盘之后：中断在此边界时容器阶段仍是非终态。`docker_runner_e2e._fresh_acceptance` 对未通过或异常验收保留容器；正式验收中断报告明确不授资格。

## 故障与预期策略

具体八格 F01–F08、逐格注入点、命令条件、身份字段及验收项保存在：
`E:\TraceFixRunsActive\goal-container-qualification-20260927\g3-protocol-01\fault-injection-protocol.json`。

分类和规则：

- Docker CLI 无法连接时报告 `docker_unavailable`；只能在 Docker daemon 成功返回明确 no-such-object 后报告 `container_missing`。
- 已存在但任务/run label、容器 ID/名称、镜像、源码、输入 manifest 或 recipe 不符时报告 `identity_mismatch`，禁止 exec、重启、移除或复用。
- 当前会话不能重连；匹配容器只能只读检查。`dispatch_started` 后无可持久化结果视为 unknown，禁止盲重放 `apply_patch` 或 `run_tests`。
- journal 已记 `result_received` 但 trace/result 未持久化时，不可再调用同一工具。因为 journal 不含完整返回值，需记为“结果已收到但返回值未落盘”，之后只能审查副作用或在独立新容器上复验冻结输入和已保存 diff。
- 验收中断没有身份匹配且完整的报告时，不授资格；保存旧现场，使用相同冻结输入、镜像和 diff 在新验收容器复验。
- 完成态必须先成功访问 Docker 并核实固定输入及最终报告；只有 daemon 在线且明确容器不存在时才接受已完成容器已清理。重复恢复只读，不增加事件或执行。

## 注入安全边界

只操作标签 `tracefix.goal=container-qualification-20260927` 的本 Goal 容器与本试次桥接/验收子进程。Docker 不可用通过把该次 inspector 的 `DOCKER_HOST` 指向未监听的本机 TCP 端口模拟，不停止 Docker Desktop，也不影响其他 workload。missing 使用从未创建的随机 ID；mismatch 使用登记的隔离容器和故意不匹配的标签。全部容器 `--network none`、无 bind mount、cap-drop ALL，不转交 secrets。旧 evidence 只读保留；不清理旧目录或历史容器。

每格保存注入前后 docker inspect、run state、事件日志、进程返回码、容器 ID、文件/diff/report 哈希和分类。开始前重新检查 E 盘与 Docker 数据盘剩余空间；低于 10 GiB 停止新增构建。此次协议本身不触发故障、不调用供应商、不使用付费任务或留出题。

## G4 执行记录（2026-09-27）

故障注入在预注册 JSON 所列自有容器和子进程内进行，未停止 Docker Desktop 或触碰其他容器。F01a 的首次注入边界不符合预期（journal 已有 `request_sent`），保留为无效注入；F01b 在 payload 写入前杀死精确的 docker-exec 进程，只留下 dispatch 记录并将调用保持 unknown。F02 在真实 `apply_patch` 已产生副作用、result frame 已到达 host 后丢弃 frame；外部 diff 与保存补丁一致，journal 没有 `result_received`，调用 unknown 且没有重放。F03 在 `result_received` flush 后、轨迹写入前退出；旧检查器误把它当 completed 且恢复身份返回成功。

F04 的不可达 TCP Docker host 与 F05 的在线 daemon/随机缺失容器在旧检查器中都返回相同检查失败。F04b 显示即使 phase=completed，不可达 daemon 也不会被误认作缺失或身份通过；预注册源审阅中的这一项理论担忧没有复现，故作为反证保留。F06 的 run label 错配被拒绝。F07 在正式 `docker_reverify.py` 的真实 pytest 子进程执行时 SIGKILL，报告标为不合格且审计未完成。F08 对已完成现场重复只读检查两次，没有新增调用或改写报告；G5 跟随 `result.json` 中的历史 trace 路径后发现 4 个 journal 返回值没有对应持久化 `tool_returned`，因此按 unknown 拒绝复用。F09 停止本 Goal 登记的五个长驻容器后保留容器对象；旧检查器将 stopped container 当成可继续的身份匹配。

修复和回归落在 E 盘 `g5-recovery-01` 与 `g5-recovery-repeat-02`：F04=`docker_unavailable`、F05=`container_missing`、F03=`result_received_not_persisted` 且调用按 unknown 处理、F01b stopped 容器=`container_stopped`。这些分类每个重复两次，输出稳定且运行输入未改。F08 完成态复核稳定拒绝其中 4 个未在历史轨迹持久化的结果；这个旧运行证据不满足完整恢复复用条件。身份吻合与可执行恢复分开；不可恢复分类返回非零。runtime 先落盘 `result.json` 再写 terminal phase。`tests/test_docker_backend.py` 与 `tests/test_runtime.py` 共 24 passed；最终定向 Docker backend 7 passed，Ruff、compileall、`git diff --check` 通过。三题正式 Runner 回归仍未完成；本记录不代表恢复实现最终验收。
