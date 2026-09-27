# 2026-09-26 冻结 48 项证据抢救与计数器资格

## 身份与边界

- 原正式批次代码 `b435368398d641e54613cc91bb701487eeafbddd`；本次工程基底 HEAD `5258dc44e324626cc9ab5345d5f7b68cc5d47613`，加工作区补丁和新增文件。完整文件哈希、工作区补丁及 244 份冻结输入见 `runs/evidence-rescue-20260926-v1/source-manifest.json` 与同目录 `working-tree.patch`。
- 原实验 `runs/no-effect-recovery-paid-development-20260926-v1/experiment`、共享账本 `runs/p2-formal-campaign-20260921/cost-ledger.json`、旧校准输入均只读。原评分、协议和账本未覆盖；20 道留出题未运行。
- 本轮模型请求 0，正式实验 0。复验只调用已有严格验收器，用已保存补丁；新输出位于 `runs/evidence-rescue-20260926-v1`。

## 运行条件

Requests-1766 在本地 httpbin 服务健康且复验子进程清除失效的 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 后，base 两次按预期失败、gold 两次通过。原环境中的代理地址 `127.0.0.1:9` 会使本地请求错误路由。

pytest-10081、Sphinx-10435、Sphinx-10449 在受限执行身份下各重复两次，仍出现权限错误或报告缺失；同一宿主、同一用户、受控完整权限执行后，每题 base/gold 两轮均合格，节点集合、审计、导入身份和依赖指纹一致。没有改全局 ACL。具体运行身份、服务和报告哈希见 `execution-profiles.json`。这证明复验环境可用，也说明原批次的 Agent 测试反馈并未因此得到修复。

## 保存补丁的派生结果

逐位置表在 `summary.md` 和 `summary.json`。对原 20 个证据问题，逐文件确认历史运行器审计产物，再生成产品补丁；没有按 `.tracefix*` 前缀过滤。四份历史补丁（13、14、30、45）无产品改动。其余 16 份在合格环境中各复验两次：14 份两次通过，29、46 两份两次出现相同目标断言失败。Requests 六份、pytest-10081 两份、Sphinx-10449 六份均属于前述通过；Sphinx-10435 两份失败、四份无产品改动。

原 48 项分类继续是成功 12、修复未通过 15、证据问题 20、操作中断 1。其余 28 项仅核对保存哈希，标为历史证据。新结果是**保存补丁在恢复环境下的行为**，不能改写当时 Agent 收到的错误反馈，不能直接计算成同期干净环境的 Agent 成功率。

## 官方 Token 计数资格

当日[官方模型列表](https://api-docs.deepseek.com/api/list-models/)仍把 `deepseek-flash` 列为 V4.1 Flash，总窗口 1,048,576 Token，最大输出 393,216。[官方 `deepseek-recipe`](https://github.com/deepseek-ai/deepseek-recipe) 固定在提交 `8cadfede7063c896b944e7bae05daa3549ae97ea`、包版本 0.1.1；V4.1 tokenizer 经规范换行后 SHA-256 为 `81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99`。转换模板与编码实现的哈希见 `tokenizer-qualification.json`。

已在独立 Windows Python 3.12 虚拟环境尝试从 PyPI 安装官方 0.1.1 包。PyPI 只发布 macOS/Linux 轮子，没有 Windows 轮子或源码包，本机也无 Rust `cargo`，因此官方模板/编码无法在当前环境执行。扩展的只读诊断保存 793 个完整请求结构哈希；其中 755 个有供应商 usage、37 个未发送。官方计数状态全部为 `unavailable`，**没有计算出本地计数与供应商 usage 的差值**。旧字节估算与 usage 的 3.72 倍中位差仅是历史诊断，不能转换成硬边界。新正式请求仍在计数资格处停机。

## 工程检查与限制

跨模块审查发现运行准备失败后才登记 `.tracefix-build-tmp`，导致失败路径可能把内部文件导出为产品补丁；已提前登记并增加回归。新增离线复验入口、48 项表和计数诊断扩展，均不创建模型客户端。完成态恢复路径未改动；复验入口的已完成任务恢复检查复用原执行记录，未再启动测试。

全量 pytest 为 **477 passed，0 failed**（655.61 秒）。全项目语句及分支综合覆盖率 **85.74%**，低于仓库配置的 90% 门槛，故覆盖率命令退出 1；这不是五模块定向覆盖率，也不宣称工程门槛全部通过。随后新增的离线复验摘要回归 2 passed、计数范围回归 4 passed。Ruff、compileall 与 diff 检查通过。覆盖率缺口主要来自既有大模块以及当前无法运行官方实现的计数路径；本轮不混入旧轨迹诊断抬高数字。

下一步仅需在可用 Linux 隔离环境安装同一官方包并离线比较 755 个请求；只有计数契约和版本身份可信，才能让长上下文请求通过硬边界。外部基线与付费实验继续冻结。
