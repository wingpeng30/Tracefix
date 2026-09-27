# 三题容器资格与恢复闭环：最终交接

日期：2026-09-27。G0–G7 均验收；原生 Goal 已完成。执行边界全程为零供应商调用、零付费实验、零留出题，历史评分/账本/协议与既有运行证据未改。脚本化 Agent 只验证 Runner 控制流与验收隔离，不能说明真实模型修复能力。

## 最终结论

- pytest-10081 官方 issue 节点：base 两次失败，gold 与保存补丁两次通过，目标节点资格通过。`testing/test_unittest.py` 整文件在 base/gold/保存补丁矩阵中重复出现 async/unraisable warning 失败；整文件资格仍不通过。warning 根因范围限于冻结 profile 下的上游子进程交互，未证明 Python 3.10.21 点版本单独致因。
- Requests-1766：固定 Python 3.9.21/pytest 4.0.2 镜像中的 `test_requests.py` 两次 90/90；Runner 独立验收重复通过。
- Sphinx-10449：Python 3.10 固定镜像目标与保存补丁验收通过；保存补丁公开文件两次 31/31。G5 的 Runner 使用 `inputs-v3`，其 Sphinx recipe 元数据是 Python 3.11/Windows；基线 `inputs-v4` 为 Python 3.10/Linux。两份 manifest 9 个文件中 8 个哈希相同，唯一差异是 recipe JSON。G5 实际 Agent 与四个新验收容器仍使用同一固定 Linux 镜像 digest、同一 source commit，且不执行构建步骤。因此 G5 支持固定镜像执行结果，不是同一 manifest/recipe 重放，也不证明从其 recipe 重建环境。差异证据：`E:\TraceFixRunsActive\goal-container-qualification-20260927\g7-audit-02\completion-audit-correction.json`。
- Docker 恢复：六类关键只读复查各重复两次，分类稳定；Docker 不可达、容器不存在、容器停止、结果未持久化明确区分。unknown 调用不盲重放；身份不匹配 fail closed。真实容器故障现场保留在 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g4-faults-*` 和 `g5-recovery-repeat-02`。
- 工程门槛：最终全量 pytest 543 passed、0 failed；有效同版本入口的精确合并 coverage 为 90.00863557858376%，超过未变更的 90% 门槛。pytest-only coverage 85.77720207253886%，单列。Ruff、129 个跟踪 Python 文件 compileall、`git diff --check` 全部退出码 0。最终证据：`E:\TraceFixRunsActive\goal-container-qualification-20260927\g6-coverage-gate-final`；pytest-only：`...\g6-pytest-only-gate-final`。

## 版本与证据索引

G6 执行身份：HEAD `5258dc44e324626cc9ab5345d5f7b68cc5d47613`；dirty status SHA-256 `4a31e90399ff4f0090c392263993d3136da1c9c3c2f1890ab208cf3383c94822`；tracked diff SHA-256 `0e544571b3a99641120eff83b21ea5e12f0feb557db9f2a7e3cbb96ad0d5d71d`。G6 新增回归限定在 `tests/test_runtime.py`、`tests/test_docker_backend.py`、`tests/test_builtin_tools.py`。详见 `g6-coverage-gate-final\execution-identity.json`。

总 Goal 状态与逐节点提示词/验收记录在 `docs/goals/2026-09-27-container-qualification.json`；最终 G7 审计与哈希索引为 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g7-audit-01`、`g7-audit-02`。G0 脏工作区快照 SHA-256 `a9efce6fe54a3d24f249879523295591eb6f8e7be8616c532bdc7570126702f5`，不等于最终 G6 执行身份；二者各自描述其版本范围。正式运行摘要与全部原始测试、容器和审计文件均留存，没有覆盖旧现场。

账本只读 SHA-256 `b811cd09abb6996bf9e0edf97f68c64fe3b63806ab73d80e9f311f0318e6a3ab`，mtime 为 2026-09-25，早于 Goal。G0 快照明确排除了 ledger/score 文件，因此不声称快照哈希证明它们未变；本 Goal 未发现对这些历史文件的写入。

## 后续优先事项

1. 若未来要声称 Sphinx 的“输入配方也可重建”，从冻结的 `inputs-v4` 复制到新 staging，先校验 manifest/recipe 一致，再用正式 Runner 将输出写入新的 `E:\TraceFixRunsActive` 目录；不要覆盖 G5 证据。当前固定镜像资格无需因此撤销。
2. pytest-10081 整文件仍是不合格状态。若要进一步定位，可另做隔离、单变量 Python/pytest warning 兼容性诊断；不得关闭 warnings、改断言或缩小公开范围来改写资格。
3. 大上下文硬计数契约和约 1M token 实测、真实模型能力比较、付费实验及留出题都在本 Goal 之外且仍冻结；Goal 完成不构成启动授权或模型效果结论。

无需为完成本 Goal 重跑测试。后续复验需继续保留工作区所有 dirty/untracked 内容；新证据写 `E:\TraceFixRunsActive`，TEMP/TMP/basetemp/cache 使用新的 `E:\TFP` 子目录，构建前重新检查 E 盘与 Docker 实际数据盘空间。
