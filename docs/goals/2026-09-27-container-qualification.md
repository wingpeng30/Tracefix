# 三题容器资格与恢复闭环 Goal

创建日期：2026-09-27。2026-09-27 完成 G0–G7；逐节点状态和证据见同目录 JSON。
总 Goal 已完成。验收结论保留 pytest-10081 公开整文件不合格及 G5 Sphinx recipe 元数据差异，详见最终交接与环境资格报告。

## 总目标与调度规则

零供应商调用下，完成 pytest-10081 整文件失败的可信归因，更新三题环境资格；随后完成
Docker Desktop Linux 容器断连、中断与恢复验证，修复经证据确认的缺陷，达到工程门槛并保存完整交接。
base 的目标预期失败不等于环境失败；归因完成也不自动授予整文件资格。

应用只创建一个原生 Goal。子目标由同一执行 Agent 根据
[依赖与状态 JSON](2026-09-27-container-qualification.json) 调度，不是多个原生 Goal 或后台调度服务。
每次恢复后，执行下列流程：

1. 读取 JSON，验证已完成节点的证据，选 dependencies 全部 accepted 的 pending 节点，按 priority、order 排序。
2. 将节点置 in_progress，执行“共同提示词 + 节点提示词”；保存实际命令、结果、退出码与证据哈希。
3. 逐条核验验收条件，填写 evidence、acceptance_result、completed_at，再置 accepted，自动选择下一节点。
4. 验收不通过不得解锁下游。可以按新证据自动拆分更小子节点，但必须先保存依赖、提示词、验收条件并继承全部边界。
5. 恢复时先检查实际进程、容器及输入身份；in_progress 不能推定完成，未知调用不可盲目重放。
6. 阻塞时记录原因，完成其他不依赖该阻塞的已授权工作；总 Goal 的 blocked 状态遵守工具连续阻塞规则。
7. 所有节点验收通过才标总 Goal complete；不能因轮次结束、预算将尽或“已经写了报告”提前完成。
8. 不创建额外聊天、定时自动化或付费模型工作流。

| 节点 | 优先级 | 依赖 | 交付 |
| --- | --- | --- | --- |
| G0 执行现场冻结 | P0 | 无 | 脏工作区快照、输入/镜像身份、空间与零调用基线 |
| G1 正式 Harness 矩阵 | P0 | G0 | 正式 issue 节点、async warning 节点、整文件各 × base/gold/保存补丁 × 两次，共 18 格 |
| G2 归因和环境资格 | P0 | G1 | 可证伪归因、必要修复复验、分范围资格结论 |
| G3 故障注入协议 | P1 | G2 | 故障时点、分类、恢复策略及证据合同 |
| G4 真实容器故障复现 | P1 | G3 | 断连、中断和恢复的真实 Linux 容器证据 |
| G5 恢复修复与复验 | P1 | G4 | 最小修复、故障重复两次、三题正式链路回归 |
| G6 工程与覆盖率门槛 | P2 | G5 | 全量 pytest、精确 coverage ≥90%、Ruff/compileall/diff |
| G7 审计和最终交接 | P2 | G2、G5、G6 | 完整证据索引、文档一致、历史未改 |

依赖：G0 → G1 → G2 → G3 → G4 → G5 → G6 → G7；G7 直接复核 G2、G5。

## 共同提示词

你在 Windows / PowerShell 的 D:\Tracefix 维护 TraceFix。开始读 AGENTS.md、docs/roadmap.md、
docs/development-history.md 最近记录、docs/experiments/2026-09-27-environment-qualification.md；
旧交接为 docs/handoffs/2026-09-17-v084.md。读取本 Goal 的 MD/JSON，核验依赖后执行当前节点。

- 项目供应商调用保持零，不构造真实供应商客户端，不跑付费比较或留出题，不修改历史评分、协议、账本或原始证据。
  脚本化模型替身仅证明控制流；合成成功率、离线定位和真实模型修复成功率分开报告。
- 真实任务只限 pytest-10081、Requests-1766、Sphinx-10449。源码在容器 Linux 原生文件系统执行；
  独立验收另建相同冻结镜像的新容器。Agent 不得接触 gold、验收专用测试、密钥或 Docker socket。
- 新实验、代码快照和原始证据写 E:\TraceFixRunsActive 的新目录；旧路径/junction 只读并保留，不自动删除。
  项目计划与脱敏文档写 D:\Tracefix。Windows 测试用 E:\TFP 短根统一 TEMP/TMP/basetemp/cache，
  每次使用新短子目录，避免 pytest 自动清理旧现场。写入遵守沙箱权限，不能绕过拒绝。
- 构建前核查 E 盘、Docker 实际存储盘及容器可用空间，相关盘剩余不足 10 GiB 停止新增构建；
  不依据旧报告假定当前数据盘与容量，不自动扩容或清理。
- 保留工作区所有既有修改与未跟踪文件，不 reset/clean/自动提交/推送。版本身份包含 HEAD、diff、
  实际运行 tracked/untracked 源码和脚本的快照及 SHA-256，明确范围并排除秘密和旧运行巨量数据。
  计划时 HEAD 5258dc44e324626cc9ab5345d5f7b68cc5d47613 只是背景，不代表执行版本。
- 每次实验固定代码快照、任务/源码、镜像、依赖、解释器、配方、产品/测试补丁、命令和配置、
  源码导入身份、时间、容器 ID、原始日志、JUnit/阶段审计及退出码。准备失败记录 preparation_failed，
  不计作测试失败或有效矩阵格。
- 原样复用正式 Harness 准备，包含版本文件生成和补丁应用顺序；不得另写简化准备逻辑。
  不关闭 warnings，不改产品断言，不缩小公开文件冒充完整资格，不修改历史 expected node IDs。
  诊断选择器用独立派生配置，保留差异和哈希。
- 每个代码任务结束更新开发记录和路线图，写实际改动、验证结果、限制、下一步。
  501 passed 与 coverage 83.462867% 导致 exit 1 是历史基线，不能写工程门槛已过。
- 755 个历史 usage 一致不构成未来可信硬计数契约；该契约和真实约 1M 请求验证不属于本 Goal，
  付费门槛保持冻结。不得把完成本 Goal 解释成模型修复率提高。

## G0：执行现场冻结

提示词：恢复后只读检查当前 git 状态、既有证据索引、三题输入 manifest、保存补丁与镜像。
建立唯一 evidence_root、可重建的脏工作区执行快照和哈希清单，记录无秘密的解释器/依赖环境、
旧账本与冻结输入基线。检查 Docker Desktop Linux 引擎、实际数据盘、E 盘空间，保留已有容器。
确认脚本化模型和零供应商调用守卫可用；权限、镜像或身份不符先记录并解决，不启动矩阵。

验收：执行代码不只由 HEAD 表示；代码与输入快照可校验；三个镜像核对成功；证据根、
空间和零调用基线齐备。执行期间代码变化必须另存新快照。

冻结镜像：

- pytest：sha256:1e2488a5c0e112771dec47fb1d07bd405c8c68e6973d1e91a8d85cf87c384c64
- Requests：sha256:c35e52584fb34cbb6eef9fd2ed073e166beec0467b37882938d6f8292516579e
- Sphinx：sha256:ff6e2792ee57fb20693939326b3d463915c72466a1f8a383689478935961acdc

## G1：12 格正式 Harness 对照

提示词：追踪 scripts/docker_runner_e2e.py::_fresh_acceptance、scripts/docker_reverify.py::run
及 validate_real_task_behavior / validate_agent_patch_strict 实际准备入口，然后复用它们。
若诊断选择器缺接口，最小扩展共用入口并验证，不复制准备逻辑或改正式评分。

base、gold、保存补丁分别运行正式 issue 节点、
testing/test_unittest.py::test_plain_unittest_does_not_support_async
和整个 testing/test_unittest.py，各重复两次。每格独立工作区；标明验收测试补丁是否注入，
除产品补丁外保持输入相同，不能混用 Agent 公开视图与独立验收视图。

记录 _pytest._version.py 生成、模块 __file__、sys.path、Python/pytest/pluggy、插件列表、
环境变量、pytest 配置、warning 策略、收集/执行集合、完整警告和异常栈、阶段/JUnit/退出码、
产品及测试补丁和输入哈希。准备失败先修复复用方式，失败现场另存；获取 12 格真实结果后才验收。

验收：18 格有真实执行和可核验的阶段审计/JUnit/源码导入证据；官方 issue 目标资格单独确认。
诊断节点或公开整文件可观察到未解决的测试失败，不能把 preparation_failed 冒充测试结果，也不能将其说成任务目标失败。

## G2：归因与资格更新

提示词：比较 G1，判断 warning 与环境、上游测试交互、产品补丁的关系；三组同失败不能独自证明根因。
追踪父/子 pytest、协程析构和 warning 转换，按证据增加单节点/整文件/顺序等最小单变量对照。
用可证伪证据支持主要归因，未分清则继续诊断。若应修复配方或桥接，最小修复并冻结派生身份，
保留旧镜像/输入，重复受影响矩阵及目标严格验收。更新三题环境报告，分别给目标、公开整文件、
验收整文件资格；明确模型能力未测。

验收：有支持与排除证据、复现路径和准确资格结论。若确定为上游既有缺陷或保存补丁限制，
归因可以完成，但整文件资格须保持不合格；不得通过改断言造通过。未解释清楚不得放行 G3。

## G3：故障注入协议

提示词：G2 通过后单独制定故障协议。审阅 docker_backend 会话日志、阶段身份和恢复入口，
覆盖发送前、容器已执行但响应未收到、响应收到但未持久化、验收中断、完成态恢复。
预定义 Docker 不可访问、容器不存在、容器/镜像/任务/源码身份不匹配的分类及恢复行为；
区分安全重连、固定快照新容器重验和 unknown 后安全停止，禁止盲重放未知 apply_patch/run_tests。

只对本 Goal 创建登记的容器和子进程注入，断开本试次连接；不停止全局 Docker 服务或其他容器。
不存在场景用从未存在的 ID 或专用空 fixture；不为实验删除旧现场。

验收：在实验前保存故障矩阵、注入手段、预期分类、恢复决策、日志字段与证据清单。

G3 协议已预注册，见 [`experiments/2026-09-27-docker-fault-injection-protocol.md`](../experiments/2026-09-27-docker-fault-injection-protocol.md) 与 E 盘 `g3-protocol-01/fault-injection-protocol.json`。协议将 Docker 不可访问、容器不存在、身份不匹配分别列为独立类别，规定 unknown 工具调用不重放；当前 `inspect_docker_run.py` 的完成态 Docker 错误分类有待 G4/G5 验证。

## G4：真实容器故障复现

提示词：按 G3 在 Docker Desktop Linux 引擎执行，用真实桥接进程断连、单任务控制进程中断、
自有容器停止验证；单元 mock 不代替真实故障。保持脚本化模型和零调用守卫。
Docker 不可访问不能误报 container_missing；恢复核对容器/镜像/任务/源码及输入身份。
保存中断前后事件、调用 ID、副作用、补丁和报告；unknown 保持未知，不新建假成功。
检查重复恢复不膨胀状态、不重复调用、不覆盖报告；缺陷交 G5。

验收：协议各格实际执行、分类和证据齐备，mock/真实故障区分清楚。
发现实现缺陷可交 G5，但故障复现完成不能写作恢复实现通过。

## G5：恢复修复与重复验收

提示词：对 G4 证实的缺陷最小修复，支持能证明安全的恢复，其他场景准确拒绝并留现场。
关键故障真实容器各重复两次，验证分类、幂等、导出证据、未知调用不重放、
完成态恢复不新增执行。对三题正式 Runner→MinimalAgent→五工具→新容器验收做必要回归，
保持固定身份、产品 diff 与隔离边界。无代码缺陷也须完成重复验收并说明无需修改。

验收：可恢复场景成功，不可安全恢复场景 fail closed 且分类准确；
三题目标资格/保存补丁严格复验稳定，隔离通过。整文件按 G2 结论报告，历史评分不改。

## G6：工程与覆盖率门槛

提示词：最终版本采用新 E:\TFP 短子目录统一 TEMP/TMP/basetemp/cache，运行全量 pytest。
针对新增 Docker/恢复/准备路径及未覆盖高风险分支补有意义行为回归，不堆实现镜像测试、
不执行无关入口凑覆盖。保留统计范围和阈值，单列 pytest-only 精确值；如用既有合并口径，
说明相关真实入口及数据来源，不合并不同代码版本。不靠整数舍入、降低阈值或排除源码过关。
执行必要 Ruff、compileall、diff 检查，注明源码/测试/脚本范围；旧临时夹具访问失败单列。
只在代码变化、新失败或未解决疑点需要时重跑测试，保存完整日志及最终退出码。

验收：最终版本全量 pytest 通过、精确综合 coverage ≥90%、必要工程检查通过；
不满足则继续处理或如实阻塞，不能把 501 passed 历史记录当本次完成证据。

## G7：审计与交接

提示词：核对所有 accepted 节点的 evidence 和哈希；确认旧输入、评分、账本未改，
项目供应商调用为零。分别汇总环境资格、实际恢复能力、工程结果与限制，
更新路线图、开发记录、环境报告、本 Goal 状态和新交接；提供代码快照身份、
重放命令、原始结果位置和下一步。确认没有本 Goal 必需的残留工作，保留现场。
全部节点验收通过才完成原生 Goal，付费和计数门槛仍冻结。

验收：每个结论可追溯，文档一致，没有必需待办；不得宣称模型修复能力提升。

## 原始证据起点

- 总报告：docs/experiments/2026-09-27-environment-qualification.md
- pytest：E:\TraceFixRunsActive\environment-recovery-20260927\formal-runner-pytest-pluggy100
- pytest 重复：E:\TraceFixRunsActive\environment-recovery-20260927\formal-runner-pytest-repeat
- Requests：E:\TraceFixRunsActive\environment-recovery-20260927\formal-runner-requests-repeat
- Sphinx：E:\TraceFixRunsActive\environment-recovery-20260926\sphinx-py310-diagnostic-20260926
- Windows：E:\TraceFixRunsActive\environment-recovery-20260926\final-engineering-check-v3-shortpath

## 最终执行状态（2026-09-27）

G0–G7 全部 accepted。G6 最终全量 pytest 543 passed；同一代码版本的有效入口合并覆盖率为 90.00863557858376%，pytest-only 为 85.77720207253886%，分开报告。Ruff、129 个 tracked Python 文件 compileall、`git diff --check` 通过。

G7 对 G5 Sphinx 输入身份作了明确核对：G5 使用 manifest `2ce71e…` 的 inputs-v3，基线使用 manifest `8dcbc6…` 的 inputs-v4；两份清单除 recipe JSON 外 8/9 文件哈希相同。G5 有效证明固定 digest 镜像上的 Runner/独立验收行为，但不属于相同 manifest/recipe 的重放，亦不证明从 G5 recipe 重建环境。该限制写入 G7 E 盘更正审计，原始证据未改。

继续保留的非 Goal 待办：pytest-10081 整文件 qualification 仍失败，不能通过关闭 warning 或改断言放行；未来真实模型能力、大上下文硬计数和付费/留出实验均未执行，也不是本 Goal 完成的推论。详细结果、路径及安全重放方式见 [`handoffs/2026-09-27-container-qualification-final.md`](../handoffs/2026-09-27-container-qualification-final.md)。

Goal 管理依据：[OpenAI 官方 Goals 指南](https://developers.openai.com/cookbook/examples/codex/using_goals_in_codex)。
切换模型后在本聊天执行 /goal resume。原生 Goal 状态以应用为准，JSON 只跟踪内部子目标。
