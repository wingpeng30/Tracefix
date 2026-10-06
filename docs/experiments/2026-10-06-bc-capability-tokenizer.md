# 2026-10-06：B/C 费用约束能力探索与官方计数

用户新授权B、C各25元、十个既有开发任务各一次，共20次任务运行；每题每组最多2.50元。
最新要求是在费用范围内尽可能取消人为Token、上下文、请求、测试及执行时间限制。
本轮不执行裸模型，不续用历史账本，不访问留出集，不根据表现换题或调整策略。

## 资源与计数

新增`bc-350k-tokenizer`保留最初批准的350000输入额度；正式新实验使用后续要求的
`bc-capability-tokenizer`，不再以350000累计输入停止。
Agent配置中的1250000输入及312500输出是2.50元按高峰2/8元每百万Token推导的理论最大值，
并非额外分配额度。次数、测试及总时间用2147483647兼容现有非空整数字段，在本轮视为不设操作上限。
保留单次供应商调用120秒及工具自身超时、供应商实际上下文容量和未知费用停机保护。
单次输出按实际剩余费用、字节输入费用预留、剩余窗口及供应商输出容量动态计算；不固定8192。
C的上下文窗口采用1048576，压缩触发提高至窗口减8192，算法、提示和其他策略不变。
因此这轮不是只提高费用而保持所有资源参数不变的实验。

旧三臂协议和默认资源保持原样。新协议冻结两组、一次重复、费用边界与计数环境，
报告由协议生成计划数和组别，分别展示独立补丁、正常结束、正常结束且独立通过三个指标。

费用预留仍使用UTF-8字节加1024的保守上界；官方tokenizer用于请求计数和供应商窗口检查。
350k配置用官方估算决定输入准入，能力配置使用费用作为主要累计停止条件。
每次保存估算、字节上界、供应商实际用量与差异；计数差异结算已知响应后停止整个实验。
官方计数仍标为estimate，不改变正式P2的verified_exact/verified_upper_bound要求。

## 已完成的离线证据

Windows没有固定deepseek-recipe 0.1.1的官方wheel；本机Docker daemon未启动。
使用已有Ubuntu-24.04 WSL Python3.12.3，在工作区tmp/bc-tokenizer-libs独立安装Linux wheel，
不改主环境。Linux发行版缺少ensurepip，venv创建失败已保留；改用工作区隔离target目录。
Windows进程通过stdin/stdout JSON调用WSL官方worker，冻结解释器、包文件、模板和tokenizer身份。
原官方源码副本CRLF哈希不同，另存规范LF副本tmp/bc-v41-tokenizer.json，不修改原件；
SHA256为81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99。

tmp/bc-tokenizer-calibration.json离线回放上一轮全部212个请求，逐项计数与供应商usage相同，
供应商调用零。tmp/bc-tokenizer-real-smoke.json另验证真实文本、中文、Unicode、转义和工具schema。
历史吻合不构成任意未来请求的精确计费证明。新的付费请求仍须满足资格、工程与精确CI门槛。

开发中源码身份变化使早期重入检查拒绝，原始失败保留在tmp/bc-legacy-tests-01及
tmp/bc-profiles-faults-03；这些运行不作为最终工程验收。稳定源码复验另存。

## 执行与报告

`tracefix-compare prepare --profile bc-capability-tokenizer --tokenizer … --counter-runtime …
--previous-campaign … --catalog … --prices … --campaign-dir …`。
counter-runtime是显式worker命令JSON；不提供时使用当前Python的原生worker。
prepare离线复核212请求并重新资格十题，复用上一轮冻结问题材料，冻结新协议与计数身份。
run串行、独立工作区和另一干净源码验收；首次B/C配对审计计入正式20次。

完整离线排程、故障注入、双版本全量pytest、精确覆盖率>=90%、安装包及Linux Docker/MCP
精确提交CI全过后才付费。记录代码、任务、环境、请求、费用和产物哈希，不修改原始失败。
报告配对C-B及固定种子20261005、10000次任务级bootstrap；每题只有一次，不衡量重复稳定性。
不声称全面能力、记忆、多轮或Docker恢复收益；不为花满50元增加运行。

下文保留实施前状态；实际付费结果及限制见末尾。

稳定源码定向复验42项通过（tmp/bc-profiles-faults-04），旧协议定向20项通过
（tmp/bc-legacy-tests-final-02）。tmp/bc-offline-20-real-01使用真实官方计数完成20次离线
三步工具/文件与pytest闭环，20次均正常结束且独立通过，所有计数差异为零，供应商调用零。
另一进程复用20条记录，账本SHA256保持
e01cbbe4e919eb5881f4c70d22ff3328f0b5cdd1b86848d5ed01168803981bb6。
editable及仓库外wheel命令/报告通过；wheel SHA256为
ca9eb0c9b32add210672077fa94d6178e9c61c8f8d0a37c8132d0e133069275d。
价格原始页面于2026-10-06T05:36:21Z重新保存，仍为高峰2/8元每百万Token。
官方模型列表也复核1048576窗口与393216最大输出；原始页面保存在tmp/bc-capability-source.html。

## 正式付费结果（2026-10-06）

冻结评测实现为 `09f97556dd6ac67a8d318a1fbd524e92c6ca7e2e`，产品基线为
`bc7dd7df4939a374062ea0c7039279de81112cd2`。协议 SHA256 为
`e0bf8579dba8eb47fa45de6ff4565d13b3d9a7ed1f2befd83277d1c41cd6e6a1`。
[精确实现 CI](https://github.com/wingpeng30/Tracefix/actions/runs/37420320591) 五项通过后才付费。
Windows 3.11/3.12 各1141项测试、零失败错误跳过，精确综合覆盖率分别90.1872418%与
90.1765116%；editable/wheel、Linux Docker/ordinary恢复/MCP及真实官方tokenizer门槛通过。
五份ZIP的GitHub digest与本地SHA256一致，保存在实验目录ci-artifacts。

十题重新证明原始失败、参考修复、冻结回归、实际源码导入及环境身份；Markdown/Click
沿用已记录的纠正参考。没有替换任务，也没有访问留出集。350k配置保留，实际执行的是
bc-capability-tokenizer：取消60k/350k累计准入，费用推导的兼容累计上限为输入125万、
输出31.25万；次数/测试/执行时长使用足够大的兼容值。单次输出按剩余费用、字节输入预留
与供应商窗口动态下传，C窗口为1048576，压缩触发1040384。仍保留120秒请求超时、工具
超时、费用预留与未知结果停止。该配置不等于无限制运行，不改变普通运行默认预算。

首对B/C均正常完成且独立通过，15请求计数差异零，保守费用0.174298/0.158278元；
审计后继续剩余排程。最终20/20已启动并形成结果，未执行0，345请求全部已知且结算，
全部返回deepseek-flash，全部官方计数与供应商输入usage一致。没有自动重试、人工提示或重抽。

| 指标 | B 最简工具循环 | C TraceFix |
|---|---:|---:|
| 独立补丁通过 | 6/10（60%） | 6/10（60%） |
| Agent正常结束 | 8/10（80%） | 9/10（90%） |
| 正常结束且独立通过 | 5/10（50%） | 6/10（60%） |
| 实际累计输入Token | 5,180,060 | 2,545,686 |
| 实际累计输出Token | 42,005 | 30,951 |
| 保守高峰未命中费用（元） | 10.696160 | 5.338980 |
| 每个成功的平均费用（全部费用/成功数，元） | 1.782693 | 0.889830 |
| 运行记录累计修复时间（秒） | 1698.248 | 1302.705 |
| 预算停止 | 2 | 1 |

总保守费用16.035140元，B/C独立各25元、单次2.50元均未超过。费用不是供应商账单；
采用启动时重新核验的[官方高峰未命中价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)
输入2元、输出8元/百万Token。未为花满授权而追加运行。
时间来自每次record.seconds，含运行准备与Agent，独立验收/等待CI另计。
C费用低约50.085%，但基础设施污染与单次采样限制了因果解释。

| 任务 | B | C |
|---|---|---|
| more-itertools #462 | 正常通过 | 正常通过 |
| Markdown #1414 | 正常通过 | 正常通过 |
| Click #2800 | 临时产物污染，未通过 | 正常通过 |
| Requests #1142 | 正常通过 | 正常通过 |
| Requests #1766 | 正常通过 | 正常通过 |
| pytest #10051 | 正常通过 | 正常结束，独立测试失败 |
| pytest #10081 | 预算停止，补丁通过 | 正常通过 |
| pytest #10356 | 预算停止，独立测试失败 | 正常结束，独立测试失败 |
| Sphinx #10435 | 临时二进制产物导致补丁应用失败 | 正常结束，独立测试失败 |
| Sphinx #10449 | 临时二进制产物导致补丁应用失败 | 预算停止，空补丁 |

C−B独立补丁通过率差为0个百分点，固定种子20261005、10000次任务级bootstrap的
95%区间为[−30,30]个百分点；C胜1题、负1题、平8题。没有证明成功率提升。
正常结束率差+10pp、正常且通过率差+10pp为描述统计，不取代主指标。
历史90次实验资源和重复次数不同，仅作描述性参照，不能归因于某一个改动。

### 真实运行暴露的评测缺陷

B运行结束时重新传入只有build目录的保护集，遗漏RunTestsTool动态登记的test临时目录。
Click B原补丁包含2个临时产品，独立验收报已有临时路径；Sphinx B两题原补丁分别包含
1246/293个临时文件diff，其中二进制diff导致git apply --check退出128。原始补丁保持原样，
未清洗、未补跑；三次全部计为未通过，另列评测基础设施影响。
Click verifier异常的原agent_completed记录另存，按明确失败人工完成记录；未重发模型请求。
summary.json的infrastructure_failures字段仅捕获运行异常，自动值0不能表示无基础设施失败；
准确分类另存failure-audit.json并在本报告列出，原summary与record不回写纠正。

C最后一题的Agent状态为interrupted/token_budget_exceeded，但对应refusal.json明确为
fee or provider context exhausted、sent=false；实际输入1051978，低于兼容125万，
字节保守预留无法容纳下一请求。不是旧350k门槛触发，也没有未知响应。
B pytest #10081达到52个请求、累计输入超过100万后费用停止，证明扩大额度实际生效。

付费结束后修复评测B的共享保护集，正常结束及预算停止两条路径均用真实pytest生成
二进制临时文件验证，2项通过。这个修复供后续实验，不改变09f97556冻结实现和本轮结果。
未对污染补丁进行额外成功判定，因此本轮不适合宣称纯模型能力的完整比较。

### 原始证据与交接

原始目录：`runs/bc-capability-20261006`。protocol.json/published-protocol.json为冻结协议及
便携身份摘要，provider保存345组请求响应，requests.json账本SHA256为
`a1198abdbad56f56e7b704cfe3bb3de0e8a0f3d251260b24f9828f654973396d`。
summary.json/report.md、per-run.csv、per-task.csv及failure-audit.json来自原始记录。
first-pair-audit.json、paid-audit.json、independent-reentry.json记录审计；独立进程复用20条
记录、供应商新增请求0、账本哈希不变。1768份资格证据的哈希复核通过，历史212请求账本
哈希也不变。HTTP服务核对所属PID与命令行后清理，cleanup证据另存。

结论限定于已知十题、指定模型、静态材料、每题一次及费用约束下的探索实测。
未测长期记忆、连续对话或Docker恢复收益，也不能称为全面能力测定。
