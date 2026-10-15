# TraceFix 当前状态

## 2026-10-16 G5—G8 实施状态

起点ac64510 / CI159已关闭；实施分支`codex/harness-runtime`，原工作区源码分离。
G5本地Runner/API/CLI、安全暂停、新Python进程恢复及共享baseline/finalgate已有定向通过。
新增ordinary异步作业协议、宿主持久收据、root预算票据与流式产物恢复组件。
真实组件回放已完成100KB中段读取、整树取消、删除旧容器后新Python进程/容器取回旧日志且不重跑、不重置配额。
这仍是组件验证：**正式Docker Runner/inspect/resume、回归基线共享产物装配和完整G5工程门尚未完成。**
公共Docker后台开关继续在模型前拒绝；G6—G8未开始。

6b70e6c的CI165双版本覆盖率89.5279%/89.5209%，未过90%；其Docker/ordinary/Serena通过。
旧CI与本地组件不能证明新源码完整通过。曾在本地完整检查期间提交导致身份保护拒绝，已记录无效；
后续完整检查固定提交及文件直到结束。无供应商调用，未合并或发布。
进度、失败归因、镜像和原始日志见[G5实施记录](tasks/2026-10-16-g5-progress.md)。

## 2026-10-16 G5—G8起点门已关闭，进入G5

实现ac64510的CI159五个job全部通过：两版Windows各1694通过/零跳过，覆盖率90.0775%/90.0698%；真实Docker/Serena、editable/wheel和公开资格通过。本机两版各1693通过/1权限skip，覆盖率90.1161%/90.1084%，同用例已在当前CI实际通过。G5进入状态契约子步骤，G6—G8未开始。 证据及边界见[起点验收记录](tasks/2026-10-16-harness-runtime-baseline.md)。
后续文档提交不借本结果宣称新HEAD重验；原工作区源码保持分离。

## 2026-10-16 G5—G8 已进入实施：先修复起点

用户已批准[分阶段计划](tasks/2026-10-16-harness-runtime-implementation.md)。实施分支`codex/harness-runtime`，
工作树`D:/Tracefix/runs/harness-foundations-worktree`，起点bb43e31；原工作区源码不混入。
实际核对发现G4保存提交2dfd759的[CI #157](https://github.com/wingpeng30/Tracefix/actions/runs/38768213294)失败：
双Python完整工程步骤通过，公开资格脚本漏dispatcher；ordinary组合在切换目录后用错Docker构建相对路径。
基础Docker及Serena通过。当前先复现修复这两项，G5—G8均未开始，不借原本地验收关闭起点门。
原始日志在候选`runs/harness-runtime-start/`，没有供应商调用。
下方“下一步仅规划”及旧上传限制保留历史归属，以本条和最新用户要求为准。

## 2026-10-16 GitHub 保存完成

用户已重新授权上传。当前原工作区快照0830b52与G0—G4候选源码快照2dfd759均已推送并核验，
分支分别为`codex/ac-30-180s`与`codex/harness-foundations`；未合并main、未发布、无供应商调用。
保存范围、验证归属与后续入口见[GitHub保存记录](maintenance/2026-10-16-github-save.md)。
现有CI可能自动运行，不能把原本地验收写成新提交的CI通过；后续仅规划G5—G8。
下方10月15日的“不上传”限制和未提交身份保留历史归属，不覆盖本轮已完成的授权保存。

## 2026-10-15 Harness Foundations 实施进度

G0—G3阶段门已通过。用户授权补齐环境后，G4已完成本地功能与工程验收；仍禁止上传、触发CI、合并和发布。
最新结果见[环境与验收记录](tasks/2026-10-15-g4-environment-acceptance.md)，设计与退出条件见
[阶段计划](tasks/2026-10-14-harness-foundations-implementation.md)。此前本地补齐与失败记录保持历史归属。

Windows Python3.11.16/3.12.5各1691 passed、0失败/错误、1项符号链接权限skip；同一测试Linux实际补证通过。
精确综合覆盖率分别`23368/25931=90.11607728201766%`、`23366/25931=90.10836450580386%`；
两版Ruff、编译、diff和当前工作树diff-check通过。editable/wheel真实ordinary Docker＋Serena完整组合、
必要容器故障与恢复、双版本仓库外安装回放全部通过，没有供应商调用。
原计划的远端候选CI退出项未执行，不能将本地结论写成新提交已经通过CI。

候选分支起点为 `bc8b224d0419c5acab4a0a4d2abfb92e0ff4c67e` 加经核对的24个已跟踪修改及两份维护报告；没有复制凭据、原始 runs 或临时实验夹具。导入清单及 patch 哈希保存在本机忽略目录 `runs/harness-foundations-setup-20261014`。基线提交 `f79a5c876f5eea22782b44b823b3c0bd64fde951` 的 CI #140 首轮失败及归因、修复后的 CI #142 通过结论，均保留在[本次 G0 记录](development-history.md#2026-10-14-harness-foundations-g0-首次-ci-与基线修复)。

G0 实现提交 `bcb6d24ac5e173228749b51c57a8a7d944c7d144` 的 CI #142（[run 38566440638](https://github.com/wingpeng30/Tracefix/actions/runs/38566440638)）完整通过：Python 3.11.9/3.12.10 各1364 passed、0 failed/error/skip；Coverage.py combined line+branch 分别为 `18964/21064 = 90.03038359285985%` 和 `18962/21064 = 90.02088872009115%`。Ruff、compileall、diff、editable、两版独立 wheel/recovery 及零供应商公开任务资格通过；基础 Docker、ordinary Docker 和 Serena MCP 也通过。G0 Artifact 原件、digest 与基线源码清单已归档在开发历程。G0 阶段门关闭。

### G1：有类型、可失效的知识记忆

实现提交 `d059ec269e7b19571b16cc6fe823a66f6461e2da` 已推送到候选分支；CI #144（[run 38596011475](https://github.com/wingpeng30/Tracefix/actions/runs/38596011475)）全部成功。Windows Python 3.11.9/3.12.10 各1388项测试、0失败、0错误、0跳过；Coverage.py combined line+branch 分别为 `19979/22196 = 90.01171382231033%` 和 `19977/22196 = 90.00270318976392%`。两版 Ruff、compileall、diff、editable、独立 wheel/recovery 和零供应商公开资格 smoke 均通过；基础 Docker、ordinary Docker、Serena MCP 三个真实 CI job 通过。

[有类型知识记忆](experience-memory.md)增加四类知识 DTO，分离发布状态、证据等级与 checkout 适用性；宿主签发直接用户输入收据；以 common-dir 识别 worktree 仓库并对 checkout 分别按源码、依赖和环境身份评估。v1 只读归一化，迁移收据与本次写入进入同一个 v2 原子信封，保留原 v1。自动召回只接受 `observed/user_stated` 且当前 `matched` 的 active 记录，最多3条/8 KiB，并将不可变 bundle 固定在运行与恢复历史中。反思使用有界证据快照和既有累计预算。

ordinary Docker 的 editable 与 wheel 零供应商回放都通过：暂停后销毁旧容器并恢复到新容器，固定记忆 bundle 未变化；最终7次累计请求、700输入/70输出 token、2次测试，反思状态为 `duplicate`，独立容器3/3测试通过。Python 3.11/3.12、容器和 MCP Artifact ID、digest、CI #143 首轮失败的归因修正、候选源码清单与本地限制见[G1 开发记录](development-history.md#2026-10-15-harness-foundations-g1-有类型与来源限定记忆)。

最终实现身份：tree `1562a6408f37f1b4d4d1475471a0dc80d09c7558`，711个跟踪文件；忽略目录源码清单 `runs/g1-source-manifest-d059ec2.json` 的 SHA-256 为 `771d8ad4ad1607e889addd4def6b634cd11d35d43f7ff77782a0ab2c4d912148`。本轮之后的文档提交只记录该已验收实现，不表示文档提交的新 HEAD 重新跑过工程门。

本机没有 Python 3.11，Docker daemon 未连接；Windows 沙盒会拦截 `Path.resolve(strict=True)`，因此本地测试使用已授权的提权进程并把临时目录放在候选 worktree 的 `runs/` 下。G0 结论绑定 bcb6d24/CI #142，G1 结论绑定 d059ec2/CI #144；本地 Python 3.12.5 全工程结果是辅助证据，不代替双版本和真实容器 CI。

### G2：可取回的工具产物（阶段门通过）

验收实现身份为候选提交 `075bfae7d921b0d4ca7729b5a69f5c5218e89a10`，tree
`2d37ed9dc063f1a819104c6a640b5d88c0acdac5`；CI #152（[run 38699965369](https://github.com/wingpeng30/Tracefix/actions/runs/38699965369)）
在该精确 SHA 上全部成功。Python 3.11 与 3.12 各1519 passed、0 failed/error/skip；Coverage.py combined
line+branch 分别为 `21449/23812 = 90.07643205106669%` 与 `21447/23812 = 90.06803292457585%`。
两版 Ruff、compileall、diff-check、editable、公开资格、wheel/recovery smoke 均通过，基础 Docker、ordinary Docker
和 Serena MCP 三个真实容器 job 均通过。阶段证据、Artifact ID/digest 及前序失败归因见
[G2 开发记录](development-history.md#2026-10-15-harness-foundations-g2-工具产物)。

G2 增加同步双流进程采集、有界 raw 与脱敏 UTF-8 检索视图、完整性校验、持久读取配额、分页读取/字面搜索、
Docker evidence 归档传输和恢复校验。使用入口及额度见[工具产物说明](tool-artifacts.md)。本机 Docker 未运行，
按用户要求由 CI 的真实 Docker/MCP job 验收。G2 验收结论绑定上述实现 SHA/tree 和 CI #152，不声称文档提交
重新跑过工程门。

### G3：结构化 TaskState（阶段门通过）

验收提交 `9d52583ede2b4d56b12c18794543e53afc20e1b1`，tree `497d478c004b34ceafd832b511156a0c4f04f0`，
CI #156（[run 38714764105](https://github.com/wingpeng30/Tracefix/actions/runs/38714764105)）全部成功。
Windows Python 3.11/3.12 各1544 passed、0 failed/error/skip；Coverage.py combined line+branch 分别为
`22125/24541 = 90.15525039729432%` 与 `22123/24541 = 90.14710077013977%`。Ruff、compileall、diff-check、
editable、独立 wheel/recovery smoke、基础 Docker、ordinary Docker 和 Serena MCP 均通过。

TaskState 将直接用户需求及宿主收据、验收条件、带来源和有效性的观察、计划/假设/问题/阻塞、memory/skill/artifact 引用
投影为单调 revision 的有界工作状态。模型更新工具只允许修改计划、假设、问题和阻塞，不能写入用户需求、可信证据、
验证资格、预算或权限；恢复校验状态哈希及其提交历史引用。ordinary Docker 回放验证暂停后销毁旧容器，在新宿主/容器进程
恢复后仍保留需求与 artifact 引用，并独立验收补丁。完整细节及早期 CI 修正轨迹见
[G3 开发记录](development-history.md#2026-10-15-harness-foundations-g3-taskstate-阶段门通过)。

最终源文件清单为18项，忽略目录清单 `runs/g3-source-manifest-9d52583e.json` SHA-256
`6aff20378f7e6b18f562c963bdd26cd7ea0b5392d095c1bbc74ab5215cbd9ea9`；代码清单、CI 原始产物 ID/digest 及 #154/#155
失败归因见开发记录。本机没有 Python 3.11、Docker daemon 与可用 Serena socket；按用户指示跳过本地容器，必需的双 Python、
真实 Docker 和 MCP 检查由上述 CI 执行。阶段门关闭，下一阶段为 G4。

### G4：统一能力策略与Docker/Serena组合（本地验收完成，远端CI未执行）

本地候选目录`D:\Tracefix\runs\harness-foundations-worktree`，基底HEAD
`9a3d756a84c22ee17ff9d27959f2716c8ab2d493`；实现仍是未提交工作树，没有合入原工作区源码。
验收绑定258项源码/测试/构建输入清单，SHA-256
`ce7618dbd4ac0828e17a91ecc93529f0a07aa84d28e14ecfb90697c9c89ea6ce`，运行期间无漂移。
使用方法、CLI/TOML/API及后端矩阵见[能力策略](capability-policy.md)；完整结果见[验收记录](tasks/2026-10-15-g4-environment-acceptance.md)。

本轮补齐Docker及独立双Python/MCP环境；修复Windows入口换行、3.11测试语法、回放记忆格式、
TaskState压缩丢revision及固定Skills恢复误判。没有放宽预算、来源、路径或身份保护。
两种安装方式真实组合均验证：固定记忆/Skill、日志中段检索、任务状态、补丁后Serena查询、
销毁旧容器/新进程恢复、第二轮输入、最终回归门和独立新容器验收。每组16次合成请求、7次累计测试、2次Serena查询，供应商调用0。
独立验收同时检查主目标与追加目标；本轮容器已清理，其他历史容器未改动。

双版本完整工程原件`D:\Tracefix\runs\g4e311`与`g4e312`，各1691通过、0失败/错误、1项Windows权限跳过；
当前源码Linux同用例1 passed、0 skipped。精确综合覆盖率90.11607728201766%/90.10836450580386%，未拼接旧数据。
原始工程summary accepted=true，全部本地汇总`runs/g4-environment-acceptance/acceptance-summary.json`的local_acceptance_passed=true。
最终wheel SHA-256 `61b174bc770296f0f16b3de60aeeae114c5effdb1065a0a1a1e87f51c13859bc`，85项Python文件与当前源码一致。

保留限制：Windows真实符号链接创建权限未改变；深TEMP路径仍需避免，已用短验收目录解决本轮启动超时。
源码尚未合并、提交、上传；远端CI按用户指示未触发，原CI退出项不能标记完成。
旧本地结果/失败和原始计划保留在[前次补齐记录](tasks/2026-10-15-g4-local-completion.md)与开发历程，不用来替代本轮证据。

## 最新 harness 比较与候选改进

2026-10-14 按用户要求比较官方 DeepSeek Harness、Codex、Claude Code 的说明和公开代码，
已保存[比较与改进报告](maintenance/2026-10-14-harness-comparison-and-improvements.md)。
比较报告中的有类型记忆、可取回工具产物和 TaskState 已分别在 G1、G2、G3 通过阶段门；G4 实现已经存在于本地候选
工作树，但尚无双 Python/真实容器 CI 验收。没有新模型收益证据或付费授权。

审阅比较报告时的候选 HEAD 为 f79；随后 G0 follow-up 已在候选工作树修改基线测试和示例 smoke，
没有改运行功能。CI139 部分归因项的现状需按当次代码核对，不能把历史失败清单直接等同当前未修复项。
当前配置支持 Docker recovery 下的记忆/连续对话，
自定义 Skills、Serena MCP 和结束前回归目标仍有后端组合限制。此事实优先于旧说明中的一概 local-only。
原比较报告只新增分析文档；其结论不作为本轮 G0 的验收依据。继续按用户当次任务执行，不自动启动历史付费实验。

## 当前代码身份

- 工作区：`D:\Tracefix`，分支`codex/ac-30-180s`。2026-10-13 按用户要求将最新源码、测试、
  实验驱动和文档纳入 Git 提交并上传该分支；父提交为
  `82d72a39f097f3c033c463395133e2322ab78463`，提交主题为
  `chore: upload latest context gateway and security workspace`。精确身份以该提交及远端核验记录为准。
  本机导入路径核对为 `D:\Tracefix\src\tracefix\__init__.py`。临时测试目录、第三方实验仓库、
  本机凭据及原始 runs 仍留在本地。开始后续任务仍需重新核对 HEAD、状态及实际导入路径。
- 同日已修复计数进程 UTF-8 编码契约，父提交 `b6898c2152df8b217bee27d88da44dcd8a6ddfe9`，
  修复提交主题 `fix: enforce UTF-8 tokenizer worker protocol`，上传同一分支；精确提交及远端核验
  记录在 `runs/counter-utf8-fix-20261013/remote-verification.json`。本次相关回归 123 passed、
  1 skipped、0 failed（404.51 秒），此前失败的进程重入测试及完整90条离线比较流程均通过。
- 包版本仍为`0.8.4`，不是当前全部功能的发布/验收身份。没有将最新上下文及网关工作区宣称为
  已通过完整发布门槛的新版本；不能把旧wheel或旧精确提交CI当作这些修改的验收。
- `ac-thirty-frozen`与`ac-continuation-frozen`是有意保留的旧实验工作树。它们不跟随本工作区更新，
  不为消除旧状态而修改冻结源码、报告或账本。

## 已实现与验证边界

1. **上下文修复已实现。** 动态测试事实和提醒追加尾部、保留用户消息、稳定版本化投影、80%触发/60%目标、
   输出预留、主动压缩入口及恢复状态同步。见[上下文说明](context-management.md)。
   30题真实运行的前缀审计无异常，119个带测试事实请求的加权缓存命中率99.26%；真实历史压缩触发0次。
   这些结果验证了追加与缓存复用，不能替代跨阈值压缩、主动压缩或真实Docker恢复的全部验收。
2. **统一网关和长驻worker已实现，发生在上述付费运行之后。**
   [LLMGateway](../src/tracefix/models/gateway.py)让窗口检查、费用准入和冻结发送共享计数；
   [OfficialCounter](../src/tracefix/comparison_profiles.py)复用JSONL子进程。
   10月11日[开发记录](development-history.md#2026-10-11-统一-llm-网关与长驻-tokenizer-worker)
   记载179项定向离线检查及Ruff/修改文件compileall/Diff通过，3项慢速完整块重入检查未运行。
   实际官方tokenizer环境在那次验证中不可用，未获得真实编码器加载/冷暖耗时或新供应商提速证据。
   不要重复规划这两项实现，也不要把它们归因到之前的19/30结果。
3. **最新工作区的完整工程验收未通过。** `bc8b224` 的CI #139已有Python3.12全量结果：
   27 failed、1272 passed，精确覆盖率88.91113653699466%，低于90%；ordinary Docker集成回放失败。
   Python3.11随后完成：同为27 failed/1272 passed，覆盖率88.9206712433257%，失败清单完全相同。
   基础Docker与MCP通过，完整CI最终失败。见[CI归因](maintenance/2026-10-13-ci139-diagnosis.md)。
   用户当前任务优先，不把该清单自动变成每次分析或文档任务的前置链。
4. **基础安全加固已实现。** 新任务默认为预备镜像的 ordinary Docker，文件路径/敏感文件过滤、pytest 选项白名单、子进程最小环境、只读执行器身份校验和低信任上下文均已落地。规范与真实验证范围见[安全规范](security.md)及[10月12日开发记录](development-history.md#2026-10-12-基础工具安全边界与普通-docker-默认值)。定向回归通过 235 项、边界模块 19 项、环境组 193 项，另有两个 P2 环境回放通过；一项 Windows symlink 用例跳过，一个范围外 P2 输出预算测试未通过。一次真实 Docker 录制响应 smoke 通过，不调用供应商。该基线不保证完全防止提示注入，也不保证测试通过的补丁无危险行为。

## 最新能力比较：同一30题，每臂一次

主成功=正常结束且独立补丁验收通过。超时计失败，统一保留30题分母。

| 指标 | A裸模型 | B最简工具循环 | 上下文修复后C |
|---|---:|---:|---:|
| 主成功 | 11/30（36.7%） | 11/30（36.7%） | 19/30（63.3%） |
| 独立补丁通过 | 11/30 | 17/30 | 21/30 |
| 正常完成 | 14/30 | 18/30 | 27/30 |
| 超时/响应未知 | 10 | 0 | 0 |
| 平均交付时间 | 93.5秒 | 195.1秒 | 272.7秒 |
| 已知加权供应商缓存命中率 | 22.93%，缺未知用量 | 92.80% | 89.89% |

最新结论和全部费用/Token/工具数据以[ABC横向报告](experiments/2026-10-11-abc-full30-comparison.md)为准。
C相对B交付更多、累计Token更少、每成功缓存费用估算更低，但平均每题更慢；相对A不能宣称更快或完整实际
成本更低。非同期、公开同题、每题一次，只支持该样本的探索观察。记忆、Skills、MCP、连续对话在实验中关闭，
未衡量这些机制或Docker恢复的收益。

该C的冻结源码清单SHA-256为`b4b3d76abf2ebe40795f06061b58c049c9b4c8c50d465e331fdbd1072834eb7e`；
它早于网关/长驻worker改动。旧C的14/30、66.8%缓存率见[旧报告](experiments/2026-10-10-b-c-full30-comparison.md)，
仅是旧批次，不能作为新C进度。[耗时调查](experiments/2026-10-11-b-c-context-comparison.md)中“下一步实现网关”
也是当时记录，如今实现已完成；后续待的是实际计数环境验收与提速验证。

## 证据和存储位置

- A及旧C：`E:\TracefixExperiments\20261009-ac-30-180s`；按canonical三批记录统计，不重复计算汇总副本。
- B：`E:\TracefixExperiments\20261010-b-30-180s`。
- 新C：`E:\TracefixExperiments\20261010-c-context-retest-30-180s`。
- 横向分析、逐题CSV及哈希索引：`D:\Tracefix\runs\abc-full30-comparison-20261011`。
- 这些原始目录保留归属/清理标记，不能为更新入口而覆写旧结果或复制整份工作区。

## 下一任务的起点

最新[CI #139](https://github.com/wingpeng30/Tracefix/actions/runs/38355799150)已取得3.12与Docker原始产物，
根因包括过时default/Docker夹具、旧技能标签断言（内容实际进入请求）、首条USER审计误识别Repo Map、
长上下文辅助未显式配置32k阈值、P2可选close接口不兼容与兼容发送遗漏输出上限。
另有工程脚本cp1252解码崩溃、历史驱动74项lint及覆盖率不足。详见[完整归因](maintenance/2026-10-13-ci139-diagnosis.md)。
本轮仅调查和本地离线复现，未修代码、未重跑远端或降低门槛；双版本原始产物均已核对。

首次上传是最新工作区快照备份，不是正式发布验收。源码/测试 Ruff、Python 编译及 diff 检查通过；
定向离线复验 75 passed、1 skipped、1 failed，失败为比较实验的进程重入测试 C 臂 agent_error/空补丁，
跳过项为 Windows symlink 权限。历史实验驱动另有 74 项 Ruff 诊断，保留原始哈希和内容。
后续归因已复现进程重入失败：父进程 UTF-8 JSON 与 Windows 子进程 GBK stdin 不一致，
导致 fake_counter 的 JSONDecodeError；同一请求以 `-X utf8` 启动计数器则成功。
现已在一次性/长驻客户端固定子进程 UTF-8 环境，并在真实 worker 入口显式设置 UTF-8。
四项新回归核对 GBK 环境下中文、重音字母及 emoji 原文往返，相关回归最终 123 passed、1 skipped，
原先失败已解决；Windows symlink 权限跳过项及历史实验驱动 lint 限制仍保留。
本次不加载实际官方 tokenizer、不新增付费请求、不改原始实验成绩。
详见[编码修复记录](development-history.md#2026-10-13-计数器-utf-8-进程协议修复)。
详见[本次开发记录](development-history.md#2026-10-13-最新工作区提交与-github-上传)；未运行全量测试、
覆盖率、精确 CI 或付费调用，原有发布验收限制保持。

按用户新的明确请求执行；没有自动续跑的付费实验。需要优化验证时，优先确认固定tokenizer环境下的
实际加载、共享计数、worker复用和计时，再按正式交付范围做工程门槛。新的付费复验另绑定实现与协议，
不能沿用旧实验结果当作新代码效果。

项目学习入口见[面试资料](interview/README.md)，使用入口见[普通仓库指南](ordinary-repository.md)。
历史交接见[交接日志](handoffs/2026-09-29-agent-harness.md)；旧“37次未运行”等文字仅指当时进度。
每次完成代码、实验或结论更新时同步本页，避免仅更新日志后让新会话继续读到旧计划。
