# TraceFix Harness Foundations：阶段实施记录

本计划依次完成：G0 基线验收、G1 有类型且可失效的知识记忆、G2 可取回的工具产物、G3 结构化任务状态、G4 统一能力策略与 ordinary Docker/Serena 组合。每阶段必须实现目标、跑完本阶段契约、完成共同工程门、保存证据并更新项目状态，才允许进入下一阶段。阶段门失败时留在当前阶段，按实现缺陷、夹具不匹配、环境缺失、证据不完整分类处理；不降低覆盖率和预算门槛。

## 决策与设计约束

- 只有可信用户明确陈述和机器可核对观察在适用时自动召回。模型解释、归纳和推断只作为候选。证据等级、发布状态和当前 checkout 适用性互相独立。
- 现有 `--memory` 使用新版记忆。产物和 TaskState 通过 `harness_foundations_enabled` 组合开关加入，默认关闭；能力策略始终执行。工具目录在会话启动时冻结。
- 首次修改 v1 记忆时，在同一仓库锁内创建 v2 信封并完成迁移与写入的一次原子替换，保留 v1。坏库、冲突、失败均 fail closed。
- 通过专用候选分支和 GitHub CI 验收；需要的 Python 3.11/3.12、ordinary Docker 与 Serena MCP 验收使用实际 CI 环境。提交并推送，不合并、不发布。
- 不调用付费供应商，不续用历史额度，不重发响应未知请求。
- 不实现后台作业、多代理、通用 MCP 插件、向量库、任意 shell hook 或新的联网模式。
- Runner 负责装配。纯领域层通过小接口依赖注入；下层不导入 Runner、MinimalAgent 或具体 Docker 后端。TaskState 不取得预算、验证资格或完成状态的权威。能力描述属于执行侧元数据，不进入供应商 ToolSpec schema。

## 阶段状态

| 阶段 | 目标 | 状态 | 进入下一阶段的证据 |
|---|---|---|---|
| G0 | 固定实现起点和双版本/容器验证链 | 完成：提交 `bcb6d24` / CI #142 全通过 | 当前源码清单、双版本全量工程门、editable/wheel、基础/ordinary Docker 与 Serena MCP 原始产物 |
| G1 | 来源可信、证据分级、适用性独立评估、原子 v2 迁移和固定召回快照 | 完成：提交 `d059ec2` / CI #144 全部通过 | Python 3.11/3.12、editable/wheel、基础 Docker、ordinary Docker 与 Serena MCP；源码清单和 Artifact 见当前状态及开发历程 |
| G2 | 截断前有界采集、不可变安全视图、可分页/搜索、配额可恢复 | 未开始 | 双流/坏字节/超时/磁盘配额/篡改/Docker 重建契约及完整工程门 |
| G3 | 需求、验收、观察、假设、计划与证据引用跨压缩/恢复保留 | 未开始 | 来源伪造、测试失效、双压缩、恢复一致性与完整工程门 |
| G4 | 所有执行受同一能力策略约束，Skills/记忆/TaskState/产物/Serena/最终回归可在 ordinary Docker 组合运行 | 未开始 | 零调用组合回放、拒绝用例、editable/wheel 与双镜像真实 CI |

## G0：基线验收

### 编码顺序

1. 记录 cwd、HEAD、分支、Python 实际导入路径、完整 `git status`；逐一给已修改文件和计划导入文档计算 SHA-256。确认身份来自当前 checkout，不用旧 CI/wheel/冻结树替代。
2. 以确认的 HEAD 创建 `codex/harness-foundations` 隔离候选工作树。仅导入既有跟踪源码、测试、脚本、CI 和必要文档；不带入凭据、`runs/` 原始产物、第三方 checkout 或实验临时目录。保存导入 patch 与清单哈希。
3. 在本文件维护阶段状态和门清单。原始工作区保留现有改动，不 reset、不清理、不切换分支。
4. 对目前已处理的 CI #139 归因项逐个执行定向测试；先验证新夹具与旧断言是否反映当前契约，再执行完整 pytest、Coverage.py 精确行/分支统计、Ruff、compileall、diff 检查、editable 与仓库外独立 wheel smoke。只修当前可复现失败。
5. CI 仅增加该候选分支触发，保留现有 job、锁文件、平台和门槛。推送 G0 后读取 Python 3.11/3.12、基础 Docker、ordinary Docker、Serena MCP 原始日志/产物及镜像身份。
6. 若本机缺 Python 3.11 或 Docker daemon，不能把跳过当通过；使用候选 CI 的实际结果。缺环境或必需 job 失败则留在 G0，修正后重新取得完整证据。

### G0 定向范围

优先覆盖：CI #139 当前夹具、memory 真实请求回放、Docker recovery prepare/runtime、输入与输出上限、技能内容检查、P2/上下文 close 兼容、项目/实验驱动 UTF-8 日志保全。历史失败名只作线索，必须以当前源码和复现为准。

### G0 退出清单

- [x] 基线来源清单与哈希完整；未导入实验原始目录和凭据。
- [x] 来源清单、导入 patch 哈希、隔离候选工作树和 CI 触发范围已核验；起始提交为 `f79a5c876f5eea22782b44b823b3c0bd64fde951`。
- [x] 基线 CI #140 三个容器 job 通过，原始产物与 digest 已记录；该结果只绑定 f79。
- [x] 对 f79 失败完成归因并修复：Windows elapsed-time 测试的时钟分辨率、skills opt-in Git fixture、local resume 演练的 backend 默认值；添加特殊类型、缓存清理和流尾哈希契约。Python 3.12.5 定向 64 passed，Ruff/diff-check 通过。
- [x] CI #141 在提交 `14046435a206cf4f38751012db37373d3fbf50f2` 上完成 Python 3.11/3.12 全量 pytest（两版各1363 passed、0 failed/error/skip）和 Coverage.py line+branch：3.11 `18964/21064 = 90.03038359285985%`，3.12 `18962/21064 = 90.02088872009115%`；原始 artifacts digest 见开发历程。
- [x] 同一提交 CI #141 Ruff、compileall、diff、editable smoke，以及基础 Docker、ordinary Docker、Serena MCP 通过；容器结果均绑定提交1404643。
- [x] CI #141 新发现公开任务资格与 wheel recovery 后的公开资格回放同因缺少 `execution_backend="local"` 失败；已补资格脚本 backend 和 RunConfig 回归测试，本机资格模块22 passed、Ruff通过。
- [x] 最新资格脚本修复提交 `bcb6d24ac5e173228749b51c57a8a7d944c7d144` 的 CI #142 通过双版本 full engineering/coverage、editable、独立 wheel/recovery、公开资格和三个 Docker/MCP job；保留 f79/CI #140 与 CI #141 失败证据及归因。
- [x] CI run `38566440638`、双版本 artifacts、三项容器 artifacts、source manifest、环境和限制已同步到当前状态与开发历程；结论只绑定 bcb6d24。

## G1：有类型、可失效的知识记忆

### G1.1 类型、可信来源和纯领域接口

1. 新增 `project_fact`、`decision`、`procedure`、`negative_result` 四类 DTO；发布状态为 `candidate/active/disabled/superseded`，证据为 `observed/user_stated/source_cited/unsubstantiated`，适用性为 `matched/stale/missing/unknown`。
2. 自动召回仅接纳 `observed`、`user_stated` 且 `matched` 的记录。`source_cited` 只证明引用存在；自由文本不会因源码引用或测试通过升级为机器事实。
3. 在宿主入口签发可信用户输入收据（消息 ID、轮次、原文哈希）。不从通用 `MessageRole.USER` 推导信任。
4. 定义来源身份接口，将逻辑仓库 common-dir、checkout、单文件内容、补丁 preimage/postimage、锁文件内容哈希、环境指纹分开保存。相同 common-dir 的 worktree 可共享仓库身份但仍按 checkout 评估；独立 clone 不按 remote URL 合并。
5. 定义无存储/模型依赖的 `EvidenceVerifier`、`ApplicabilityEvaluator`、`RecallMaterializer` 与 `SourceView` 协议。完整路径评估首版以当前整文件哈希和明确环境字段为边界。

### G1.2 v2 存储与迁移

1. 保留仓库级锁、JSON 校验、原子替换、版本链、冲突拒绝、disable/rollback；先定义 v2 信封和迁移报告 schema。
2. 只读读取 v1 时归一化显示，禁止推造历史 hash、preimage、来源收据或证据等级。
3. 首次 publish/disable/rollback 在同一锁中校验 v1 原始 hash 和 v2 权威文件。迁移内容、receipt 与操作写入同一个 v2 envelope 并一次 atomic replace；保留 v1 原文。
4. 损坏 v2 不回退到 v1 写入；冲突、坏目标、权限/磁盘错误保证原文件 hash 不变。
5. 提供 `memory migrate --dry-run`，输出预计记录数、规范化/冲突和将保留的未知字段，不写入文件。通过定向迁移和并发锁契约后再接运行路径。

### G1.3 适用性、召回和运行接入

1. 适用性查询只产生 checkout-local 的评估结果，不写全局 active。A 分支的缺文件/哈希变化不会停用 B 分支记录；用户显式 disable 仍是共享命名空间状态。
2. 将产物补丁的 `patch_sha256` 与源码树 hash 分离。修复事实关联 postimage；操作前提关联 preimage；当前源码未应用导出补丁时 postimage 事实应 stale。
3. 召回最多 3 条、正文合计 8 KiB UTF-8；固定权重为路径 8、错误签名 4、关键词 1；同分按 ID。中文用字符二元组、拉丁文用规范词项，不引入 embedding。
4. 返回每条记录的入选理由、证据、checkout 适用性和失效原因。旧五条固定流程标为 legacy，并要求回读当前实现，不升级其历史资格。
5. 生成不可变 `MemoryRecallBundle`，固定记录版本/正文/来源/策略 hash；新请求通过低信任追加消息注入且不再重复经 Skill 注入。恢复复用原 bundle；若源码变化，只在新消息追加 stale 提醒，不改写旧请求。
6. 反思保留当前累计预算、每轮最多一次和未知响应不重放；先构建有界 `EvidenceSnapshot`。negative result 首版仅从最终成功任务中已知失败的调用提取，不从失败终止任务学习。
7. 接入 local 和已有 Docker recovery 记忆路径，保持旧 checkpoint 的跨实现身份 fail closed。

### G1 退出清单

- [x] v1/v2 并发、迁移、失败原子性、disable/version/rollback 行为有测试。
- [x] 可信用户来源不可由 Repo Map、Skill、tool result 或模型伪造。
- [x] worktree-local stale 不污染兄弟 checkout；禁用不会因内容恢复而自动解除。
- [x] 未应用补丁的 postimage fact stale；precondition 和成功 postimage 语义分开。
- [x] 自动召回只包含允许等级；固定 bundle 在恢复后不随活库变化。
- [x] 反思预算拒绝零调用，unknown 结果不重试，local/Docker recovery 验证通过。
- [x] 完成双版本工程门与对应真实 Docker CI；CI #144 全部通过。用户要求在 G1 后中断，故本轮不进入 G2。

## G2：可取回的工具产物

### G2.1 存储与读取 DTO

1. 定义 `ArtifactRef`（run/call/tool/source 身份、opaque ID、raw/view hash、长度、完整性、创建时策略）；存储和读取接口不依赖 Agent、LLM、MemoryStore、DockerBackend。
2. 固定 v1 限制：raw 或 view 各 8 MiB；每 run 64 MiB、128 个产物；单页 4096 Unicode 字符；每会话 64 次读取/搜索、累计 128 Ki 字符；字面搜索最多 20 hit，不接受正则。配额存于 run 状态并跨压缩和恢复累计。
3. ID 仅在当前授权 run 可解析；模型参数不接受文件路径、其他 run ID 或任意 offset 的越界访问。

### G2.2 ProcessCapture 与视图生成

1. 将现有同步命令执行的输出采集抽为 `ProcessCapture`，保留白名单命令、环境净化、审计/JUnit、超时、取消和进程启动计数回调。
2. stdout/stderr 各自二进制 reader 持续排空；磁盘存完整 raw 到每流上限，并仅在 RAM 保留有界首尾预览。必须在 `_truncate_text` 之前保存。
3. run 总配额或产物上限耗尽后停止写盘、继续排空输出，ref 标记 incomplete；绝不为日志完整性重跑工具。超时异常设置有限 drain/reap 时间并保留已捕获部分。
4. raw 仅用于审计；从 raw 派生不可变 UTF-8 检索视图，对配置秘密和固定敏感模式做有限脱敏。raw/view 独立 hash 和策略身份，manifest 不存秘密值。视图失败不得返回可读 ref，但测试计数不回滚。
5. offset 使用 view 的 Unicode 字符位置；对中文、emoji、坏字节替换保证分页稳定。完整性校验按字节进行。

### G2.3 模型工具、上下文和 checkpoint

1. 注册 `read_tool_artifact`、`search_tool_artifact`：来源、完整性、页位置、EOF 明确返回；其结果不能获得当前源码读取资格。所有结果仍经过现有上下文计数和 LLMGateway。
2. Presenter、ContextManager 再次裁剪及摘要保留有界 locator。`search/read/diff` 可保存完整结构化 `ToolResult` 并标成展示结果存档，不声称恢复工具未返回或未捕获的字节。
3. Docker artifact 存放在 evidence 域，不挂宿主目录。JSONL 只传短 ref/receipt；用专用有界流传归档，校验成员清单、路径、类型、大小、hash，拒绝链接、重复路径、特殊文件和 traversal。
4. 批次提交严格执行：batch close → artifact publish/verify → snapshot manifest → checkpoint atomic commit。恢复先校验引用、完整性及累计配额再发模型请求；缺失内容失败关闭，不重跑原工具。
5. bridge hello 显式协商 artifact 协议与限额；任一方缺能力或 hash 不符即停止恢复。

### G2 退出清单

- [ ] 100 KiB 测试日志的中段信息可分页读取，读前工具已结束且未重跑。
- [ ] 并发双流、坏字节、超时、磁盘满、单流和总配额、部分产物行为正确。
- [ ] 跨 run ID、路径注入、hash 篡改、archive traversal、链接和配额恢复负例拒绝。
- [ ] 多次 Presenter/Context 裁剪后 locator 仍可读取；读取历史 artifact 不刷新源码证据。
- [ ] 真实 Docker 销毁旧容器、新容器还原 artifact 并读回一致；editable/wheel 兼容、共同工程门全通过后进入 G3。

## G3：结构化任务状态

### G3.1 状态模型与投影

1. 纯 DTO 记录用户需求和消息 ref、用户给出的配置/验收、带来源观察及 `valid/historical/stale`、假设、计划、问题、阻塞、Skill/Memory/Artifact refs 和 revision。
2. reducer 只消费已闭合历史事件；Renderer 输出最多 8 KiB 摘要。规范化持久状态最多 256 KiB。若关键需求无法保留则拒绝整个更新，不能静默删除或截断为“已满足”。
3. 复用已有 AgentState、测试审计、Patch/Diff 和验证门；TaskState 不重算测试资格、预算或 finish。工具结果先进入历史再投影。
4. 实际源码变化后失效关联观察和旧测试事实；无效果或失败补丁不伪造源码改变。旧日志和产物可读，但不作为当前 gate 证据。

### G3.2 用户来源、anchor 与模型更新工具

1. 初始和追加要求复用 G1 宿主签发的用户收据，完整原文仍保留 MessageHistory。模型及 Tool/RepoMap/Skill 不能写入 user-requirement 字段。仅 host CLI/API 的明确 `supersedes` 引用允许撤销指定要求。
2. 扩展原 `task_fact_anchor` 为版本化短投影；相同 digest 不重发，闭合 tool batch 后才追加，并继续保护用户原文与已激活 Skill 规则。
3. `get_task_state` 返回有界状态/ref。`update_task_state(expected_revision, operations)` 每批最多 16 项、自由文本每项最多 1000 字符；只编辑计划、假设、问题、阻塞。revision 冲突、未知字段、越界整批拒绝。
4. 模型将计划标为 done 不等于实际验证通过、完成任务或关闭预算；结果由现有测试和验证门投影。

### G3.3 恢复

1. checkpoint 只保留 canonical TaskState 一份，宿主持权威状态，Docker 不另存副本。记录 revision/hash 和被引用的历史前缀。
2. 恢复前逐项验证用户消息、tool call、artifact、skill、memory ref 属于已提交前缀；缺失、越权、损坏或哈希不符时不创建新摘要续跑。
3. 跨实现旧 checkpoint 不自动迁移；只读旧报告/产物仍可检查。

### G3 退出清单

- [ ] 模型无法伪造用户需求、source identity、观察/测试通过、预算、权限或 finish。
- [ ] 实际二次补丁使前一版源观察和测试资格失效；failed/no-effect patch 保持来源状态。
- [ ] 两次以上跨阈值压缩后需求、计划、阻塞、验收和主要 refs 可追溯。
- [ ] 用户输入追加/指定撤销、revision 冲突、超限拒绝、checkpoint 前缀错误覆盖契约。
- [ ] local 与真实 Docker 删除容器后新进程恢复一致；共同工程门通过再进入 G4。

## G4：统一能力策略与 Docker/Serena 组合

### G4.1 能力目录与唯一分发路径

1. 为每个工具附加独立不可变 `CapabilityDescriptor`：能力 ID、资源域、副作用级别、幂等、执行位置、并发约束。资源域包含 workspace、skill snapshot、memory selection、artifact、checkpoint、source snapshot。
2. `CapabilityPolicy` 用宿主可信 origin、资源域、策略 allow/deny 和 backend profile 返回结果、稳定原因码及 policy hash。未知或缺 descriptor 工具拒绝注册/调用。模型参数不含可信 origin。
3. 模型工具调用、缓存命中、prepare、执行都先过 `ToolDispatcher`；path/pytest/environment 既有专用保护仍作为执行器内层检查。
4. 用 `PreparedInvocation` 显式传递开始回调和一次性 dispatch receipt。不添加始终存在的 prepare wrapper，保持现有 prepare 语义。权限/参数拒绝时进程不启动、测试不计次；已派发后响应未知时禁止重放。
5. ordinary bridge 再验能力并检查嵌套 prepare 的最终目标；拒绝模型调用内部控制名。baseline、finish gate、restore、独立验收和源码导出分别授权，不能用调用方字符串绕过。

### G4.2 目录身份、Skills 和记忆组合

1. 分开绑定 policy、capability catalog、executor catalog、agent catalog、供应商 ToolSpec 五种 hash。MCP/TaskState 等宿主工具不混入 executor hash；旧协议字段缺失不能按 allow-all 解释。
2. 将策略和 capability catalog 纳入 root 所有的只读 executor bundle；hello、checkpoint、snapshot 和恢复校验一致。工具目录在新 session 冻结，运行中拒绝动态扩展。
3. Skills 根只接受获准 Markdown 正文及受限 references；拒绝 symlink/junction/特殊文件，限制深度、总大小和链接目标；复制成只读 snapshot，不挂 live 目录。`skill_bundle_sha256` 与 `memory_selection_sha256` 分离；名称冲突拒绝或显式区分来源。
4. 恢复复用原 snapshots 和 memory bundle。保持普通记忆/连续对话现有 recovery 依赖，不在此阶段放宽。

### G4.3 Serena MCP 的受限源码快照

1. 增加专用 `SourceSnapshotProvider`/exporter：先按 `FileAccessPolicy` 过滤相对路径，再读取正文；只收普通文件及当前新增/删除状态，上限 5000 files / 100 MiB。拒绝越界、凭据、特殊文件、链接、重复路径和限额溢出。
2. Docker 固定只读执行器以流式归档导出；宿主验证路径、成员、长度、hash 和导出期间源码是否变化，之后才物化 run-owned 临时快照。Serena broker 只依赖 provider 接口，不导入 DockerBackend。
3. 每次查询获取/验证当前 snapshot，保留现有 3 个只读工具与固定镜像身份。每查询容器独立清理，首版不加常驻优化。
4. MCP 容器只读挂载临时 snapshot；任务容器没有 Docker socket、memory store 或可写宿主目录。MCP 输入不使用包含任意恢复内部文件的通用 recovery archive。

### G4.4 Docker 最终验证和准入 resolver

1. 将 baseline/final regression orchestration 从 Runner 业务抽出到 `ValidationBackend` 接口，提供 local/Docker adapters。每个 baseline target 启动全新 ordinary 容器，绑定同源码、镜像和环境。
2. 保留原 `2 * regression_targets + 1` 最低测试调用/时间预算；保存 baseline 来源源码、patch、镜像、环境、策略和 evidence hash。
3. Final gate 使用当前任务容器实际测试结果及完整 diff identity。resume/continue 校验并复用旧 baseline，重建验证适配器，不重跑覆盖历史基线；独立验收在新容器逐目标应用并验证交付补丁。
4. Pydantic 只校验字段形状；能力 profile、镜像、SDK、snapshot 与功能依赖在模型创建前由统一 resolver 检查。缺能力给稳定错误，禁止静默 local fallback、自动安装和联网。

### G4 最终组合退出清单

- [ ] 策略拒绝、未知能力、假 origin、内部 control 名、缓存绕过和嵌套 prepare 绕过均失败关闭且不计次/不派发。
- [ ] memory、custom Skill/reference 和 regression baseline 初始化；ordinary Docker task 召回并加载；Serena 当前符号查询；中段日志落 artifact 并可 search/read。
- [ ] patch 后再次查询 Serena 可见更新快照；闭合批次后 checkpoint。
- [ ] 销毁 task 容器并重启宿主进程恢复：旧 artifact、TaskState 和来源身份完整，追加一轮用户消息并继续验证。
- [ ] 新 Docker 容器独立验收所有目标，核对累计预算、策略 hash、来源和资源清理。
- [ ] 整条零供应商调用组合分别通过 editable 和 wheel，以及真实 ordinary task image + 固定 Serena MCP image 回放。

## 所有阶段的共同工程门

每个阶段退出时均须使用该阶段候选提交的实际安装和身份运行：Windows Python 3.11/3.12 全量 pytest 无失败/错误；Coverage.py 未取整 combined line/branch 不低于 90%；原工程范围 Ruff、compileall、diff；仓库外 editable 和独立 wheel smoke；基础 Docker、ordinary Docker、Serena MCP 必需 CI；新增契约和本阶段故障回放。无解释 skip、缺少产物、环境不可用不能计为通过。失败只重跑受修复影响的项，随后重跑阶段共同门。日志/coverage/JUnit/镜像信息与 commit/hash 配对保存。

兼容门：组合开关关闭时旧 ToolSpec、消息前缀、工具 schema/结果语义保持；文件访问、输出与累计预算、未知调用保护不退化；原子失败不留假状态；源码变化使旧事实/缓存/测试资格失效；不降低范围或门槛。

## 项目进度记录要求

阶段每次退出后同步 `docs/current-status.md`、`docs/roadmap.md`、`docs/development-history.md` 和文档索引。新用户入口及已可用 CLI/API 示例完成时同步根 README 和 AGENTS。记录每阶段代码清单、commit/CI run、解释器、配置/镜像、测试原始产物、缺环境及限制。最终总结不以历史 19/30 或旧 wheel/CI 证明新能力收益；功能收益需另行设计且授权独立实验。
