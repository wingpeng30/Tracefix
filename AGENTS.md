# TraceFix 持续维护约定

新会话首先读取固定入口 [`docs/current-status.md`](docs/current-status.md)，再读
[`docs/roadmap.md`](docs/roadmap.md)、[`docs/development-history.md`](docs/development-history.md)
开头及当前状态引用的报告。不要以文件名日期、包版本号或旧交接末尾决定当前进度。

## G5—G8实施入口

最新用户已批准实施G5—G8。ac64510 / CI159及本地双版本起点门已关闭，G5.6进行中；G6—G8未开始。
G5本地Runner/API/CLI、安全暂停及新进程恢复已有定向通过，基线/最终门共用根账本；
候选正式Docker入口/恢复与baseline共享产物现已真实回放；完整安装包与>=90%工程门仍待完成，G5未关闭。实际证据见固定状态入口引用。
原工作区源码与候选分开，实施分支codex/harness-runtime；下方旧状态按日期保留。

## 2026-10-16 当前入口补充

用户本轮已授权保存项目：原工作区0830b52、G0—G4候选2dfd759均已推送至各自分支。
候选G4已有双Python及真实Docker/Serena本地验收；新提交CI需读实际结果，不借旧证据。
没有合并main或发布。下一任务仅规划G5—G8，先读取固定状态及其引用的保存记录。
下方旧日期的上传限制及实现状态保留历史归属。

## 当前状态提示（2026-10-15）

- 候选分支 `codex/harness-foundations` 的 G0—G3阶段门已完成，远端最后核验G3提交为
  `9d52583ede2b4d56b12c18794543e53afc20e1b1` / CI #156。G4已完成环境补齐、双版本完整工程及真实editable/wheel Docker＋Serena组合的本地验收，
  不上传、不触发CI；原候选CI退出项未执行，不能冒称新提交通过CI。详细状态及源码清单见
  [`docs/current-status.md`](docs/current-status.md) 和 [`docs/development-history.md`](docs/development-history.md)。

- 同一30题A/B/上下文修复后C主成功为11/30、11/30、19/30；超时计失败。
  最新汇总为[`ABC横向报告`](docs/experiments/2026-10-11-abc-full30-comparison.md)，不是旧C的14/30。
- 在这批付费实验之后，当前工作区已实现LLMGateway共享计数和长驻tokenizer worker，
  记录有179项定向离线检查通过；真实tokenizer耗时、全量工程门槛与新付费提速证据仍未完成。
  不要再次把“实现共享计数/长驻worker”列为未做，也不要把19/30归因于之后的网关改动。
- 10月13日按用户要求提交并上传最新工作区快照；实际身份以当前HEAD及固定状态入口为准。
  冻结实验工作树、历史CI和旧wheel不等于此工作区的验证身份。
  开始代码任务时核对实际cwd、Git HEAD/状态和导入路径。

## 维护与验收

每次代码、实验或结论发生变化，结束前同步更新`docs/current-status.md`，并在开发/实验记录
保存实际改动、验证、限制、代码与任务身份、环境、配置和原始产物位置。
新入口若改变，同步更新AGENTS、README和文档索引的链接；避免只追加日志而漏掉启动入口。

用户当前指令与已经给出的授权优先。按当次任务选择必要验证，不自动重启旧交接中的离线演练、
付费排程或全量前置链；正式交付仍遵守当次方案的工程门槛，不降低覆盖率或预算保护。
付费调用只在用户授权的任务及额度内执行，历史额度和未决请求不得自动续用或重发。
合成控制流验证、资格验证、独立补丁通过与真实模型主通过率分开表述；计费估算不是供应商账单。

## 历史入口

[`docs/handoffs/2026-09-29-agent-harness.md`](docs/handoffs/2026-09-29-agent-harness.md)保留跨会话历史。
旧维护约定已移到[`截至10月2日的归档`](docs/handoffs/2026-10-02-maintenance-archive.md)。
历史文件中“当前”“下一步”“尚未运行”均指其记录日期，不能覆盖固定当前状态或用户最新指令。
