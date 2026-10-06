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

当前尚无本轮付费结果；完成状态以本文件后续正式证据为准。

稳定源码定向复验42项通过（tmp/bc-profiles-faults-04），旧协议定向20项通过
（tmp/bc-legacy-tests-final-02）。tmp/bc-offline-20-real-01使用真实官方计数完成20次离线
三步工具/文件与pytest闭环，20次均正常结束且独立通过，所有计数差异为零，供应商调用零。
另一进程复用20条记录，账本SHA256保持
e01cbbe4e919eb5881f4c70d22ff3328f0b5cdd1b86848d5ed01168803981bb6。
editable及仓库外wheel命令/报告通过；wheel SHA256为
ca9eb0c9b32add210672077fa94d6178e9c61c8f8d0a37c8132d0e133069275d。
价格原始页面于2026-10-06T05:36:21Z重新保存，仍为高峰2/8元每百万Token。
官方模型列表也复核1048576窗口与393216最大输出；原始页面保存在tmp/bc-capability-source.html。
