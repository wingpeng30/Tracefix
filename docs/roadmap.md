# TraceFix 路线图

## 2026-10-06 A/B/C留出对照实施中

已授权新300元账本、20道留出题每臂三次，共180次；先修复评测器和复验资格，
精确实现CI及全部离线门槛通过后才付费。新配置与平衡排程已实现，旧结果不变；
尚无新模型效果结论。记录见[本轮实施记录](experiments/2026-10-06-abc-holdout-capability.md)。

## 2026-10-06 B/C扩大额度实测完成

固定20次、345个请求全部完成并结算，官方计数差异零。B/C独立补丁通过均6/10，
正常结束8/10与9/10，正常且通过5/10与6/10；保守费用10.696160/5.338980元。
C−B为0pp，95%任务级bootstrap区间[−30,30]，不宣称成功率提升。
B三次补丁收集遗漏测试临时产物，原失败保留；付费后修复共享保护集，未补跑。
冻结付费实现09f97556的五项CI全过；后续修复与正式报告另做最终验收。
详见[`完整报告`](experiments/2026-10-06-bc-capability-tokenizer.md)。不再执行新增付费调用。

## 2026-10-06 新执行任务

用户新授权B/C各25元、固定十题各一次，并要求尽可能取消人为Token与执行限制。
新增官方tokenizer计数入口和费用约束能力配置；上一轮212请求离线计数全部与usage一致。
先完成20次离线闭环、资格、工程和精确CI，再付费；不续旧账本，不访问留出集。
详见[`本轮记录`](experiments/2026-10-06-bc-capability-tokenizer.md)。

## 2026-10-06 当前结果

固定十题三臂各三次的付费对照已完成90次运行、212个已结算请求，保守峰值费用4.903642元。
A裸模型0/30、B最简工具循环2/30、C TraceFix6/30；C−A为+20个百分点（95%区间[0,43.33]），
C−B为+13.33个百分点（[−6.67,40]）。区间含零，不宣称稳定能力提升。
通过数指独立验收的产品补丁；B/C全部运行最终因预算停止，完整会话结束率另行说明。
精确评测实现e98279c的五项CI全部通过，Windows3.11/3.12各1124项、零跳过，精确覆盖率均超过90%。
原始协议、逐任务/逐次表、账本及证据索引在runs/agent-model-comparison-20261005；
[`实验报告`](experiments/2026-10-05-agent-model-comparison.md)记录全部失败、资源及范围限制。
下一步建议依据原始请求离线分析保守输入预留与上下文负担；本轮没有修改产品策略，留出集保持封存。

## 2026-10-05 当时执行计划

用户已新授权三组冷启动付费对照：裸模型、最简工具循环、当前TraceFix；固定十题各重复三次，
共90次任务运行、人民币20元上限。先完成离线资格与精确提交工程/CI门槛，再发起付费请求。
复用已知开发任务，不访问留出集、不续用旧额度；尚无本轮能力提升结论。
记录见 [`experiments/2026-10-05-agent-model-comparison.md`](experiments/2026-10-05-agent-model-comparison.md)。
G4的最终文档提交bc7dd7d已通过精确CI #110/#111；以下旧“待通过”状态保留为历史。

## 2026-10-04 当前执行计划

用户批准按 G1 经验 Skills 长期记忆、G2 连续对话、G3 普通 Docker 重建恢复、G4 联动验收推进。
新功能默认关闭，仅离线验收；各阶段精确候选 CI 全部门槛通过后才创建正式 PR。
G1 已通过精确 CI #98 并提交正式 PR #17；G2 已通过精确 CI #100 并提交正式 PR #18。
G3 已通过精确 CI #104 并提交正式 PR #19；三个 PR 均尚未合并。
G4 精确 CI #108 已在提交7208eab全部五项通过；Windows3.11/3.12各1078项全量测试通过，
覆盖率分别90.0467%和90.0581%，Linux Docker/MCP门槛通过。PR #20已创建且未合并。
当前补录CI与artifact证据的文档提交仍须通过精确CI，随后才完成G4验收。
详细状态见 [`tasks/2026-10-04-memory-dialogue-docker.md`](tasks/2026-10-04-memory-dialogue-docker.md)。
下列“下一步”保留为历史，旧付费额度不续用。

## 2026-10-02 离线预算调查

27 个真实请求与第四次本地拒绝已复现。重叠读取主要补取被裁剪或压缩退出的证据；32,000 单请求估算阈值与累计预算不协作。两个调查候选均不直接采用，下一批先做默认关闭的定向读取可见性契约；不放宽正式计量保护，不执行第五次付费运行。详见 [`experiments/2026-10-02-offline-budget-investigation.md`](experiments/2026-10-02-offline-budget-investigation.md)。以下保留历史状态。

## 2026-10-02 真实实验后的优先级

新授权四次运行后停止：一项完成并独立通过，两类复杂任务未完成；请求前预算拒绝的已知／未知计量语义已离线修复。下一步用真实历史离线改善定向读取和预算内上下文选择，不新增大模块或扩大付费预算。证据见 [`experiments/2026-10-02-public-three-types-live.md`](experiments/2026-10-02-public-three-types-live.md)。

## 2026-10-02 当前状态

空输入、解析协作、资源生命周期三个公开任务已通过冻结资格矩阵；PR #12/#13 已合并，最新功能 merge `47f2619`。Windows 双版本安装包闭环与现有 Linux 门槛通过；本批零供应商调用，纠正参考不等同于原上游修复。下一步申请新的五次／五元真实验收，并先补齐独立实验账本和回归目标预检；详见 [`tasks/2026-10-02-three-public-task-types.md`](tasks/2026-10-02-three-public-task-types.md)。以下保留历史优先级。

## 2026-09-30 当前执行计划

2026-09-30 PR #9 `codex/gated-regression-feedback` 已合并，merge commit `b18958b8342b0642b7f4fe828c39852ada769319`。精确 PR head `cb5571815a09f831cde0fd1f648059b6fdff6877` 的 CI #73 五个 job 全部成功；CI #72 对应代码 head `9501ee65c523f129074756f10573c2de518f4479`，Windows 双版本覆盖率均为 90.0006666%，Ruff、compileall、editable/wheel smoke 和 Linux Docker/MCP job 通过。结束前自动回归门与 Agent 反馈闭环默认关闭；本批零供应商请求，不声称总体修复率提升。实现边界和历史验证见 [`tasks/2026-09-30-validation-feedback-gate.md`](tasks/2026-09-30-validation-feedback-gate.md)。

下一步先冻结可复现的公开任务契约，再决定是否启动模型诊断。候选审查显示 `sphinx-doc__sphinx-10323` 适合优先资格预检；`psf__requests-1724` 有外部 HTTP 服务和任务测试目标不一致风险；`pylint-dev__pylint-4604` 是历史重用样本且基线测试补丁有收集依赖。尚无多类型任务集通过本轮 base/gold 复验，因此不将这些候选写成已冻结或已验证。筛选记录和停止条件见 [`tasks/2026-09-30-public-task-qualification.md`](tasks/2026-09-30-public-task-qualification.md)。

2026-09-30 下一批聚焦补丁验收可信度：普通本地运行支持在原始与补丁 checkout 中追加公开回归目标，并在报告中显式展示发现的回归。Boltons 真实运行的派生诊断纠正了对旧 `count` 参数的过度判断；该差异属任务未规定的接口行为。实现与边界见 [`experiments/2026-09-30-regression-verification.md`](experiments/2026-09-30-regression-verification.md)。

普通受信任 Python/pytest 仓库的本地闭环、A 任务事实与安全恢复、B 普通用户路径、C 可选只读 MCP、D 普通仓库 Docker，以及真实模型验收与计量／压缩修复，已按 PR #1–#6 顺序以 merge commit 合入 `main`（`7a28687f897821b9600cc4a9e8bf8da583ec7d45`）。8/8 次真实运行结果见 [`experiments/2026-09-29-live-model-acceptance.md`](experiments/2026-09-29-live-model-acceptance.md)。具体边界见 [`tasks/2026-09-29-agent-harness-completion.md`](tasks/2026-09-29-agent-harness-completion.md)。

进展更新：A–D 的工程验证已通过；C 的隔离查询原始证据见 [`experiments/2026-09-29-serena-mcp-zero-call.md`](experiments/2026-09-29-serena-mcp-zero-call.md)，D 的普通仓库容器证据见 [`experiments/2026-09-29-ordinary-docker-zero-call.md`](experiments/2026-09-29-ordinary-docker-zero-call.md)。PR #6 的 [CI #50](https://github.com/wingpeng30/Tracefix/actions/runs/36588295467) 五个 job 全部通过。真实模型 8 次运行包含预算停止、Skills 加载、恢复和压缩触发；小样本不代表总体修复成功率。

## 2026-09-28 当前执行顺序

用户已将重点转为系统本身：首先交付普通受信任 Python/pytest 仓库的安装、配置、预检、运行、报告与补丁导出；随后完成任务事实与 checkpoint/恢复；再评估可选只读 MCP 和普通仓库 Docker 执行契约。此前作品集优先的条目保留为历史状态。当前能力边界见 [`reviews/2026-09-28-system-review.md`](reviews/2026-09-28-system-review.md)，入门流程见 [`ordinary-repository.md`](ordinary-repository.md)。

## 历史优先级（2026-09-28 较早版本）

尽快交付**可用、可演示、能形成简历亮点的 Coding Agent 项目**。当前工程底座已通过 CI，
下一步按“可视化修复报告与公开 demo → 真实案例与最短使用路径 → README/演示及候选包”推进。
算法收益与全部历史环境资格不再作为首版交付前置条件。pytest-10081 点版本诊断、Sphinx 历史配方重建、
Requests 整批历史复验及完整 Token 契约移出当前交付主线；已有正式请求保护仍保留。
完整优先级、完成标准、时间盒和停止规则见
[`tasks/2026-09-28-portfolio-release-plan.md`](tasks/2026-09-28-portfolio-release-plan.md)。
下文保留历史路线与证据；旧记录中的“下一步”与本计划冲突时，以本节为准。

2026-09-27 容器资格与恢复闭环 Goal 已完成 G0–G7。pytest-10081 官方 issue 节点资格通过，但公开整文件仍因可重复的 async/unraisable-warning 交互不合格；Requests-1766、Sphinx-10449 的固定镜像目标与保存补丁验收通过。Sphinx G5 重跑的 manifest 与基线不同，差别仅在 recipe：执行仍使用相同冻结 Linux 镜像、相同源码和其余相同输入；因此此证据支持该固定镜像上的运行，不证明从 G5 recipe 重建环境。真实容器故障复现、fail-closed 分类与恢复重复检查完成。G6 最终全量 pytest 543 passed，精确合并 coverage 90.00863557858376% 通过既有 90% 门槛；pytest-only coverage 85.77720207253886% 单列，Ruff、129 个跟踪 Python 文件 compileall、diff 检查通过。全程零供应商调用；脚本替身贯通不表示模型修复成功率提升；付费实验、留出题、账本与历史评分保持冻结。

2026-09-27 Skills/MCP 接入：TraceFix 内置 `tracefix-debugging` skill 已完成默认关闭的按需加载实现，含运行轨迹、哈希身份和压缩后 system 锚点；fixture 与全量测试通过。MCP 单独评估后暂不接入 Serena：其语义引用能力有潜在补充价值，但当前源码/容器与异步会话生命周期、工具白名单和冻结依赖尚未完成零费用端到端验证，故不引入空壳适配器或依赖。没有离线定位或真实独立修复收益结论。配置与证据见 [`experiments/2026-09-27-skills-mcp-integration.md`](experiments/2026-09-27-skills-mcp-integration.md)。

2026-09-28 当前 checkout 复现与 Skills Docker 链路：实现 bridge 技能目录传递、UTF-8 字节限制、参考文本去重、运行轨迹记录、可压缩上下文锚点、请求序列哈希契约及本地/Docker 零调用复现入口；Requests TLS 在实际测试进程与新独立验收容器通过。最终 pytest-only 为 602 passed，coverage JSON 精确覆盖 10803/12054 = 89.62170233947238%，低于 90%；pytest 日志报告门槛失败，但旧的退出码文件为 0，不能据此断定原始子进程返回码。勘误见 [`experiments/2026-09-28-engineering-evidence-correction.md`](experiments/2026-09-28-engineering-evidence-correction.md)。pytest-10081 点版本归因、Sphinx 基线 recipe 重建和跨平台完整依赖哈希锁仍待补齐。具体实现、证据位置和限制见 [`tasks/2026-09-28-current-checkout-reproduction.md`](tasks/2026-09-28-current-checkout-reproduction.md)。

TraceFix 的目标是在固定模型和预算下，通过代码检索、上下文管理和测试反馈，减少真实 Issue
修复的输入 Token 与耗时，同时维持或提高独立验收成功率。

长期执行目标（2026-09-26 确定）：Agent 在容器内修改并测试代码；独立验收使用相同基础环境的**新容器**。近期只按需迁移受 Windows 权限或依赖影响的题目，不一次构建全部任务镜像。容器环境稳定、源码存档可独立恢复且证据完整后，才扩展迁移；不自动启动付费实验或留出集。

| 阶段 | 状态 | 验收门槛 | 证据与下一步 |
| --- | --- | --- | --- |
| Agent 核心、工具与轨迹 | 已完成 | 合成任务闭环、预算与轨迹可追溯 | V0.1–V0.3 开发记录 |
| 上下文压缩、行动效率、Repo Map | 已完成基础实现 | 可关闭模块、离线检索对照 | 12 题 Repo Map Hit@5 75%，MRR 0.465 |
| 真实候选与固定源码 | 已完成 | 12/12 commit、工件、组合补丁预检通过 | `benchmarks/real_candidates/` |
| 可信行为验收 | P0/P1 已完成并持续复核 | 报告完整性、源码/目标/环境一致；12 题均有本轮分类 | 10/12 行为资格（8 常规、2 受审查收集失败）；V0.8.10 补验 pytest 配置差异 |
| 真实 Token 对照 | P2 首轮完成并统一审计 | 每题 C/T 各三次；独立验收、Token、耗时和费用完整 | 60/60 证据有效；C 8/30、T 7/30；压缩 0 次触发，含 1 次基础设施事故 |
| 开发集单项消融 | 120 项正式运行及离线详细诊断完成 | 全计划有效、同版本执行、零调用恢复、配对与机制证据核验 | 普通集基线 13/24、Repo Map 10/24、行动 11/24、上下文 14/24；无组满足预定净收益条件，保留基线；未证明实现缺陷 |
| 工具输出呈现单变量验证 | 开发集 60 项已运行；未满足配置采纳门槛 | 固定 10 题、C/T 各三次；完整预算与独立验收证据 | C/T 普通题均 12/24；T 输入 Token 少 10.51%，但 Agent 用时多 18.83%；58/60 证据有效，保留基线，不运行留出集 |
| Agent 测试反馈与验证闭环 | 已完成正式开发集比较；处理配置不采纳 | 固定 8 道普通任务、C/T 各 24 项，证据 48/48 有效 | C 15/24，T 12/24；T 多用输入 300,066、Agent 时间多 167.443 秒；保留 C 基线。先分析空补丁与 pytest 失败，不进入留出评测 |
| 重复无效果补丁恢复门槛 | 零费用 fixture 预检与工程门槛已完成；真实效果待同期比较 | 正式比较仅一个配置差异；8 道普通开发题 C/T 各 24 项，预注册采纳规则 | fixture 控制流通过；全量 457 passed，pytest-only coverage 86.073278%，合并只读诊断后 90.149626%；余额不足完整批次，执行器接线与重新核账后再决定启动 |
| 最终评测与对照 | 未开始；20 题留出集保持未使用 | 仅在开发集选定并冻结优于基线的配置后做预注册比较 | 当前没有满足采纳门槛的改进配置，不启动留出评测 |

V0.8.4 历史记录报告 8 题常规 base 失败/gold 通过、2 题受审查收集失败/gold 通过；
这是当时验收器的资格输出，不是 Agent 修复成功率。2026-09-17 已完成 P0 安全与证据收尾：
P0 完成时全量测试为 239 passed、精确综合覆盖率 90.052614%。P1 实现后的最终回归为 246 passed、
精确综合覆盖率 90.01458907440428%，报告、源码、目标和环境边界已有负例回归。随后 P1
在新管理环境中完成 12 题复核，得到 8 道常规资格、2 道受审查收集失败资格、Requests-1724 未复现和
Sphinx-10323 待平台验证。该结果仍只表示行为资格，不是 Agent 修复成功率。下一步固化付费实验配置。Sphinx-10323 待平台验证，
Requests-1724 在现有 Python 3 环境中未复现，不为提高合格数放宽标准。

详见 [`handoffs/2026-09-17-v084.md`](handoffs/2026-09-17-v084.md)。

长期功能由失败证据决定：早期事实丢失时改进证据摘要；无效搜索时改进检索排序和读取记忆；
独立验收失败时增加补丁验证；环境差异反复出现时加入容器化。

2026-09-21 使用供应商账单闭合旧批次费用。全部 TraceFix 历史账单为人民币 3.24553320 元；修正版
`e464f9b` 的全量工程验证为 276 passed、综合覆盖率 90.06228373702422%。真实小额探测返回
`deepseek-flash`，usage、缓存明细和共享账本完整。60 项正式运行因自动审批要求用户明确授权把任务、
源码片段和工具输出发送给 DeepSeek 而暂停，尚未启动。随后用户明确授权外发范围，60/60 项正式运行和
恢复检查已完成。控制组成功 8/30，实验组 7/30；实验组输入 -2.73%、Agent 用时 -6.14%，但成功率
下降 3.33 个百分点，尚未同时达到长期目标。详见
[`experiments/v0.8.6-p2-formal-rerun.md`](experiments/v0.8.6-p2-formal-rerun.md)。
离线诊断纠正了“基础设施失败 0”的结论：第 59 项发生 Windows 账本替换故障；17 项属于正常预算
停止。T 组上下文折叠 0 次，因此输入 -2.73% 不能归因于压缩。下一步先做 Repo Map 与工具结果精简
的离线归因，再决定单项消融。详见
[`experiments/v0.8.6-p2-formal-diagnostic.md`](experiments/v0.8.6-p2-formal-diagnostic.md)。
证据统计和评分规则已在 V0.8.7 统一；本地没有可充当独立留出集的未查看任务。下一阶段先补充并
冻结 20 道新留出任务，再按已冻结的单项消融方案决定是否开展新的付费运行。详见
[`experiments/v0.8.7-p2-evidence-contract.md`](experiments/v0.8.7-p2-evidence-contract.md)。

2026-09-21 已从不可变 SWE-bench Verified revision 冻结 69 道候选顺序，并完成首轮 19 道
base/gold 验收；当前只有 4 道普通资格，16 道缺口主要来自历史 Python/测试依赖配方尚未冻结，
不能宣称 20 题留出集完成。两道合成长上下文机制任务的确定性回放均超过 32k 并成功折叠，继续与
真实留出集效果估计分开。详见
[`experiments/v0.8.8-holdout-freeze.md`](experiments/v0.8.8-holdout-freeze.md)。

2026-09-22 完成兼容配方重验与严格冻结：20 道普通资格留出任务已按固定顺序冻结，四个仓库各 5 道；
首轮 4/20 仅保留为历史记录。两道长上下文机制任务同时通过压缩触发、关键事实保留和 fixture 行为验证。
下一阶段可先在 10 道开发集上执行预先设计的单项消融，配置冻结后再一次性评测留出集；不得用留出集
结果调参。详见 [`experiments/v0.8.9-holdout-final-freeze.md`](experiments/v0.8.9-holdout-final-freeze.md)。

2026-09-22 V0.8.10 完成四组协议、完整依赖锁、20 题更严格的原始证据复核和本地 httpbin 服务配置。
真实开发题 120 项无答案模拟及正式入口 120 项合成模拟均完成，恢复不增加调用；前者 96 项预期断言
失败、24 项受审查收集失败，后者 120 项 fixture 修复通过，二者均不构成模型效果结论。全量 335 passed，
综合覆盖率 90.98527475158626%，Ruff/compileall 通过。首个模拟批次发现的 pytest 配置问题已修复重验，
历史现场保留。下一步先明确开发集阶段预算：余额 ¥70.70445880，120 项固定预算的保守费用上界为
¥103.20，不能默认增加原 ¥100 上限。随后运行开发集消融，按预定规则冻结配置，再评测留出集。
详见 [`experiments/v0.8.10-ablation-preflight.md`](experiments/v0.8.10-ablation-preflight.md)。

2026-09-22 V0.8.11 在原 ¥100 共享上限内完成开发集四组 120 项正式运行，全部证据有效，48 成功、
72 失败、基础设施/验收无法判定/证据问题均为零。恢复复用 120 项且新增请求和供应商构造均为零。
执行版本 `a8309eb`；全量 391 passed、综合覆盖率 91.00807867931155%，Ruff/compileall 通过。
本轮保守计算费用 ¥51.07322400，共享累计 ¥80.36876520，余额 ¥19.63123480；不等于实际供应商扣费。
行动优化在普通集输入下降 35.77%，但成功率下降 8.33 个百分点、Agent 用时增加 0.50%；没有组同时
满足成功率不低于基线、输入和用时下降的预注册要求，保留基线。上下文组历史折叠 0，但 18/30 项
发生工具裁剪，不能写成整个模块未触发。下一步优先离线检查行动优化的 5 对退步及 3 对反向结果、
上下文各阶段与空补丁/失败原因；不立即改算法或启动留出评测。20 题留出集仍未使用，付费阶段需在
开发配置及预算明确后另行执行。详见
[`experiments/v0.8.11-development-ablation.md`](experiments/v0.8.11-development-ablation.md)。

2026-09-23 完成 V0.8.12 开发集离线详细诊断：120/120 原试次、120 条冻结轨迹、2,094 条响应和 480 个
试次工件哈希通过核验。普通基线/行动优化配对为 5 退步、3 反向、16 同结果；所有原分类保持不变。
Repo Map 在 519 个请求视图中出现、读取 55 个候选文件；行动优化缩短 603 条工具输出。V0.8.12 初版
把 253 个上下文工具裁剪事件误记为历史折叠；V0.8.13 按 `messages_compacted` / `batches_compacted`
更正为 417 次工具裁剪操作、253 个裁剪事件、0 次历史折叠。原始轨迹和评分未变；更正报告见
[`experiments/v0.8.13-presentation-only-preflight.md`](experiments/v0.8.13-presentation-only-preflight.md)。
现有观察不足以证明可复现实现缺陷，因此不改算法。V0.8.13 已冻结输出呈现单变量协议并通过合成零费用预检；
当前共享余额不足以覆盖 60 个位置的最坏预算，不启动半批。下一步先按启动时价格复核共享账本并保证完整阶段
预算，再决定是否执行开发集比较。留出集仍未运行。诊断报告见
[`experiments/v0.8.12-offline-detailed-ablation-diagnostic.md`](experiments/v0.8.12-offline-detailed-ablation-diagnostic.md)。

2026-09-24 已按 DeepSeek 官方当日高峰价完成 V0.8.14 工具输出呈现单变量开发集 60 项正式运行，阶段费用上限
¥55、共享累计上限 ¥150 均未触及；本阶段保守计算费用 ¥26.177326，共享累计 ¥106.54609120。此金额不是供应商账单。
8 道普通题 C/T 均 12/24；T 输入 Token 下降 10.51%，但 Agent 时间增加 18.83%。另 2 道收集失败资格题两组
均 0/6。第 11、51 项独立验收证据问题在新目录复验后仍存在，故仅 58/60 项证据有效；两项按未通过保留，不能满足
预注册的全证据门槛。输出呈现配置不采纳，保留全部关闭基线；20 道留出集未运行。恢复同目录时 60/60 试次复用。
完整结论见 [`experiments/v0.8.14-presentation-only-paid-development.md`](experiments/v0.8.14-presentation-only-paid-development.md)。

2026-09-24 Agent 测试反馈闭环代码在最终版本全量 pytest `443 passed, 0 failed`；pytest 单独覆盖率 86.64245%，命令因原有 90% 阈值退出 1。按既定口径追加同一冻结 V0.8.11 轨迹的真实离线详细诊断后，综合覆盖率 90.05246%（语句 92.36129%、分支 82.43712%），覆盖门槛命令退出 0。Ruff 与 127/127 tracked Python compileall 均退出 0。预检查 P1/环境/源码身份通过但记录时跟踪工作区有改动，不能作为正式干净运行凭证；提交后重跑 `p2-check`。账本余 ¥43.45390880，48 项最坏价 ¥41.28，阶段上限 ¥42；官方页面即时重开超时，沿用同日官方快照。全程未启动本批付费请求；成功率未知，20 题留出集未使用。详见 [`experiments/v0.8.15-validation-closure-preflight.md`](experiments/v0.8.15-validation-closure-preflight.md)。

2026-09-25 V0.8.16 对既有 48 项验证闭环轨迹完成离线诊断：48/48 试次证据有效，Agent 原记录仅 6 项已验证。pytest-10081 的三次 T 空补丁分别来自未尝试修改、曾产生空白差异后撤回、反复无实际差异的补丁尝试；均未观察到结束前 `validation_required` 提示。统一 diff 无实际变化却返回成功已独立复现，本轮仅修正补丁反馈契约。另发现 17 次通过测试调用被去选集合审计误判无效，留待下轮修复；原评分不改。完整结论见 [`experiments/v0.8.16-validation-feedback-diagnostic.md`](experiments/v0.8.16-validation-feedback-diagnostic.md)。20 题留出集仍未运行，尚无提高真实成功率的证据。

2026-09-25 V0.8.17 已在独立 fixture 复现并修正 Agent pytest 去选集合审计：完整收集与最终执行集合分开记录，以最终执行集合校验 JUnit 和阶段证据。离线重核 V0.8.15 中 17 次调用、13 个试次的反馈应由无效改为测试通过；后续失败和补丁变化仍有效，48 项独立验收评分保持 C 15/24、T 12/24。零费用模拟覆盖补丁无效果、真实变更、撤回、结束与预算停止验收。下一轮 48 项同期 C/T 日程与采纳规则已冻结为草案，按历史价格的保守上界 ¥41.28，当前共享计算余额 ¥23.12462080，不满足完整阶段预算；正式启动前还需重新核价与绑定干净执行版本。20 题留出集继续保留。详见 [`experiments/v0.8.17-selection-feedback-correction.md`](experiments/v0.8.17-selection-feedback-correction.md)。

2026-09-25 V0.8.18 在两份账单中只提取 TraceFix 费用并对重叠日期去重，历史账单基线为 ¥41.74029308；在同一 ¥150 共享账本下完成修正反馈后的 48 项同期正式比较，阶段计算 ¥18.470972，共享计算累计 ¥60.21126508。8 道普通开发题 C 为 16/24、T 为 13/24；配对为 C 单独成功 4、T 单独成功 1，只有 pytest-10051 净增，pytest-10081 净减 2。19 个 `benchmark_error` 经核实均为请求前单次输入上界拒绝，不是供应商故障；全部补丁仍经过独立验收。恢复复用 48/48，未增加 Agent 运行目录或账本请求。验证闭环不满足预注册采纳条件，保留基础 Agent C；20 道留出题未使用。下一步优先复盘 pytest-10081 的组间退步和 T 组空补丁，只在零费用回归证实具体缺陷或假设后再设计比较。详见 [`experiments/v0.8.18-validation-closure-comparison.md`](experiments/v0.8.18-validation-closure-comparison.md)。上述计算费用不是供应商账单。

2026-09-25 V0.8.19 复盘修正反馈后的 48 项比较：共享证据审计判定仅 46/48 项有效，第 19 项 Requests 网络错误、第 39 项 pytest 执行错误；重验分别超时和重复执行错误。固定分母 C 16/24、T 13/24 原记录不改；即使第 19 项最终通过，T 至多 14/24，仍低于预注册 16/24 采纳线。pytest-10081 两项 T 空补丁由相同无效果补丁反复提交直到预算耗尽形成，闭环结束提醒在该题未触发；不能把组间退步归因为闭环。全批 T 仅两项触发提醒。下一步先用合成 fixture 预检有限次数的重复无效果补丁恢复门槛，另外单列无效测试命令消耗测试额度的问题。原始评分、账本及 20 题留出集未动。详见 [`experiments/v0.8.19-validation-closure-failure-review.md`](experiments/v0.8.19-validation-closure-failure-review.md)。

2026-09-25 V0.8.20 完成有限次数重复无效果补丁恢复门槛的零费用 fixture 预检：门槛可拒绝未重读即换补丁，并允许重读目标后提交不同补丁完成独立测试。功能默认关闭；脚本化模拟不构成真实修复成功率证据。Agent 相关回归 84 passed，Ruff/改动文件 compileall 通过；全量测试与覆盖率待进行。详见 [`experiments/v0.8.20-no-effect-recovery-preflight.md`](experiments/v0.8.20-no-effect-recovery-preflight.md)。下一步先决定是否在最终代码上完成全量工程门槛，再冻结同期 C/T 验证方案；共享余额不足时不得启动不完整付费批次，20 题留出集继续保留。

2026-09-25 V0.8.21 已将重复无效果补丁恢复门槛接入正式 P2 协议和 CLI，并冻结 8 道普通开发题的 48 项 C/T 日程。模拟端到端与完成态恢复通过；Requests-1766 本地 httpbin 配置及 pytest-10051 断言错误分类已修正。执行提交 `adc8658` 上的 `p2-check` 通过。全量 pytest 461 passed，Ruff 与 129 个跟踪 Python 文件 compileall 通过。pytest-only coverage 85.658153%；合并真实离线入口后的精确值 89.634203%，Coverage.py 门槛命令显示并接受 90%，但未取整值低于 90%，已明确披露。共享 CNY 账本 SHA `f0f1be69…` 记录计算余额 ¥89.78873492；此前 ¥23.12462080 是账单更正前的历史快照。余额不是供应商账单。本轮没有开始付费比较，启动前仍需刷新价格和账本并检查本地服务，20 道留出题保持未用。详见 [`experiments/v0.8.21-no-effect-recovery-execution.md`](experiments/v0.8.21-no-effect-recovery-execution.md)。

2026-09-26 V0.8.22 已完成修正反馈后的 48 项同期正式比较（执行代码 `b435368`，协议 SHA `a98782c1…`）。C/T 均为 6/24，净增 0，未达到预注册采纳线；严格汇总有 28/48 个有效证据位置、20 个证据问题，另有一项中断基础设施事故。该轮不能证明闭环改善，保留基础 Agent。共享账本记录计算支出 ¥79.67736708、无预留或未知请求；新增供应商账单仅覆盖本批前 95 个请求，后续 661 个请求仍待账单核实。完成态恢复复用了 48/48，新增请求 0。下一步先排除 Requests-1766、Sphinx 两题报告缺失和 pytest-10081 权限证据问题，再决定是否重做同期比较；20 题留出集未运行。详见 [`experiments/v0.8.22-no-effect-recovery-48-campaign-results.md`](experiments/v0.8.22-no-effect-recovery-48-campaign-results.md)。

2026-09-26 输入边界与测试证据零费用改动：新协议移除额外 128k 默认门槛，提供 350k/2M 累计输入档；可信计数契约尚未建成，新正式请求保持请求前停机，不能宣称已可使用约 1M。离线配对 756 个旧账本请求、755 个供应商 usage，旧字节上界中位约为实际输入 3.72 倍；37 个未发送视图仍不可判断。测试额度改为预检后、进程启动时扣减，审计证据与产品补丁隔离。继续先抢救旧验收，再考虑外部基线；留出题封存。详见 [`experiments/2026-09-26-input-bound-zero-cost.md`](experiments/2026-09-26-input-bound-zero-cost.md)。

2026-09-26 冻结证据抢救：四道问题任务在明确的复验执行条件下，base/gold 各两次结果与证据一致。原 20 个证据问题的保存补丁派生复验得到 14 份两次通过、2 份稳定断言失败、4 份无产品改动；原评分和账本保持不变，不能把派生结果当成当时 Agent 的干净效果。官方 V4.1 源码与 tokenizer 已固定，但 Python 0.1.1 无 Windows 轮子或源码包，本机无 Rust 编译器，755 个有 usage 请求尚不能作官方本地计数对比；新长上下文请求保持停机。下一步是 Linux 隔离环境的离线计数资格试点，再决定外部基线，不启动付费实验。详见 [`experiments/2026-09-26-frozen-evidence-rescue.md`](experiments/2026-09-26-frozen-evidence-rescue.md)。

2026-09-26 Docker 验收试点：已安装的 Docker Desktop Linux 引擎可用。`pytest-dev__pytest-10081` 的 base/gold 与第 26 份保存产品补丁在无网络容器中各重复两次，base 均为预期断言失败，gold 与补丁均严格验收通过。原始输入只读挂载，checkout 在容器 Linux 文件系统，证据另存；CRLF 补丁应用与 Windows 挂载文件系统问题已在试点中定位并处理。这是单题派生验收，不重写历史评分，也不表示全部任务环境已迁移。下一步先将通用容器配方与依赖锁定，再用 Requests、Sphinx 各一题验证。详见 [`experiments/2026-09-26-docker-reverify-pilot.md`](experiments/2026-09-26-docker-reverify-pilot.md)。

2026-09-26 基础迁移：官方 `deepseek-recipe==0.1.1` 已在独立 Linux 镜像断网核验；755 个完整供应商 usage 与本地计数逐项相同，但任意未来请求的硬计数契约未成立，继续停止新的正式付费请求。pytest-10081、Requests-1766、Sphinx-10449 已从 Git 存档恢复，在 Docker 中分别完成两轮 base/gold/保存补丁严格验收；三题结果和证据一致。当前无可确认删除的旧目录，Docker 数据也在 E 盘。下一轮只接入 Agent 容器执行并做隔离验证，然后按需扩展；详见 [`experiments/2026-09-26-docker-foundation.md`](experiments/2026-09-26-docker-foundation.md)与[`maintenance/docker-migration-cleanup-candidates.md`](maintenance/docker-migration-cleanup-candidates.md)。

2026-09-26 Agent 容器三题零费用闭环：pytest-10081、Requests-1766、Sphinx-10449 在既有固定镜像中使用 Linux 工具桥读取源码、运行公开 smoke、应用保存产品补丁和导出产品 diff；只运行测试时 diff 为空。独立验收每题另建相同镜像 ID 的容器，严格验收各 2/2 通过，节点、测试计数、依赖指纹和旧复验一致。Agent-only 哨兵未进入验收容器，Requests 本地服务在各自容器内单独启动。全量 pytest 492 passed；改动文件范围 Ruff/compileall 与 diff 检查通过。脚本输出根改为 `TRACEFIX_RUNS_ROOT` 可配置到 `E:\TraceFixRunsActive`。全仓 Ruff 扫描因权限拒绝和旧临时夹具报错；正式 `TraceFixRunner` 的容器模式及自动中断续接仍待下一轮。未进行模型修复评测、正式实验或付费请求。详见 [`experiments/2026-09-26-docker-agent-container-e2e.md`](experiments/2026-09-26-docker-agent-container-e2e.md)。

2026-09-26 后续接入正式 `TraceFixRunner` Docker 后端，默认 Local 保持不变。三道冻结任务都经 MinimalAgent 五工具容器桥运行，测试前产品 diff 为空，测试额度按三个真实 pytest 启动计数；每题的 base/gold 资格与保存补丁在新容器各重复两次严格通过，隔离哨兵未出现于验收 `/work`/`/input`，验收容器无宿主挂载。Requests 服务日志保留 97 条容器内测试子进程请求。三题各自较大的公开测试文件仍有 1/2/2 个失败，不授予完整环境资格；TraceFix 全量 pytest 在 Windows basetemp 权限检查中 215 个测试体执行、284 个 setup 报错，40% 覆盖率不具代表性。新调用事件日志、容器阶段身份与只读检查入口已添加；自动续跑未实现，状态未知的调用不重放。定向工具回归 58 passed，新增恢复分类测试 5 passed；Git 跟踪源码 Ruff、compileall 和 diff 检查通过。三题增强版运行结果在 `E:\TraceFixRunsActive\docker-runner-final-20260926\docker-runner-e2e-summary.json`，详细原因见 [`experiments/2026-09-26-docker-formal-runner-integration.md`](experiments/2026-09-26-docker-formal-runner-integration.md)。没有供应商调用、正式实验或留出集评测；可信 Token 硬计数仍独立未满足。

2026-09-27 环境复核后，Windows 全量 pytest 能在 E 盘短路径临时根完整执行：501 passed，0 setup/断言失败；覆盖率 83.462867%，90% 门槛未通过。四个因长路径而失败的 Git workspace 用例短路径复验 4/4 通过。pytest-10081 的源代码支持 Python 3.10；新镜像目标资格 base/gold/补丁各两次通过，但整文件在两个 Agent 容器均有相同 warning 失败，完整资格未授予。Requests-1766 公开文件在两次独立运行均 90/90 通过；Sphinx-10449 在 Python 3.10 下保存补丁整文件 31/31 两次通过，原 Python 3.11 的 default-options 差异消失。证据与镜像身份见 [`experiments/2026-09-27-environment-qualification.md`](experiments/2026-09-27-environment-qualification.md)。本轮不扩大题目、不运行正式实验；下一步仅补 pytest-10081 warning 节点在正确准备条件下的 base/gold/保存补丁整文件对照，并保持 coverage 缺口公开。

2026-09-27 Goal G6 复验：新增 Docker `TraceFixRunner` 持久化顺序回归，断言终态 phase 写入时 `result.json` 已存在且状态一致；单测 1 passed。按 TEMP/TMP/basetemp/cache 均在 E:\TFP 的最终配置，全量 pytest 为 517 passed、0 failed，589.73 秒；coverage 精确 89.33506044905009%（语句 91.6837315%、分支 81.9354839%），仍低于冻结的 90% 门槛，exit 1 仅来自 coverage。Ruff、compileall 和 `git diff --check` 均通过。完整证据位于 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g6-runtime-durable-phase-final`；G6 继续 in progress，G7 依赖未满足。下一步仅在确认尚有有意义的生产行为路径可测后继续补测；不堆覆盖、不降低阈值。模型调用为零，结果不代表模型修复率提升。

G6 pytest-only 精确覆盖补测（同一最终代码版本）：用独立 `COVERAGE_FILE` 完成 517 项纯 pytest 运行，全部 passed，604.11 秒，coverage gate 导致退出码 1。纯 pytest 覆盖 84.4041450777202%（语句 87.13310580204778%、分支 75.80645161290323%）；独立证据位于 `E:\TraceFixRunsActive\goal-container-qualification-20260927\g6-pytest-only-final`，与合并同版本有效 Harness/诊断入口的 89.33506044905009% 明确分开。两种口径都未达 90%；不把脚本/诊断入口计作 pytest-only，也不宣称资格门槛完成。

2026-09-28 当前候选 `e72139d` 的干净 checkout 工程门槛：Windows x64 Python 3.11.16/3.12.5 各 632 passed、0 failed、0 skipped；纯 pytest 语句和分支合计均为 11139/12375 = 90.0121212121%，Ruff、compileall、diff 检查通过。原始证据分别位于 `E:\TFP\tracefix-engineering-py311-e72139d` 和 `E:\TFP\tracefix-engineering-py312-e72139d`。独立 editable 与 wheel 的 baseline/Skills-only 本地合成运行均为 0 供应商构造、0 请求、0 网络连接尝试；Docker prepare 控制流已由合成测试覆盖。本机没有 Docker daemon，真实隔离容器 smoke 尚待推送后的 Linux CI。此次结果仅证明工程门槛和合成控制流，不代表真实修复成功率变化；Requests TLS、pytest-10081 点版本对照、Sphinx 配方重建及完整 Token 契约留待后续。详见 [`experiments/2026-09-28-clean-checkout-engineering-gates.md`](experiments/2026-09-28-clean-checkout-engineering-gates.md)。

2026-09-28 GitHub CI run `36411826884`（分支 `b7addcb`，PR 合并 checkout `b7f4990`）显示 Windows 3.11.9/3.12.10 各 632 项测试通过但 pytest-only 覆盖率均为 89.9151515%，且浅克隆导致 `HEAD^` Diff 检查返回 128；Linux Docker 两臂通过，但原产物未验证容器清理。该运行失败记录与身份见 [`experiments/2026-09-28-clean-checkout-engineering-gates.md`](experiments/2026-09-28-clean-checkout-engineering-gates.md)。当前修订显式接收 Diff 比较基准、导出覆盖缺口、让安装 smoke 在门槛失败后仍运行，并在 Docker 运行前记录 network/mount、运行后审计容器清理。Docker backend 与 Docker 审计定向测试 53 passed，Ruff、YAML 解析与 Diff 检查通过；本机全量检查受到 pytest `basetemp` 目录权限拒绝（641 项中 381 个 fixture setup 错误，覆盖结果 44.12% 无代表性），证据位于 `D:\Tracefix\tmp\tracefix-candidate-b7addcb-py312`。因此不能代表工程验收。本次候选仍须由干净 GitHub CI 重新判定；未有结果前不宣称达到 90% 或完成 Docker 清理验收。

2026-09-28 PR 候选 `1882cc9` 的 GitHub CI run [`36420487054`](https://github.com/wingpeng30/Tracefix/actions/runs/36420487054) 已全绿：Windows Python 3.11.16/3.12.5 各 646 passed、0 skipped，pytest-only coverage `11158/12388 = 90.07103648692282%`，Ruff、compileall、Diff 通过；editable 与仓库外 wheel 的 baseline/Skills-only 合成运行通过；Linux Docker bridge 两臂通过，容器无网络、无挂载、删除已审计，供应商调用计数为零。Docker 审计哈希字段修正和三项 Docker 证据导出行为回归已并入候选。完整身份、wheel/image/artifact 哈希见 [`experiments/2026-09-28-clean-checkout-engineering-gates.md`](experiments/2026-09-28-clean-checkout-engineering-gates.md)。本批工程复现门槛达成，不等同于真实修复成功率提升；后续依序开展 Requests 历史 TLS 复验、pytest-10081 点版本诊断、Sphinx 配方重建和 Token 计数契约。

2026-10-02 当前执行顺序更新：先交付安装包内 `regression-feedback` 入口，再冻结 more-itertools #462 空输入任务并执行资格矩阵；此前 Sphinx 初筛保留为历史，本批不重建环境。所有调用均离线，原八次付费额度不续用。执行记录见 `docs/tasks/2026-10-02-package-and-public-task.md`（文档内路径相对仓库根目录）。

2026-10-02 安装包回归反馈入口已通过 PR #10 精确 CI #77 并合并，主线 `0954c4f`。more-itertools #462 单任务已在本机 3.12 通过完整资格矩阵与生产参考回放；Windows 3.11/3.12 资格以本批 PR 的锁定依赖 CI 产物为准。当前不是多类型任务集，也没有新模型效果证据。下一步增加两种不同缺陷类型，先做契约与资格，再申请新的付费验收。详见 `docs/tasks/2026-10-02-package-and-public-task.md`，原始目录不提交。
# 2026-10-02 public task expansion

The active batch adds Markdown parsing/cooperation and Click resource lifecycle
qualification, with separately identified corrective references authorized by the
user after actual upstream-reference failures. No new paid calls are authorized.
Current implementation/evidence: [three task types](tasks/2026-10-02-three-public-task-types.md).
