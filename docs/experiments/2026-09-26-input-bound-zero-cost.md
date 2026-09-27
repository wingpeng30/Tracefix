# 2026-09-26 输入边界与测试证据零费用改动

执行前代码 HEAD：`5258dc44e324626cc9ab5345d5f7b68cc5d47613`。本轮只改工程代码、测试和只读诊断；没有供应商调用、新正式实验或留出题评测。原始 48 项结果、评分和账本不变。

## 官方能力与本地资格

DeepSeek [模型列表](https://api-docs.deepseek.com/api/list-models/)将 `deepseek-flash` 列为 V4.1 Flash，总上下文 1,048,576 Token、最大输出 393,216 Token。[Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/)说明输入与输出共享总窗口。当前单请求输出仍为 4,096，故理论输入空间为 1,044,480。此值不是本轮实测吞吐。

新协议默认 `per_request_input_tokens=null`，取消 TraceFix 独立 128k 上限。`standard` 累计输入预算 350,000，`long-context` 为 2,000,000；显式 `max_input_tokens` 优先。费用上限、请求前按输入上界和全额输出预留、响应后按 usage 结算的逻辑保留。旧账本条目可读取，完成态恢复只读、零新调用；未完成的旧协议因代码身份变化仍需单独迁移，不能直接用新默认继续发送。

新请求的计数不可用、模型窗口超限、用户显式单请求上限和每试次累计输入不足分别产生独立停止原因；共享费用及阶段费用不足沿用原有独立原因。旧评分分类未重写。

供应商容量绑定官方端点、供应商和模型身份。新请求只允许 `verified_exact` 或 `verified_upper_bound` 的输入计数进入硬边界。代码包含官方 V4.1 模板/tokenizer 的可选估算入口，限定文本 Chat Completions、关闭 thinking、函数工具调用，拒绝已知特殊标记文本；但当前本机没有已验证的官方计数环境和 API usage 对照契约，因此其状态仍为 `estimate` 或 `unavailable`，**新正式请求在预留费用前拒绝**。不会用历史 3.72 倍比例换算为硬 Token 数。[官方 tokenizer 用法](https://github.com/deepseek-ai/deepseek-recipe/blob/main/docs/tokenizer.md)及[特殊标记差异报告](https://github.com/deepseek-ai/deepseek-recipe/issues/5)是资格核查依据。

## 冻结证据校准

只读入口 `scripts/p2_token_diagnostic.py` 使用冻结轨迹、原账本、响应哈希及供应商 usage；最终报告另存于 `runs/input-token-calibration-20260926-v2/token-calibration.json`，初版诊断报告保留。输入账本 SHA-256 `b811cd09abb6996bf9e0edf97f68c64fe3b63806ab73d80e9f311f0318e6a3ab`，原协议 SHA-256 `a98782c1dd266ed8658513332fd718b15dabe6bbd3bf186385e59b394fae0a15`，最终报告 SHA-256 `d79a0e44d0d07e781f101e31aab00e2938a083a33c96979cec34cc7deb1ffa3f`。

793 个请求视图中，756 个对应账本请求，37 个未发送。756 个旧字节上界均可精确重建，755 个有供应商输入 usage，响应证据哈希及供应商 usage 对账均无差异。旧上界/实际输入的最小值 3.4175、中位数 3.7233、最大值 4.1417。37 个未发送视图无 usage，不能据此断言可发送。这些是离线计数诊断，不是模型修复成功率或新计数器合格证明。

## 测试、补丁与限制

`run_tests` 先做参数、命令和 pytest 配置预检，再检查额度。非法调用单独记事件和次数，同一签名 3 次或全部 6 次停止；成功创建的 pytest 子进程立即记录启动并扣 1 次，测试通过、断言失败、收集失败和超时都只计一次，创建失败不扣。总步数限制保留。

审计 JSON、JUnit、插件与生成配置写入 checkout 外的运行证据目录。checkout 内确需存在的构建及测试临时目录按本次运行显式登记，`get_git_diff`、最终补丁和补丁写入保护共用登记；不依赖 `.git/info/exclude`。仅运行测试的产品 diff 为空，真实产品变更仍导出。目录已有同名路径或证据目录指向 checkout 内会报错。

最终冻结代码的全量 pytest 为 **476 passed，0 failed**；Ruff、compileall 与 `git diff --check` 通过。聚焦覆盖率运行 87 个相关用例通过，最终代码对应的原始 coverage 数据保存在 `runs/input-token-calibration-20260926-v2/coverage-focused-final.data`；仅就 `agent/minimal.py`、`tools/builtin.py`、`p2_protocol.py`、`models/litellm_adapter.py`、`models/input_bounds.py` 五个改动模块计算的合并覆盖率为 **53%**，未达到项目 90% 门槛，不能称为全项目覆盖达标。该覆盖运行刻意没有重复全量的长时模拟，低比例主要反映许多 P2/适配器路径未在这 87 个聚焦用例中执行。原账本与原协议的 SHA-256 在测试后复核均未变化。

CLI 可用 `p2-run --input-budget-profile long-context` 选择 2M 累计输入预算，也可用 `--max-input-tokens` 覆盖累计预算、`--per-request-input-tokens` 额外收紧单请求；这些配置不会绕开计数资格、模型窗口或费用上限。现有 `standard` 默认仍为 350k。当前未完成的旧正式协议不会在新代码身份下自动继续发送请求；完成态只读恢复可用。

本轮没有实测真实 1M 请求。下一步先在独立环境中安装并固定官方 tokenizer/模板，比较既有完整响应（包括工具 schema、多轮、Unicode、特殊标记）与供应商 usage，确立保守上界资格；若无法建立契约，保持停机。之后按项目方向评审顺序，先复验已有补丁和 gold/base、查清 Requests 代理及 pytest 临时权限，再决定是否改用 Linux 容器；证据稳定后才规划同预算外部基线。20 道留出题继续封存。
