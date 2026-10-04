# 2026-10-04：经验记忆、连续对话与 Docker 恢复

用户明确批准实施 G1→G2→G3→G4；此顺序取代此前仅做读取可见性调查的优先级。
基线为 `b5db1f44bd39ad6f11efa74fd3725651e48c369e`。新功能默认关闭，付费调用为零；
不复用旧调用额度，不访问留出集，不修改历史结果或账本。

## 子 Goal 与提交门槛

- [x] G1：自动形成、维护并跨进程复用仓库级经验 Skills。
- [x] G2：CLI 与 Python API 连续对话、独立轮次记录及累计预算。
- [x] G3：ordinary Docker 完整批次快照与原容器删除后的新容器恢复。
- [ ] G4：多轮→经验→新会话→Docker 中断恢复→独立验收的联动链路。

每阶段本地端到端与全量工程门槛通过后推送候选分支；精确提交 Windows 3.11/3.12、
90% 精确综合覆盖率、Ruff/compileall/Diff、editable/wheel 与 Linux Docker/MCP CI 通过后，
才创建该阶段正式 PR。候选提交不等于已交付；任何必需检查缺失时该 Goal 保持未完成。
运行目录、模型消息、密钥和历史未跟踪材料不提交。

## 已确定的边界

记忆仅覆盖经验 Skills，证据资格通过后自动启用；CLI 在轮次之间追加消息，不接受运行中插入。
经验提炼使用同一模型适配器及累计预算，每轮最多一次；未知结果不自动重发。
存储按源仓库隔离，新会话按需检索，活跃会话固定经验版本。候选、去重、失效、冲突、
停用与回滚均须有测试。初版不做用户偏好或实体关系库。
Docker 首批只覆盖普通仓库 profile；原镜像、基线、产品快照和执行状态绑定批次身份。
结果未知的模型或修改工具调用阻断恢复。

## 当前验证

### G1 正式交付证据

精确提交 `773ec7abbf012592c16fd95c01141f2629370fc1` 已完成所有门槛，
正式 PR [#17](https://github.com/wingpeng30/Tracefix/pull/17) 已创建，尚未合并。
精确 push CI [#98](https://github.com/wingpeng30/Tracefix/actions/runs/37141279902)
五项全部通过：Windows Python 3.11/3.12 各 895 tests，无失败、错误或跳过，
精确综合覆盖率 14247/15799 = 90.17659345528197%；三个 Linux Docker/MCP 门槛通过。
本地冻结 checkout 895 tests 全通过，覆盖率 14260/15799 = 90.25887714412305%，
Ruff、compileall、Diff 均退出 0。证据位于 `tmp/memory-engineering-frozen-20261004-03`。
独立 editable 与仓库外 wheel 实际命令验收通过，跨进程学习、召回、真实加载、工具执行、
独立补丁验证、停用及回滚均通过，供应商调用为零。
最终 wheel SHA256 `f90bf0d4f049d8840b277cc04a9f5c5ff52b7b43e5470c6a3298fbc176f17ed0`。
原始 CI run/jobs/artifacts 记录位于 `tmp/memory-ci-98-final-20261004.json`。
以下早期候选失败记录保留为历史，不作为交付证据。
G2 从该精确提交继续，分支 `codex/continuous-dialogue`，尚未完成验证。

### G2 实现与候选检查

G2 最终交付：精确提交 `608cfd65f443139c39a71f9737a245e4b9260ec8`，
正式 PR [#18](https://github.com/wingpeng30/Tracefix/pull/18) 已创建，基于 G1 PR #17，尚未合并。
精确 push CI [#100](https://github.com/wingpeng30/Tracefix/actions/runs/37147028347) 五项通过；
Windows 3.11/3.12 各 902 tests，无失败、错误或跳过，综合覆盖率
14531/16114 = 90.17624425964999%；三个 Linux Docker/MCP 门槛及 editable/wheel 通过。
本地冻结 checkout 902 全通过，覆盖率 14544/16114 = 90.2569194489264%，
Ruff、compileall、阶段 Diff 均退出 0，证据 `tmp/dialogue-engineering-frozen-20261004-02`。
修正候选的包装证据 `tmp/dialogue-packaging-20261004-02.json`；最终 wheel SHA256
`856a6367e480fcf3148709b3ac1113d6ec500571bf2d8fdb6056a33c6467458f`。
完整 CI 记录及各 job 日志 `tmp/dialogue-ci-100-final-20261004.json`、
`tmp/dialogue-ci-100-job-*.log`。受控时钟等待排除及真实 G1 旧记录读取的补充验证
`tmp/dialogue-clock-legacy-20261004-01.json`；没有供应商请求。
本机真实 ordinary 故障门槛也通过，记录 `tmp/dialogue-docker-failures-20261004-01`。
G3 从该精确提交建立 `codex/docker-recovery`，尚未实现或验收。
以下候选记录与失败保留为历史。

新增显式连续会话格式、chat/continue CLI 与 continue_turn API，保留单轮中断恢复入口。
共享历史、工作区、累计预算和固定经验快照，各轮独立归档补丁、轨迹、验证与结果，
新轮重置探索及结束验证；每轮经验请求使用独立幂等收据。
已完成三轮真实修改、pytest、校正要求、独立进程重新进入的定向测试。
稳定定向回归 78 passed，退出码 0，JUnit `tmp/dialogue-targeted-frozen-01.xml`；
补充报告和进程内 CLI 分支后，连续对话专项 7 passed，退出码 0，
JUnit `tmp/dialogue-final-targeted-01.xml`。Ruff 与 staged Diff 检查通过。
尚未通过 G2 冻结提交全量工程检查和精确 CI，不创建 G2 正式 PR。

早期定向失败保留：缺少模拟费用字段导致会计不完整而拒绝继续；CLI 未指定离线模型
在请求前被配置保护拒绝；Windows GBK 输出不能编码费用符号，验收进程改用 UTF-8；
独立验证缺少源码导入字段被拒绝。检查还发现编辑中的实现身份变化正确阻断跨进程继续，
以及记忆函数新增参数不兼容旧替身、旧会话局部变量未初始化，均修复后从稳定代码复验。
此前失败不记为通过，不放宽预算或身份约束。

冻结候选 `960fe6eea43f57d7543f84cf92232b0b7f4dcc5b` 全量 902 tests，898 passed、
4 failed、无跳过；覆盖率 14538/16114 = 90.21968474618345%。Ruff/compileall/Diff 均退出 0。
失败为四个新增 API 测试未显式指定离线模型，在没有本地 .env 的干净 checkout 中
触发请求前凭据拒绝；修正测试配置，不修改生产保护。
原始证据 `tmp/dialogue-engineering-frozen-20261004-01`，该候选不推送。
仓库外 wheel 与独立 editable 三进程验收已通过，零供应商调用，原始汇总及哈希
`tmp/dialogue-packaging-20261004-01.json`，wheel SHA256
`cce9e26cff802a4b6c301559a7902160c76d30e864d1d6772f40054d58a7ae2f`。
本机启动已有 Docker Desktop 后，真实 Linux ordinary 运行及另一个独立容器验证通过，
镜像 ID `sha256:49b75da8b9ba8ffb762e0995422817381db39c379973c8c777d54f2e4e724869`。
运行目录 `tmp/dialogue-docker-ordinary-20261004-01`，独立验证日志
`tmp/dialogue-docker-ordinary-independent-20261004-01.log`；本轮所属容器已清理。

2026-10-04 开始 G1 实现，功能分支 `codex/experience-memory`；尚未通过交付门槛。
本机普通 Python 位于 Anaconda，工程检查采用仓库 `.venv` 的锁定环境。
Docker 本机 daemon 的可用性以执行时探针为准；真实容器验收必须由 Linux 实测证明。
实施与离线注入模型的控制流测试不证明真实模型经验提炼质量或修复成功率。

### G1 本地候选记录

实现仓库隔离的原子经验索引、不可变版本、证据资格、固定版本 Skills 快照、最多三个确定性候选、
一次预算内提炼及持久化请求收据，另提供 list/show/disable/rollback CLI。
自动激活步骤仅允许有实际工具证据的固定操作模板；模型自由表述作为候选保留，不能凭自报信心启用。
证据失效时暂停，冲突不覆盖，候选可在后续取得有效证据时新增已验证版本。

最终定向测试：42 passed，退出码 0，运行目录 `tmp/memory-final-tests-20261004-03`。
关闭功能的既有 runtime/CLI/recovery/checkpoint 定向回归：97 passed，退出码 0。
Ruff 和工作区 Diff 检查通过。wheel 构建日志 `tmp/memory-wheel-build-20261004-05.log`；
安装包 `tmp/memory-wheel-20261004-05/tracefix_agent-0.8.4-py3-none-any.whl`。
仓库外 wheel smoke 位于 `%TEMP%/tracefix-memory-wheel-20261004-02`：供应商调用为零，
学习 PID 38068、召回 PID 4044，两次补丁均独立验收通过，停用和回滚通过。
原始配置、轨迹、结果、退出码与产物摘要由 replay 输出保存；计量为模拟值。

此前全量检查在临时目录权限问题及实现编辑期间运行，不能作为精确版本验收。
将使用冻结候选的独立 checkout 验收；全量覆盖率、Windows 双版本、editable、Linux Docker/MCP
及精确提交 CI 尚未通过，G1 保持未完成，未创建正式 PR。

候选 `50260a5e86f5295110e7970601777e0b94000a21` 的独立 checkout 检查：
892 tests，889 passed、2 failed、1 skipped；精确覆盖率 14161/15787 = 89.70038639386837%，
未过门槛。原始日志、JUnit、覆盖率及哈希位于 `tmp/memory-engineering-frozen-20261004-01`。
失败源于缺少既有 Docker TLS 锁定依赖和运行中补齐依赖造成的环境身份变化；不修改测试断言。
已按原工程锁文件的哈希安装 cryptography 48.0.0 / cffi 2.1.1 / pycparser 3.0。
补齐依赖后的 TLS 与记忆定向测试 46 passed；模拟实验失败单独重跑 1 passed。
新增在进程内运行真实记录适配器和独立验证器的闭环测试，测量子进程未计入的产品路径；
同时保留跨进程测试。增加主动停用不能被自动版本更新覆盖的维护测试，1 passed。
将固定新候选并在依赖不变的环境重新运行完整检查，不降低 90% 门槛。

候选 `2aceba7` 的仓库外 editable 与 wheel 记忆闭环均通过，已安装 wheel 的 memory list 命令通过；
产物为 `tmp/memory-wheel-20261004-06`，日志 `tmp/memory-editable-smoke-20261004-01.log`、
`tmp/memory-wheel-smoke-20261004-06.log` 和 `tmp/memory-wheel-cli-20261004-06.log`。
审查补齐索引容量保护：16 MiB 上限在替换前检查，超限保留旧索引，不写入之后无法读取的记录。
容量与原子写入损坏定向测试 2 passed，退出码 0，目录 `tmp/memory-capacity-tests-20261004-03`。

2026-10-04 G3 底层增量验证（尚未完成阶段）：真实 Linux Docker 快照回放
`tmp/replay-docker-snapshot-lowlevel.py` / `tmp/docker-snapshot-linux-01.log` 退出码 0。
镜像 sha256:49b75da8b9ba8ffb762e0995422817381db39c379973c8c777d54f2e4e724869；
原容器删除后，新容器恢复新增文件、删除状态、755 权限、Skills 计量及空测试临时目录，真实 pytest 通过。
验收 JSON 位于 `tmp/docker-snapshot-linux-01/acceptance.json`，供应商调用为零。
桥接恢复身份、禁用 Skills、失败或截断 diff 的拒绝测试与工作区快照定向测试共 33 passed，
JUnit `tmp/docker-snapshot-unit-05.xml`，退出码 0；Ruff 通过。
首次 sandbox 测试因临时目录权限失败；随后一次夹具缺少失败 ToolResult 必需 error 字段，
记录 `tmp/docker-snapshot-unit-04.xml` 为 32 passed/1 failed，修正夹具后复验通过。
这些仅证明底层快照和桥接行为；Runner 原子检查点、inspect/resume、Docker 连续对话及
跨进程故障验收仍待实现，G3/G4 保持未完成，不提交正式 PR。

2026-10-04 G3 Runner 保存路径增量：新增默认关闭 `docker_recovery_enabled`，
CLI `--docker-recovery` / TOML `docker_recovery`；仅 ordinary Docker 接受，
旧配置身份哈希排除不存在的新字段。真实 Linux Docker Runner 离线修复完成，
6 个记录适配器请求、0 供应商调用，保存 7 个完整检查点（含终态回复）。
每个检查点绑定工作区快照、桥接状态、Agent 与轨迹，只有原子 store.save 成功才推进序号。
原始日志 `tmp/docker-runner-checkpoint-02.log`；产物目录
`tmp/docker-runner-checkpoint-01/20261003T231000Z-89e90729`（含 runner-proof.json）。
首次辅助脚本导入保护器位置错误，`tmp/docker-runner-checkpoint-01.log` 保留失败记录。
配置定向测试 5 passed；既有 CLI TOML 定向测试 1 passed；Ruff 通过。
此时仅保存路径已接入：Docker inspect/resume/continue 与 G4 验收仍未完成，
不宣称完整恢复可用，不提交 G3 正式 PR；零新付费调用。

2026-10-04 G3 批次校验增量：新增 docker_recovery.validate_batch，校验批次序号、
快照路径与 sidecar、镜像身份、保存源码基线、桥接摘要、工作区成员哈希、测试证据、
桥接日志前缀和未决工具操作。拒绝检查点后已完成但未提交的修改工具；仅允许
有完整 dispatch/result 的最终 get_git_diff 读取后缀。该函数不创建模型或容器。
已对真实 Docker Runner 产物运行通过；定向测试 9 passed，Ruff 通过，
JUnit `tmp/docker-batch-validation-01.xml`。辅助探针首次 UTF-8 未指定、随后未处理
handshake 事件均失败；修正读取和协议处理后真实产物验证通过。
仍待连接 Runner.inspect 与跨进程恢复，G3/G4 不标完成，不创建正式 PR。

2026-10-04 G3 inspect 增量：Runner.inspect 按 Docker 分支调用只读检查，
核对实现/配置/RepoMap 身份、批次内容、Agent/桥接一致性、消息历史未决调用、
运行时记忆字段、轨迹前缀和镜像实际 ID；已完成任务拒绝 resume，未启用恢复明确拒绝。
单测证明检查无需原容器，仅执行 docker image inspect；10 passed，Ruff 通过，
JUnit `tmp/docker-inspect-unit-02.xml`。完整新容器 resume、工具/解释器/Skills 身份复核、
连续对话仍待完成，G3 保持未完成；当前增量不作为精确候选阶段验收。

2026-10-04 G3 身份复核增量：inspect 保留工具定义摘要校验，使用仅规格注册表，
无需宿主产品 Git 工作区、不执行产品工具；复核 Skills 内容版本、受保护目录、
有效测试证据与保存 diff 的身份。定向 10 passed，JUnit
`tmp/docker-inspect-identity-05.xml`；Ruff import 格式修正后通过。
早期注册表路径/证据布局探针失败记录保留在 identity-01/02/03；
自动审批拒绝移除工具摘要检查，未执行被拒绝修改，后续方案保留同一检查。
跨进程新容器 resume 和完整 G3/G4 阶段门槛仍待完成。

2026-10-04 G3 后端恢复身份增量：快照保存桥接 tool_sha256，新容器 prepare
在恢复 Skills/执行 Agent 前核对实际工具定义和容器解释器身份；inspect 绑定
批次工具身份与 session 身份。定向测试 10 passed，Ruff 通过。
真实 Linux Docker 第二次底层回放通过，原容器 f42f7b46... 已删除，恢复容器
b22df8fc... 恢复新增/删除/755权限/Skills 状态并运行真实 pytest；原始证据
`tmp/docker-snapshot-linux-02/acceptance.json`、`tmp/docker-snapshot-linux-02.log`。
真实容器工具与解释器身份不符注入均拒绝，且失败容器清理验证通过，
`tmp/docker-snapshot-linux-02/identity-faults.json` / `tmp/docker-identity-faults-01.log`。
供应商调用均为零。Runner 的新进程 resume 入口仍未接入，G3/G4 不标完成。

2026-10-04 G3 Runner 恢复入口增量：接入从保存基线/快照创建新容器、恢复
完整 Agent 历史与累计预算、首次请求前落盘新检查点、终态补丁与结果持久化。
Ruff 通过。真实独立进程安全中断生成成功，2 步后 Interrupted，原始
`tmp/docker-runner-resume-run-01.log`、`tmp/docker-runner-resume-01`。
后续独立进程 inspect 被工具定义摘要差异阻断，未执行恢复模型请求：
`tmp/docker-runner-resume-continue-01.log`。不能宣称 Runner 跨进程恢复通过。
下一步定位宿主/容器工具规格序列化差异，保留工具身份拒绝，不跳过检查。
G3/G4 保持未完成，未提交正式 PR，供应商调用为零。

2026-10-04 G3 首次 Runner 跨进程恢复通过：真实工具规格差异定位为 Pydantic
整数数值边界序列化 1/1.0，不是工具契约差异。新增规范工具身份摘要仅统一
等值整数 JSON 数字，所有键/实际数值/约束仍绑定；11 定向测试通过，Ruff 通过。
新记录 `tmp/docker-runner-resume-02`：第一个宿主进程在完整读取批次后安全中断
（step 2），第二个宿主进程 inspect 通过、重建新容器并恢复至 Completed（step 6）。
新容器 d727a8f4...，真实 pytest 与补丁工具执行，供应商调用为零。
原始日志 `tmp/docker-runner-resume-run-02.log` / `tmp/docker-runner-resume-continue-02.log`；
规格差异原始 JSON `tmp/docker-tool-spec-probe-01/local.json` / `remote.json`。
此前 run-01 的拒绝记录保留，不修改旧证据。该验证尚未覆盖全部故障矩阵、
独立容器最终补丁验收、连续对话与长期记忆联动；G3/G4 仍未达阶段提交门槛。

2026-10-04 G3 补丁后及测试后恢复增量：首次独立验证拒绝 run-02，原因是
恢复结果漏保存配置 test_target；修复生产元数据保存，不修改旧结果/验证器。
新 run-03 在补丁后安全中断，独立进程新容器恢复完成，并在另一独立容器
最终补丁 pytest 验收通过；原始 `tmp/docker-runner-resume-run-03.log`、
`tmp/docker-runner-resume-continue-03.log`、`tmp/docker-resume-independent-03.log`，
产物 `tmp/docker-runner-resume-03`。失败 run-02 日志保留。
run-04 另测真实 pytest 完成后中断及独立进程恢复，原始 run/continue-04 日志。
Ruff 通过；供应商调用零。全部故障与阶段全量门槛尚未完成，不提交正式 PR。

2026-10-04 G3 二次恢复验证：同一任务三个独立宿主进程依次 run(step2中断)、
resume-one(step4中断)、resume-two(step6完成)，每次恢复新建不同 Linux Docker 容器。
最终补丁在另一个独立容器通过公开 pytest。产物 `tmp/docker-runner-resume-05`；
原始 `tmp/docker-runner-resume-run-05.log`、`tmp/docker-runner-resume-one-05.log`、
`tmp/docker-runner-resume-two-05.log`、`tmp/docker-resume-independent-05.log`。
三个任务容器删除审计通过 `tmp/docker-resume-cleanup-05.json`。供应商调用零。
inspect 改为读取静态工具规格副本，保留摘要校验，避免 RunTestsTool 构造产生
宿主临时证据目录；11 定向测试通过、Ruff 通过。完整故障与安装/CI阶段门槛
仍待完成，G3/G4 保持未完成。

2026-10-04 G3 Docker 连续对话增量：仅显式 ordinary Docker 恢复允许连续对话，
会话格式 schema2；inspect continue 检查累计预算、当前轮次结果与各轮独立产物哈希；
恢复结果归档到 turns，仍累计历史/步骤/Token。真实三个独立 CLI 进程
chat --config、chat --run、continue 完成三轮修改与测试：第二轮纠正第一轮，
第三轮零值修复且整文件 pytest；累计 step18，turn3，每轮归档存在。
最终补丁在另一独立 Linux Docker 容器公开测试通过。供应商调用零。
产物 `tmp/docker-dialogue-01`；三轮 CLI 日志 `tmp/docker-dialogue-turn-01/02/03.log`，
独立验证 `tmp/docker-dialogue-independent-01.log`。Ruff 通过。
经验提炼/召回与 Docker 联动、故障矩阵及阶段全量/安装/CI仍待完成，G3/G4未完成。

2026-10-04 G3 Docker 长期记忆增量：宿主从校验快照构造产品镜像，以真实
工具/补丁/测试证据提炼；每轮一次同适配器请求计入预算、保留固定 Skills 版本。
真实三轮 Docker CLI 提炼/去重完成，turn3 step21（含3次提炼），产物
`tmp/docker-dialogue-memory-01/runs`，三轮日志 `tmp/docker-dialogue-memory-turn-01/02/03.log`。
首次新会话召回失败：经验选中但 Skills root 在 prepare 后才传递；原始失败
`tmp/docker-memory-recall-01.log` 保留。修复传递顺序后另一独立新会话召回通过：
load_skill 文本确实进入下一次模型请求，再执行真实工具/pytest，最终经验去重。
`tmp/docker-memory-recall-02.log`、`tmp/docker-dialogue-memory-01/recall-runs-02`。
供应商调用零；尚待记忆启用的中断恢复、完整故障/安装包/CI门槛及 G4 集成验收。

2026-10-04 G3 经验加载后跨进程恢复通过：新 Docker 会话从已启用经验中
实际 load_skill，下一模型请求含经验正文，补丁后安全中断；独立宿主进程
创建另一容器恢复同一 Skills 版本/已加载计量，真实 pytest 完成并宿主提炼去重。
最终补丁在另一独立容器验收通过；所有供应商调用零。
产物 `tmp/docker-dialogue-memory-01/memory-resume-runs-01`；原始日志
`tmp/docker-memory-resume-run-01.log`、`tmp/docker-memory-resume-continue-01.log`、
`tmp/docker-memory-resume-independent-01.log`，记录 PID 32188 为恢复进程。
inspect 增加未决经验请求拒绝；恢复/配置定向 16 passed，Ruff 通过，
JUnit `tmp/docker-memory-recovery-unit-01.xml`。仍需安装包可复现入口、故障矩阵、
冻结候选全量门槛和精确 CI；G3/G4 不标完成、不提交正式 PR。

2026-10-04 G3 可安装验收模块增量：新增 docker_recovery_replay 及
tracefix-reproduce --scenario docker-recovery --backend docker --image-id ...，
覆盖读取/补丁/测试后中断与二次恢复，独立宿主进程/新容器/容器删除/独立补丁
验证及原始 JSON 哈希。命令入口定向 12 passed，Ruff 通过。
首次模块回放因 session 指针与 run 收据同名覆盖失败，原始
`tmp/docker-recovery-module-01.log` 及目录保留；改名 session-location.json。
新回放 `tmp/docker-recovery-module-02` 正在运行，未宣称验收通过；
运行期间不再编辑实现文件。G3/G4 尚未达到全量安装包与精确 CI 门槛。

2026-10-04 G3 可安装回放矩阵通过：`tmp/docker-recovery-module-02/recovery-summary.json`
accepted=true/provider_calls=0，读取/补丁/测试后三种中断各2个独立宿主进程，
二次恢复3个进程，每次新建不同容器且删除审计通过，四个最终补丁独立容器验收通过。
原始 stdout/stderr、收据、轨迹/配置/镜像/实现摘要/产物哈希由模块保存；计量为模拟。
Linux ordinary CI 增加仓库外已安装 wheel 的 docker-recovery 场景及证据上传；
YAML解析通过。相关回归 `tmp/docker-recovery-regression-01.log`/JUnit 仍运行，
未作为阶段资格；完整故障矩阵、Windows全量覆盖、editable/wheel精确版本与CI仍待完成。

2026-10-04 G3 故障与回归增量：相关检查点/连续对话/桥接/快照/Docker回归
143 passed，196.05秒，退出码0，JUnit `tmp/docker-recovery-regression-01.xml`。
真实 Linux Docker 快照故障：越界符号链接、超过1GiB稀疏文件、发布写入失败
全部拒绝，先前快照字节保留、partial清理、容器删除通过；原始
`tmp/docker-snapshot-faults-01/faults.json` / `tmp/docker-snapshot-faults-01.log`。
完整且有已知结果的内部只读恢复状态查询允许作为保存失败后的日志后缀，
未决查询及修改仍拒绝，14定向测试通过。原始真实上一批次再次validate_batch通过。
尚需故障回放产品化、完整覆盖门槛、安装包/精确CI与G4，不提交正式PR。

2026-10-04 G3 首次冻结候选 f29356f6f98e62e6c19228232a312845e43d1390：
仅候选提交，未创建正式 PR。干净 checkout `tmp/docker-recovery-frozen-01`，
工程检查输出 `tmp/docker-recovery-engineering-01`，当前统一执行会话 3577 正在运行。
工作区 Ruff 扫到历史 tests/.tmp-workspace 内生成的 conftest，不能代表干净候选检查；
保留这些历史文件，以冻结 checkout Ruff/全量 pytest/精确覆盖率结果为准。
完整故障与安装包/Windows双版本/精确CI仍未通过，G3/G4保持未完成。

2026-10-04 G3 故障回放产品化：新增 docker_recovery_faults，接入可安装
Docker recovery 场景和 Linux CI 断言。真实 Linux Docker 运行通过，故障含
越界链接、1GiB限制、发布写入失败/上一批次保留，以及实际补丁已执行但宿主
丢失结果的桥接断连；日志记录 outcome_unknown，validate_batch 拒绝自动恢复。
容器删除通过；供应商调用零。原始 `tmp/docker-packaged-faults-01/faults.json`
与 `tmp/docker-packaged-faults-01.log`。Ruff通过。
冻结 f29356f 的工程检查会话3577仍运行，不修改其独立 checkout；本次新模块
属于后续修订，必须重新冻结最终候选并验收，不能混用旧版本覆盖证据。

2026-10-04 G3 故障补齐：产品化真实 Linux Docker 回放新增清理失败注入，
确认 backend 明确报告 ordinary Docker cleanup was incomplete，解除注入后
重试清理、本次容器删除通过。原始 `tmp/docker-packaged-faults-02/faults.json`
及 `tmp/docker-packaged-faults-02.log`，供应商调用零。
新增损坏批次/路径/镜像/桥接/日志/sidecar/测试证据拒绝回归，23 passed，
JUnit `tmp/docker-corruption-unit-01.xml`，Ruff通过。
冻结f29356f全量工程检查仍在会话3577运行；本轮新故障代码和测试不混入
该冻结版本的资格结果，后续最终候选需重新验收。G3/G4未完成。

2026-10-04 G3 恢复失败诊断增量：Runner 在准备/恢复或容器清理失败时写入
独立 recovery-failure 收据（序号/阶段/已脱敏错误/容器身份），保留历史结果、
补丁与有效检查点；不盲目生成未知操作后的检查点。两项故障回归通过，
断言供应商构造零、清理尝试、历史字节一致和两类诊断落盘。
`tmp/docker-recovery-diagnostics-01.xml`，Ruff格式修正后通过。
冻结f29356f全量检查仍运行；该新修订需后续候选验收，G3/G4未完成。

2026-10-04 G3 遗留容器清理实现：新容器完成状态恢复后，按保存的完整原容器ID
查询是否仍存活，仅所有权标签等于本run ID才删除；不存在正常继续，身份
不符/查询失败拒绝，发生在模型构造前。此增量尚待真实Linux与定向验收，
不宣称通过。冻结f29356f工程会话3577仍运行，不修改其checkout。

2026-10-04 首次 G3 冻结候选 f29356f 全量结论：Windows Python3.12.5，
951 passed、0 failed，846.25秒；pytest仅因覆盖门槛退出1，精确综合覆盖约88.01%，
低于90%，阶段验收失败。原始 `tmp/docker-recovery-engineering-01`，
干净候选 tracked_working_tree_dirty=false。不降低门槛、不提交正式PR。
下一步按 coverage-gaps.json 补充有意义的恢复/故障路径，最终修订重新冻结验收。
遗留原容器真实Linux复验通过：保存原容器存活，重建恢复后按完整ID与本run标签
删除，随后真实pytest/新增删除/755权限检查通过；原始
`tmp/docker-owned-container-linux-03.log` / `tmp/docker-snapshot-linux-03/acceptance.json`。
定向26passed，`tmp/docker-owned-container-02.xml`；首次测试插入位置错误失败保留。

2026-10-04 G3 覆盖缺口定位：f29356f精确综合15148/17211 = 88.01347975132182%，
缺口集中新恢复编排及Windows pytest未执行的容器路径，不降低90%门槛。
新增宿主执行适配器补充测试，实际Git工作区/补丁/pytest/快照/CheckpointStore/
Runner.inspect与resume/经验提炼均运行，断言完整历史、累计Token、两次工作区身份
及记忆关闭/启用行为；真实Linux Docker矩阵独立保留为必需验收。
4 passed（含先前恢复失败诊断），25.08秒，`tmp/docker-runtime-tools-02.xml`，Ruff通过。
首次适配器忘记重建注册测试临时目录，2失败/2通过，原始tools-01 JUnit保留，
修正适配器后复验通过。尚需其他预算/故障覆盖与最终候选完整验收，G3/G4未完成。

2026-10-04 G3 预算与未决请求拒绝覆盖：新增每项累计 step/test/input/output/time
预算耗尽的 continue 拒绝，保留轮次归档；未知记忆请求、不完整 runtime 状态、
测试源码身份不符、轨迹前缀和未提交模型请求拒绝。全部37定向测试通过，
JUnit `tmp/docker-budget-inspect-01.xml`，Ruff未用import/格式修正后通过。
这些补充Windows检查器覆盖，不替代真实Linux Docker验收；最终精确综合覆盖
仍待重新冻结和全量测量，G3/G4保持未完成。

2026-10-04 G3 流式恢复故障覆盖：新增 envelope/1GiB限制、越界/重复路径、
缺失成员、权限/大小/内容哈希/类型不符拒绝，断言无工作区外写入；流式目标为
可丢弃新容器，损坏部分恢复不进入Agent。30定向快照测试通过，9.47秒，
`tmp/docker-stream-fault-01.xml`，Ruff通过。最终综合覆盖仍待冻结复测，不降低门槛。

2026-10-04 G3 连续轮次故障覆盖：新增非法attempt路径、缺失归档项、补丁
归档被修改、轮次身份不符及镜像查询不可用/缺失/身份变化的拒绝测试。
44检查器定向测试通过，13.23秒，`tmp/docker-round-identity-01.xml`，Ruff通过。
最终覆盖门槛仍未复测，G3/G4未完成，不提交正式PR。

2026-10-04 G3 宿主恢复及传输补充验证：真实文件/pytest/归档的宿主后端补充
恢复、经验提炼、准备失败和清理失败诊断覆盖。新增传输测试最初6项因夹具
缺少prepare初始化的python字段失败，原始JUnit保留；修正夹具后6项全部通过
（tmp/docker-snapshot-transport-02.xml）。联合84项全部通过，52.33秒
（tmp/docker-focused-coverage-01.xml）；该定向子集的全包覆盖31.36%触发原90%
保护并返回1，不作为全量验收，也不合并不同实现版本覆盖结果。
修复恢复结果workspace仍指向旧容器的问题，断言新旧容器身份不同；4项回归
通过，25.29秒（tmp/docker-runtime-tools-03.xml）。新增文件Ruff通过。
下一步冻结新候选，全量重新测量覆盖，再进行安装包和真实Linux验收；G3/G4
保持未完成，没有新增正式PR，供应商调用为零。

2026-10-04 G3 新冻结候选为9fd5f28（尚未推送）。从git归档构建wheel，
SHA256 0dcb98672b8293b276c886d37cda264af1407e7d8b152ac6a827e662fb99eee6。
新增恢复/故障/流式归档模块及Docker资源全部包含，resource-proof.json保存。
独立wheel安装环境在仓库外执行完整真实Linux Docker验收通过，退出0：
读取、补丁、测试后安全中断各2进程，二次恢复3进程，全部原容器已删除，
各最终补丁在另一独立容器验证通过。越界链接、1GiB上限、写入失败保留
旧批次、修改结果未知拒绝自动恢复、清理失败显式报告及最终清理均通过。
完整证据tmp/docker-recovery-installed-03/recovery-summary.json，绑定实现哈希
2667cce70952d7c7a0f2701d0331dc37e6b80fa1aaec7808249e497ca3d5b455，
镜像sha256:49b75da8b9ba8ffb762e0995422817381db39c379973c8c777d54f2e4e724869，
供应商调用0，离线模型计量为合成数据，不作为真实提炼/决策质量证据。
另增加验收器拒绝进程/容器复用、未清理容器、工作进程失败、独立验证失败
的6项契约测试，通过2.97秒（tmp/docker-replay-contract-01.xml），仅为补充。
冻结全量验收仍在运行，G3/G4未完成，未新增正式PR。

同一9fd5f28源码归档的editable独立环境也通过真实Linux恢复：两个独立
宿主进程、新容器恢复，另一个容器验证补丁通过，退出0。证据
 tmp/docker-recovery-editable-01/summary.json 及independent.json，供应商调用0。
安装环境Python3.12.7/pydantic2.8.2；冻结全量门槛使用锁定Python3.12.5环境，
最终Windows3.11/3.12精确CI仍待执行，不把安装检查代替版本矩阵。

验收器补强：父进程显式检查工作进程终态、provider_calls==0，以及故障
验收passed和零供应商调用；不只信任退出码。9契约检查通过3.27秒，另4故障
反例（不安全快照被接受、断连被隐藏、清理失败被隐藏）通过0.50秒，Ruff通过。
原始证据tmp/docker-replay-contract-02.xml、tmp/docker-fault-contract-01.xml。
这些是验收器补充契约测试，不替代上述真实Linux容器执行。实现变更尚未
纳入9fd5f28冻结候选，需要下一精确候选复验，不复用旧实现哈希作为新验收。

2026-10-04 G3 冻结2511451全量1013项无失败/错误/跳过，pytest退出0，但精确
综合覆盖15553/17353=89.62715380625828%，工程检查仍拒绝；Ruff/编译/Diff通过。
证据tmp/docker-recovery-engineering-03，保留原始结果，不采用四舍五入覆盖率。
该候选wheel真实Linux四类恢复+故障均通过（tmp/docker-recovery-installed-04），
实现哈希355844f9acfd08f5e2908de415c9c5c8ae6c9caa59477ca56b5edc920607654c。
新增产品/权限/Skills与工具/解释器身份故障脚本并纳入Linux CI。首次脚本
因_git助手不返回提交值失败（products-04），修正后发现无.gitignore仓库的
生成缓存进入Git差异、但快照排除缓存，恢复差异不一致（products-05）。两次
失败日志保留，原容器均清理，不将失败报告改为成功。
修复限定在恢复启用时：仅在容器私有.git/info/exclude忽略未跟踪缓存，不改
目标仓库；快照保留已跟踪缓存目录下的产品文件，生成缓存继续排除。37项
快照/路径边界检查通过10.96秒（docker-cache-products-01.xml），39项运行时/
传输/普通Docker回归通过39.62秒（docker-cache-runtime-01.xml），6项准备阶段
身份/默认关闭检查通过1.07秒（docker-prepare-identity-01.xml）；16项工作进程
安全恢复及保存后中断契约通过4.41秒（docker-replay-worker-01.xml）。
Ruff修正换行后复验，下一步冻结缓存修复候选并重跑完整本地与Linux验收。
G3/G4未完成，无正式G3 PR，无供应商调用，不降低精确90%门槛。

2026-10-04 缓存修复候选4effdda已冻结，完整工程检查运行中。wheel SHA256
26b418eab8a6da11dd1a692a7c7e61020201fa76ba3b7b9ed620e80769f044af。
真实产品恢复products-06已通过恢复后pytest，但脚本重复清理已删除旧容器
而报清理错误，原退出1保留。只修正脚本对已确认删除的容器不再重复删除，
products-07全部通过，退出0：文件新增/删除、755权限、已跟踪缓存目录产品、
重建空测试临时目录、Skills恢复、工具/解释器身份拒绝、四容器最终清理。
证据tmp/docker-recovery-products-07/acceptance.json，实现哈希
3ea1e661d4b5b23be30b489abde23ffb8489f4378846b310b75bdf7e614d9d62，
镜像保持49b75da8...，供应商调用0。脚本清理修正尚未纳入4effdda，下一
候选须包含此修正并执行精确CI；完整恢复轮次正在installed-05重新验收。

2026-10-04 4effdda的wheel完整四类恢复验收installed-05与editable-02均退出0，
通过且实现哈希同为3ea1e661...，供应商调用0。全量engineering-04会话85013
随后丢失，系统查询无原检查/python子进程，无summary或最终pytest产物；不能
视为完成或通过。冻结脚本清理修正后重新运行完整检查，原空/未完成目录保留。

2026-10-04 G3 根目录边界复核：真实 Windows junction 隔离复现表明导出与恢复
会跟随被替换的工作区根目录，2项反例失败（tmp/docker-root-boundary-red-01.xml）。
已在根目录解析和清理之前拒绝符号链接及reparse/junction；使用lstat属性兼容
Windows Python3.11。39项根目录及快照回归通过7.87秒，外部哨兵文件保持不变
（tmp/docker-root-boundary-green-01.xml），Ruff通过。43cd63e冻结检查仍继续，
该修复需另冻结并重新验收，旧检查结果不作为新实现通过依据，零供应商调用。

候选43cd63e完整验收通过：1033项、0失败/错误/跳过，精确15649/17372=
90.08174073221275%，Ruff/编译/Diff全通过，tmp/docker-recovery-engineering-05。
根目录修复另提交f123eff，并将真实Linux符号链接根目录的导出/恢复拒绝检查
纳入产品恢复验收脚本；外部哨兵不变，不以旧候选门槛代替新候选验收。

2026-10-04 G3 新实现本地完整门槛通过：冻结d50f6e9，1035项0失败/错误/跳过，
精确15660/17383=90.08801702813093%，Ruff/编译/Diff通过，engineering-07。
wheel SHA0e1837d9d9da81f6bd270745a94f663ff7110dd635aa0c9c2c265e23062815c6，
实现SHA a27b8dc65ddeac727861eb879dd9e51cfcc0147a49c92e71050bbe5d890b1e6a。
已安装wheel的完整四类恢复installed-06与editable-03均退出0，独立宿主进程、
新容器恢复及独立容器补丁验证通过，供应商调用0。products-08因Docker服务
未启动失败；恢复已安装的用户级Docker Desktop后products-09发现探针缺少
PYTHONPATH，修正为容器内/opt/tracefix/src后products-10全部通过（退出0）。
符号链接根目录导出/恢复均拒绝、哨兵不变，产品新增/删除/755权限/跟踪缓存/
Skills/身份拒绝及四容器清理通过；所有失败原始记录保留。最终候选只补验收
脚本模块路径及本文证据记录，不改变上述已验证的src实现或pytest测试。
下一步推送候选执行精确Windows双版本、安装包及Linux Docker/MCP CI；此前
不建立正式G3 PR。G4仍未完成，真实模型提炼与决策质量未测。

2026-10-04 候选09297c4的CI #102三个Linux Docker/MCP job通过；下载的ordinary
证据ZIP SHA256为697b0d321fd9ddddc42afffae53d876c25cac6683f37bd00224529805adf2f87。
四类恢复、快照故障、产品/权限/Skills/身份/根路径拒绝及清理通过，供应商调用0。
Windows双版本job失败；3.11原始pytest为1016 passed、19 setup errors，精确
15595/17383=89.71408847724788%，不能作为工程通过。原始日志与产物保留在
tmp/g3-ci102-py311、tmp/g3-ci102-job-*.log，G3不创建正式PR，G4仍未完成。
原因是新增inspect夹具在产物根目录而非已提交source仓库创建工具注册表；
本机目录位于父Git仓库内掩盖错误，干净CI正确拒绝。改为config.repo，不改
生产Git保护；44项定向回归通过（tmp/g3-ci-fix-targeted-01.xml），后续必须
在干净冻结checkout重新执行完整工程门槛，再推送精确候选CI。

候选7658a82本地1035项全通过，15660/17383=90.08801702813093%，原始工程
证据tmp/docker-recovery-engineering-09。精确CI #103三个Linux门槛再次通过；
Windows3.11/3.12均为1034 passed、1 failed、0 errors/skipped，覆盖率分别
15649/17383=90.02473681182765%、15647/17383=90.01323131795432%。
唯一失败是inspect夹具仍手写未规范化工具哈希，CI锁定Pydantic2.10.1的1.0
与本机2.13的1导致身份不匹配；产品已经使用保留完整契约的数字规范化。
原冻结候选在独立Pydantic2.10.1下复现1失败（g3-tool-identity-red-01.xml），
夹具改用正式tool_identity后44项全通过（g3-tool-identity-green-01.xml）。
不移除工具身份校验，不改变生产代码；失败CI和原始结果保留。
新修正仍需冻结全量工程与精确CI，G3/G4未完成，不创建正式G3 PR。

### G3 正式交付及 G4 接入

G3 精确提交 `a7a95bbbc1d90fd6433ae58f7e3438416ccaa245` 已通过
[CI #104](https://github.com/wingpeng30/Tracefix/actions/runs/37210269401) 五项门槛，
正式 [PR #19](https://github.com/wingpeng30/Tracefix/pull/19) 已创建，基于 #18，未合并。
Windows3.11/3.12各1035项，无失败/错误/跳过，精确覆盖率分别
15649/17383=90.02473681182765%、15647/17383=90.01323131795432%。
Ruff/编译/Diff、editable和仓库外wheel通过；三个Linux Docker/MCP门槛通过。
原始结果位于tmp/g3-ci104-py311、tmp/g3-ci104-py312、tmp/g3-ci104-ordinary，
完整run/jobs记录tmp/g3-ci104-final.json，供应商调用0。
本地统一Pydantic2.10.1环境1035项全通过，15660/17383=90.08801702813093%，
证据tmp/docker-recovery-engineering-11。此前engineering-10混用父进程2.10.1/
子进程2.13而被工具身份保护拒绝，其1失败原始结果保留；独立一致环境45项
定向回归及父子版本证据见g3-locked-consistent-targeted-01.xml和
g3-locked-consistent-provenance.json。没有降低门槛或改动生产身份保护。

G4从该已验证提交建立codex/memory-dialogue-integration。新增安装包联动入口
memory-dialogue-integration，串联三轮实际CLI/提炼、新Docker召回/修改、安全
中断与旧容器删除、新进程恢复及第三容器全文件验证；Linux CI分别执行wheel
与editable场景。接入时尚未实测，以下记录后续验证；全量/精确CI完成前不创建正式PR。

### G4 本地联动及故障验证

提交 f0f202e 的首次真实 Linux Docker 联动通过，原始证据 tmp/g4-linux-integrated-01。
提交 06f20b0 补充经验版本实际加载、提炼计量与供应商零调用检查，保存原始命令输出、
补丁、轨迹、测试和快照文件哈希及 Python/平台身份。42 项故障注入全部通过；
控制器测试仅补充实际 Docker 证据，不替代它。该提交真实联动再次通过，证据
tmp/g4-linux-integrated-02：五个独立 PID，暂停 step=4，恢复 step=8，累计
800 input/80 output tokens、2 次测试、费用0，独立容器完整3项通过且三个容器均已删除。
使用固定镜像 sha256:49b75da8b9ba8ffb762e0995422817381db39c379973c8c777d54f2e4e724869。
实际供应商调用0，资源计量来自预录模型。真实模型提炼与决策质量未测。

冻结 checkout tmp/g4-frozen-01。首次 wheel 构建缺少 setuptools 而失败，原始记录
tmp/g4-wheel-build-01.log 保留；改用已有构建环境生成 wheel，SHA256
b686b64ed0ec0e1307b9379838685ff307045551ca81fd2839e34ae0ac81cb0b。
仓库外 wheel 和 editable 的真实 Linux Docker 联动均通过，分别保存于
tmp/g4-wheel-integration-01、tmp/g4-editable-integration-01。每次独立容器3项全部通过，
供应商调用0；各143个原始产物哈希全部复核，安装包新增模块、命令和 Skills/Docker
资源已核对，综合证据 tmp/g4-installed-audit-01.json。当前 checkout 实现哈希
54de8c8e45dd36fbf2342adf784b14fb760b4bf4eec00e1f7946707b344972ff，冻结 checkout/
wheel/editable 哈希56a35e419d796869f4a909fa5901fecb32f9cdd0b597a90fbf99b981454f4a12；
各运行内部固定身份，Git checkout 换行转换造成文件字节差异。
冻结提交 06f20b031a93e7e363e908cdd2a8ae2a5e94d953 的本地全量工程门槛通过：
Python3.12.5/Pydantic2.10.1，1077项测试、0失败/错误/跳过，精确覆盖率
15808/17541=90.12028960720598%；Ruff、compileall、Diff均退出0，工作树干净。
原始工程结果 tmp/g4-engineering-frozen-01，汇总哈希见其 summary.json。
首次在未设置PYTHONPATH的父工作区运行花费1363秒，两个既有子进程导入测试失败，
覆盖率89.89225243714725%；修正为冻结checkout且令子进程继承 `src` 导入路径后，
两项定向测试和全量测试均通过，没有改代码或降低门槛。
全量复验耗时689秒。首次精确候选 CI #106（提交a8d6c4c55d6a5d7f464d60363ce524ee0a8fa790）
五项中四项通过：Linux ordinary Docker、冻结 Docker、MCP及Windows3.12通过；
Windows3.11覆盖率为15795/17541=90.04617752693689%，但有1项测试失败，故未创建PR。
run ID 37215201883；3.11 artifact ID 11308143393，SHA256
b0ebe2f208c544ff63b066a2fdaa4e9aef6d287afb31b49d8567e76185b6f200。

使用与CI锁文件相同的Python3.11依赖在本机复现了新增验收测试失败：本机特殊Windows
CPython构建的`platform.platform()`在系统`shell ver`探测上抛TypeError。改用稳定的
`sys.platform`记录执行平台，并断言收据中的值；修复提交7e221f5c244ee242a26950602ecede9af0d68c89。
Python3.11/3.12的42项定向验收均通过。修复后wheel SHA256
cb837b2f4a3f4ca98793943fe8bfc7ea78cc7522b22b713b5bbc2137ff50f824；wheel/editable真实
Linux Docker联动分别重跑通过，独立容器3项通过，provider调用0；各143个产物哈希复核，
详见`tmp/g4-installed-audit-02.json`。最终修复候选精确CI待运行，G4正式PR尚未创建。

精确 CI #107（提交df51f4d07bcb7c69857bddcc5f9ca401f641b9e2，run ID 37218406037）
四项通过，Windows 3.11 editable smoke 在连续对话三轮验收处失败，未创建PR；工程全量检查、
wheel场景和wheel构建通过。失败日志只有聚合断言，未输出其子条件。检查本地运行证据后，
将不同轮次必须拥有互异OS PID判定为不稳健：各轮确由独立`subprocess.run`启动，Windows允许
回收PID。验收现改为检查每个子进程写入的轮次、成功退出、有效PID及零供应商调用，并加入
PID复用回归测试。当前本机三轮真实CLI均退出0、各7个预录请求、0供应商调用，且已通过轮次
状态断言；随后独立测试验证在本机受限目录创建临时回归目录时被Windows ACL拒绝，不能将该
本机隔离错误计为功能通过。该修复的精确CI尚未运行，G4 PR仍未创建。
