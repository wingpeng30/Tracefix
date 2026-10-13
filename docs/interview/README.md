# TraceFix 面试资料入口

本目录是项目学习与面试入口。完整讲稿按“先理解当前系统，再看开发历程”组织，覆盖2026年9月7日至10月11日，已纳入30题复测之后的统一LLM网关与长驻tokenizer改动。

## 按目的阅读

- **第一次学习**：从[完整讲稿的分页例子](project-history-detailed.md#present-overview)开始，依次理解组件、工具、Repo Map、上下文和预算，再读[开发历程](project-history-detailed.md#history-overview)。
- **复习架构**：重点看[组件装配](project-history-detailed.md#present-assembly)、[Repo Map](project-history-detailed.md#present-repomap)、[上下文](project-history-detailed.md#present-context)、[预算与缓存](project-history-detailed.md#present-budget)、[统一网关与长驻计数器](project-history-detailed.md#present-gateway)和[配置对照](project-history-detailed.md#present-config)。
- **复习实验**：先读[10月9日至11日诊断与复测](project-history-detailed.md#stage-14)，再读[网关改动与离线验收](project-history-detailed.md#stage-15)，用[实验表](project-history-detailed.md#experiment-table)核对历史批次，最后返回原报告查口径。
- **面试前速读**：用[两分钟／五分钟介绍](project-history-detailed.md#introductions)组织口述，再检查[技术追问](project-history-detailed.md#questions)；“我实现”只用于自己的实际贡献。

## 最新结果与版本边界

同一30道公开任务各运行一次，A裸模型、B最简循环、上下文修复后的C分别有11/30、11/30、19/30次“正常完成且独立补丁验收通过”；独立补丁通过分别为11/30、17/30、21/30。A有10次未知响应，仍保留在30题分母里。

C比B完整交付更多、累计输入更少，但每题平均耗时272.7秒，高于B的195.1秒。计入缓存的总费用估算略高，每成功费用估算较低，尚无供应商最终账单。该比较由非同期历史同题记录组成，每题一次，不能宣称普遍更快、更省。完整数字与限制见[最新ABC报告](../experiments/2026-10-11-abc-full30-comparison.md)。

新C来自HEAD `82d72a39f097f3c033c463395133e2322ab78463`上的未提交源码修复；实际身份以报告中的源码清单SHA-256为准。已有定向测试和本批真实运行证据，尚未完成这份修复的全量回归、覆盖率、双Python版本、Docker Linux恢复和精确CI门槛。此次30题没有触发真正历史压缩，不能用缓存复用结果替代压缩收益证据。

30题之后，当前工作区新增内部LLMGateway，让窗口检查与预算准入复用同一正文的计数，发送冻结后的最终参数；比较实验的tokenizer worker在任务内长驻。179项定向离线检查通过，预算与未知请求保护保留。实际官方tokenizer环境在本机不可用，未做实际编码器子进程或冷启动／复用耗时检查，没有新供应商调用。该改动不能解释此前19/30结果，也没有真实模型提速结论。详见[第15章](project-history-detailed.md#stage-15)与[开发记录](../development-history.md#2026-10-11-统一-llm-网关与长驻-tokenizer-worker)。

## 其他面试资料

以下精简材料与清理说明继续保留；其中截至10月7日的结果应按历史状态阅读，最新实验以完整讲稿第14章和上方报告为准：

1. [完整项目讲解稿](project-history-detailed.md)：当前系统入门、十五个历史阶段、六幅流程图、最新实验、统一网关与worker、术语和面试介绍稿。
2. [项目历程与技术决策](project-story.md)：做了什么、为什么做、结果的精简版。
3. [实验节点与证据](experiments.md)：可信结论、失败和原始材料位置。
4. [面试问答与演示](interview-guide.md)：两分钟介绍、追问与演示命令。
5. [需手动复核的清理项](manual-cleanup.md)：自动未删除项、拒绝原因及不能删除的 C 依赖。
6. [实际清理结果](cleanup-result.md)：释放空间、保留范围、验证结果和限制。

## 10月7日归档状态（历史）

当时的代码基线为 `6980d2e0796000b5f30327ce007ae7251daa497f`，已上传 GitHub 的
`codex/bc-holdout-180s` 分支，不等同于已合入 main 或该提交已通过全部 CI。
归档资料提交建立在这个基线之上，不改变 Agent 产品策略。

本地精选原始档案：`E:\TracefixExperiments\interview-archive-20261006`。
该目录的 manifest、清理清单、执行记录和校验结果解释哪些材料被保存、哪些工作副本被舍弃。
原始请求及本地配置不公开上传。旧实验文档里的路径是原始来源；已精简的实验不能再声称
可以直接按旧路径原样复跑。查找原始文件时使用归档索引中的来源路径与 ZIP 内路径映射。

当时留出实验仅保护 C：6 次完成、3 次独立补丁通过；截至10月7日，新 180 秒协议尚无付费运行。
旧条件下的6次不能算成新条件下的进度。当时拟在旧授权内只续跑C，须另立协议并继承该账本已用费用。
10月9日至11日另有新授权与协议的同题实验，不把它们算作旧计划补齐；清理、演示和本次文档修订均不续跑付费实验。
