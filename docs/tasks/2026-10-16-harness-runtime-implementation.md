# TraceFix G5—G8 分阶段实施计划

用户已批准本计划实施。基线：G4源码2dfd759、文档bb43e31；实施分支
`codex/harness-runtime`，工作树`D:/Tracefix/runs/harness-foundations-worktree`。
原工作区源码不混入；不合并main、不发布、不新增付费实验，不重发未知请求。

## 阶段状态

| 阶段 | 目标 | 当前状态 | 进入下一阶段的必要条件 |
|---|---|---|---|
| 起点核验 | 核对并修复实际G4保存提交失败 | 已关闭：ac64510 / CI159及本地双版本通过 | 修复定向契约及同源码完整工程/CI通过 |
| G5 | 可取消、可查询的后台pytest | 已关闭：c02c79d / CI169及同提交Linux专用；双版本各2017通过/2平台skip，覆盖率90.1143%/90.1045% | 整树取消、增量日志、预算与恢复及工程门已通过 |
| G6 | 分层仓库规则 | 下一阶段：从G6.1纯模型和安全读取开始 | 规则先送达、缓存/批次/压缩/恢复契约及工程门通过 |
| G7 | Skills契约和多MCP生命周期 | 未开始 | Serena+官方Fetch真实组合及工程门通过 |
| G8只读 | 共享预算的探索/独立审查 | 未开始 | 根预算、权限、报告、取消/恢复及工程门通过 |
| G8写任务 | 独立worktree交付补丁 | 未开始 | Windows/ordinary隔离写、冲突拒绝及根验收通过 |
| 最终组合 | 跨功能和安装方式回放 | 未开始 | 完整组合、证据和使用文档齐全 |

每个子步骤先通过定向契约再继续；每阶段归档证据并更新当前状态、路线图、开发记录后才能进入下一阶段。
状态区分实现完成、本地通过、CI通过、阶段关闭，环境不可用不算通过。

## 已确定的范围与边界

- 后台作业随会话停止；正常退出先取消保存，崩溃未知不接管、不重跑；不做常驻作业服务。
- Skills首版仅加载前检查，保持现有load_skill，不增加技能执行模式。
- MCP支持多个受控服务，允许联网准备必要依赖/镜像；任务运行不自动安装/升级，任务容器继续network none。
- 子代理先只读，再隔离写；新功能默认关闭，关闭时保持旧schema、消息顺序和行为。
- Runner只装配；领域模型不导入Runner/DockerBackend；ContextManager仅消费消息和引用，不读取子系统存储。
- 作业、规则、扩展、预算和子代理使用明确调用和小接口，不引入通用事件总线或任意shell hooks。

## 起点核验与实施身份

1. 核对cwd、HEAD、导入路径、工作区差异及G4保存提交实际CI，不借原本地证据宣称新提交通过。
2. 实际失败先复现修复，不能跳过后开始G5；建立实施分支、源码清单和阶段记录。
3. CI157（38768213294、源码2dfd759）实际结果：双Python工程检查步骤通过；公开资格及wheel资格失败；
   ordinary组合失败；基础Docker和Serena通过。日志保存在忽略目录`runs/harness-runtime-start/ci157-job-*.log`。
4. 已定位两处：scripts/qualify_public_task.py调用regression._test漏dispatcher；ordinary步骤cd RUNNER_TEMP后用相对docker路径。
   仅针对实际失败修复，不降低测试范围或权限保护。
5. 定向复现2项失败已归档；修复后Python3.11/3.12相关35项均通过，Ruff/compileall/diff通过。
   Docker Linux29.8.2宿主侧可用；沙箱内管道权限被拒，不记成daemon故障。完整起点门待新提交CI。

## G5 可取消的后台作业

目标：长测试可查询、增量读取和取消；取消/崩溃不漏计、不重跑、不产生假通过。

1. 纯模型和状态契约：新增start_test_job(command,timeout_seconds)、get_job_status(job_id)、
   read_job_output(job_id,cursor,limit)、cancel_job(job_id)。仅现有白名单pytest，每checkout一个活动测试，不排队。
   状态区分not_started/starting/running/cancelling/completed/failed_to_start/cancelled/deadline_exceeded/outcome_unknown，
   另存确认启动与确认整树退出，工具只接受当前run的opaque ID，不接受PID或路径。
2. 分离RunTestsTool准备、不可变PreparedTest、执行和TestRunOutcome判定；同步与后台共享白名单、最小环境、审计/JUnit和源码身份。
   启动回调为单次参数，不修改共享工具字段；先证明同步schema、拒绝与计次兼容。
3. ProcessTreeScope.start/terminate_tree/wait_empty/close：Windows Job Object先归属再运行、禁breakaway、kill-on-close；
   Linux local要求委派cgroup v2，缺能力拒绝、不提权；ordinary容器为最后清理边界，无法证明清空时停容器中止本轮。
   实测父/子/孙、父提前退出、脱离进程组、取消失败，取消请求不等于取消成功。
4. 持久双流采集：raw审计与脱敏view分离，已提交字符cursor；跨块UTF-8/秘密不提前泄漏，超长不安全片段隐藏。
   共享G2存储/读取额度，临时转最终产物不重复扣费；磁盘/额度满继续drain，标incomplete，终态才发布不可变ArtifactRef。
5. 同一测试预算：启动前保留、确认启动按execution ID计一次，明确未启动才释放；未知保留并禁止重放。
   deadline取请求、宿主及会话剩余最小值；授权/参数检查早于准备、保留、spawn。注入各启动边界崩溃。
6. ordinary bridge start派发worker后即返回；主循环处理查询/输出/取消；worker不写协议stdout；hello协商job_protocol。
   主线程在闭合批次消费终态，通过同一判定更新事实。活动期可读/维护计划，拒绝补丁、第二次测试和最终gate。
7. 活动作业批次仅写不可恢复进度收据，不替换安全checkpoint、不每批取消；正常pause/exit先停止接收、取消、
   确认清空、有界drain、封存、消费终态，再提交安全checkpoint。崩溃unknown不从旧checkpoint重放start。
8. TaskState仅引用作业/证据；裁剪保留ID/cursor/状态/ArtifactRef，源码改变使旧测试事实失效，日志仍可读。

退出：零供应商长测试中段查询→取消并清孙进程→再测完成→安全暂停恢复不重跑；硬崩溃unknown不重放/不退款；共同工程门通过。

## G6 分层仓库规则

目标：相关读写前规则已实际送达模型；规则有来源/范围/版本，不能扩权。

1. 根到文件父目录逐层AGENTS.md；.tracefix/rules.toml指定固定规则目录Markdown及受限glob。
   不读仓库外祖先、不接受任意include/regex/脚本。祖先在前、子目录在后、路径规则按声明顺序；不能覆盖用户/执行策略。
2. RuleDocument/RuleSelection保存完整正文、哈希、版本和有序引用；单份16KiB、完整集合32KiB/16份，超限拒绝不截断。
   先路径过滤再读取，拒绝链接/junction/特殊文件/遍历/证据目录；前后身份检测TOCTOU。
3. ToolAccessPlanner先纯词法提取read/patch新旧路径/test目标和配置，再RuleAccessGate，再实际读取/正规化/执行。
   搜索逐候选、缓存先guard，补丁不能在检查前读原文件。
4. 新规则返回rules_pending零副作用；整批tool result闭合后注入低信任规则；下一请求登记真实送达才可重试。
   同一响应read→patch不能绕过，写进历史不等于模型看过。
5. 启用时search增加opaque cursor；遇首个新作用域停止该页，返回安全partial结果与cursor，绑定run/query/候选/规则版本。
   损坏/超限明确报错，重复无进展受现有循环保护。源码MCP正文需可验证来源路径，新作用域延期交付。
6. ContextManager保护/重新送达规则；TaskState仅引用；规则变更/删除追加新版本失效，不改历史请求。
   checkpoint固定正文/版本/送达位置，恢复不以活目录替换；无直接用户收据，不能改网络/预算/profile/能力。

退出：嵌套/匹配/同批/补丁预读/cache/search分页正确；两次压缩、continue及local/ordinary新进程恢复可追溯；恶意规则不能扩权；工程门通过。

## G7 受约束扩展生命周期

目标：Skills及多个MCP按需可用，说明、发现、升级不能悄悄扩权或改协议。

1. SkillContract受限TOML声明契约版本、TraceFix范围、后端、必需能力/工具；compatibility自由文本仅说明。
   allowed-tools做加载前可用性检查，不授予权限、不宣称逐调用sandbox；不满足拒绝，旧无契约技能标legacy。
2. 契约精确加入现有Skill快照白名单/身份/package-data，判断只用可信配置、目录与明确环境；不运行探测/安装脚本；恢复用原契约。
3. McpServerSpec固定ID、transport、程序/镜像、工具/schema摘要、独立CapabilityDescriptor、资源/期限及认证引用/网络profile。
   命名空间防冲突，annotations/文本不授权，无descriptor/内部控制名拒绝；显式prepare隔离锁依赖，运行中不安装升级。
4. 实际多服务采用Serena+官方Fetch，Fetch服务端依赖独立，不为它直接升级宿主client；固定镜像并验收协议协商。
   任务容器仍禁网；Fetch仅经受限出站代理访问批准HTTPS文档域名，单调用绑定初始host，拒绝私网/环回/linklocal/跨域连接。
   DNS全部地址检查后直接连接已验IP，限制响应/解压大小、调用次数和期限；无宿主目录/凭据挂载。
   CONNECT只限制出站目标，不能宣称检查TLS内HTTP方法/正文。依据官方Fetch说明，不引入新增账号要求。
5. 分开发现与激活：模型只选可信目录；分页/数量/schema字节有界，未知/漂移拒绝；listChanged只触发核验不自动注册。
6. ToolCatalogTransition保存from/to generation和目录哈希；批次闭合无pending时提交，首版add-only，已有绑定不变。
   schema变化要求新会话/具名converter；请求/响应绑定原generation，转后重投影/计数不改历史，恢复固定转换链不重新替换。
7. 每服务一个串行lease，key含服务/配置/schema/策略/认证身份，源码服务再绑定SourceSnapshotIdentity。
   同源码复用、源码改变关闭旧session/容器后新建，不热改只读挂载；受管event loop、deadline/idle/max-age、统一close。
   恢复重建连接不信旧PID；已发送未知不重发，清理错误附记不覆盖主因。

退出：legacy/契约拒绝、两服务按需发现激活、同源码复用/补丁换session、身份/schema/generation/出站拒绝、断线过期取消恢复清理；editable/wheel真实组合和工程门通过。

## G8 有界子代理

1. 先RootBudgetAuthority/ModelBroker：父/子/反思同入口，原子预留(已结算+未释放+本次)后才发；唯一request幂等结算。
   明确未发才释放，未知保留且停止新请求，取消后返回用量仍结算；无复制根额度/新价格授权；首版供应商请求串行，工具可并行，禁不透明重试。
2. explore/review使用独立只读循环/完成策略，不套修复Agent补丁+测试结束门；报告完成非修复通过，来源parent_delegation非直接用户。
3. 权限为父∩角色∩路径；固定源码快照、按需规则/Skill/记忆/产物，不复制全对话；无根TaskState/记忆/checkpoint写、测试、补丁或再次spawn。
   目录启动固定不随父扩展；默认depth1/live2/累计6、每子6请求/12000输入/2000输出/120秒，均受根剩余额度及G2总配额约束，跨恢复/continue不重置。
4. spawn_subagent/get_subagent/cancel_subagent仅任务/范围/更小额度；复用G5执行取消，父结束先收拢，未知模型请求不退款。
   结果通过查询返回，不异步插入未闭合批次。
5. 报告摘要/发现/源码身份/原call及artifact/停止原因；推论低信任，不自动发布记忆；父源码改变标stale；审查未发现问题不等于独立测试通过。
   根checkpoint固定子状态/报告/预算序列；恢复拒旧epoch，不接管执行中worker、不重发未知。
6. 先关闭只读工程门，再写任务；写单独开关默认关，同时最多1个。
   宿主获准当前源码(含未提交修改)建受管基准，再detached worktree，固定Git参数/空hooks/配置。
   Docker父受限导出到临时基准，子用独立容器，无原仓库/socket/凭据挂载；交patch ArtifactRef/基准/自身证据，不直接改父。
   父偏离基准拒自动整合，无隐式三方merge；主agent原apply_patch整合并重跑根gate；子测试计根额度但不给父验证资格。
   只清理本run确认所有权/绝对路径的工作树，保留报告/补丁。
7. Windows/真实ordinary写补丁、冲突拒绝、取消和清理回放后，运行最终零供应商完整组合。

## 共同验收与最终交付

每阶段Windows Python3.11/3.12全量pytest、Coverage原始行+分支精确综合>=90%、原Ruff/compileall/diff范围、
仓库外editable/独立wheel、原Docker/Serena回归及阶段真实集成。平台限制保留原因与替代证据，不降门槛。
证据绑定提交/源码清单/配置/解释器/镜像/协议。阶段完成再更新current-status、roadmap、development-history。

最终回放：规则/Skill→两MCP→后台日志→探索/审查并取消一子→独立写补丁→主agent整合→旧证据失效→安全暂停销毁容器→新进程恢复→历史日志/报告→根gate及独立补丁验证。
补齐CLI/TOML/Python API示例、矩阵、限制和零供应商入口。纯文档后续提交不得冒称新HEAD重做完整验收。

起点正式结果见[基线验收记录](2026-10-16-harness-runtime-baseline.md)，CI及本地原件已归档；验证归属于ac64510。
