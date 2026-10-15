# TraceFix 当前状态

更新日期：2026-10-15（Asia/Shanghai）。这是新会话的固定状态入口；日期化交接、路线图条目与实验报告
保留当时事实，不自动成为今天的任务。先读本页，再按用户当前请求读取对应源码和证据。

## Harness Foundations 候选实施进度（本机入口）

G0/CI #142、G1/CI #144、G2/CI #152、G3/CI #156已通过。按用户新授权补齐Docker与独立双Python/MCP环境后，
G4本地功能与工程验收完成；没有上传、触发CI、付费模型调用、合并或发布。
实现仍位于`D:\Tracefix\runs\harness-foundations-worktree`，本目录源码没有被候选改动覆盖。

Python3.11.16/3.12.5各1691 passed、0失败/错误、1项Windows符号链接权限skip，Linux同用例实际补证通过。
精确综合覆盖率分别90.11607728201766%/90.10836450580386%；Ruff、编译、diff及当前工作树diff-check通过。
editable/wheel真实ordinary Docker＋Serena完整组合、必要恢复/身份/超时故障及双版本仓库外安装回放全部通过。
最终结果、使用入口及原始产物见[候选验收记录](../runs/harness-foundations-worktree/docs/tasks/2026-10-15-g4-environment-acceptance.md)。

验收绑定258文件清单`ce7618dbd4ac0828e17a91ecc93529f0a07aa84d28e14ecfb90697c9c89ea6ce`及基底9a3d756的未提交工作树，
不是新Git提交或CI结果。原计划的候选CI退出项未执行；Windows符号链接权限未变，深临时目录需避免。
下方比较报告与原工作区CI139状态按其原源码身份保留，不覆盖候选最新结论；历史19/30不证明新功能收益。

## 最新 harness 比较与候选改进

2026-10-14 按用户要求比较官方 DeepSeek Harness、Codex、Claude Code 的说明和公开代码，
已保存[比较与改进报告](maintenance/2026-10-14-harness-comparison-and-improvements.md)。
建议优先增加有类型且可失效的知识记忆、可取回工具产物、结构化任务状态，再完善统一能力策略、
可取消作业及 Docker 功能组合，之后评估有界子代理。现有记忆、沙盒、上下文、网关和恢复均作为
已有基础，不重复规划；这些建议尚未实现，也没有新增模型收益证据或付费授权。

审阅 HEAD 为 `bc8b224d0419c5acab4a0a4d2abfb92e0ff4c67e`，另有未提交源码/测试修复。
静态核对发现当前已包含 CI139 部分归因项的修复，不能把历史失败清单直接等同当前尚未修复；
本轮未验证这些修复或重跑工程链。当前配置支持 Docker recovery 下的记忆/连续对话，
自定义 Skills、Serena MCP 和结束前回归目标仍有后端组合限制。此事实优先于旧说明中的一概 local-only。
本次只新增报告及同步文档，不更改运行实现；源码身份、来源收据与文档检查保存在
`runs/harness-review-20261014`。继续按用户当次任务执行，不自动启动候选功能或历史付费实验。

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
