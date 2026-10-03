# 2026-10-04：经验记忆、连续对话与 Docker 恢复

用户明确批准实施 G1→G2→G3→G4；此顺序取代此前仅做读取可见性调查的优先级。
基线为 `b5db1f44bd39ad6f11efa74fd3725651e48c369e`。新功能默认关闭，付费调用为零；
不复用旧调用额度，不访问留出集，不修改历史结果或账本。

## 子 Goal 与提交门槛

- [x] G1：自动形成、维护并跨进程复用仓库级经验 Skills。
- [ ] G2：CLI 与 Python API 连续对话、独立轮次记录及累计预算。
- [ ] G3：ordinary Docker 完整批次快照与原容器删除后的新容器恢复。
- [ ] G4：多轮→经验→新会话→Docker 中断恢复→独立验收的联动链路。

每阶段本地端到端与全量工程门槛通过后推送候选分支；精确提交 Windows 3.11/3.12、
90% 精确综合覆盖率、Ruff/compileall/Diff、editable/wheel 与 Linux Docker/MCP CI 通过后，
才创建该阶段正式 PR。候选提交不等于已交付；任何必需检查缺失时该 Goal 保持未完成。
运行目录、模型消息、密钥和历史未跟踪材料不提交。

## 已确定的边界

记忆仅覆盖经验 Skills，证据资格通过后自动启用；CLI 在轮次之间追加消息，不接受运行中插入。
经验提炼使用同一模型适配器及累计预算，每轮最多一次；未知结果不自动重发。
存储按源仓库隔离，新会话按需检索，活跃会话固定经验版本。候选、去重、失效、冲突、
停用与回滚均须有测试。初版不做用户偏好或实体关系库。
Docker 首批只覆盖普通仓库 profile；原镜像、基线、产品快照和执行状态绑定批次身份。
结果未知的模型或修改工具调用阻断恢复。

## 当前验证

### G1 正式交付证据

精确提交 `773ec7abbf012592c16fd95c01141f2629370fc1` 已完成所有门槛，
正式 PR [#17](https://github.com/wingpeng30/Tracefix/pull/17) 已创建，尚未合并。
精确 push CI [#98](https://github.com/wingpeng30/Tracefix/actions/runs/37141279902)
五项全部通过：Windows Python 3.11/3.12 各 895 tests，无失败、错误或跳过，
精确综合覆盖率 14247/15799 = 90.17659345528197%；三个 Linux Docker/MCP 门槛通过。
本地冻结 checkout 895 tests 全通过，覆盖率 14260/15799 = 90.25887714412305%，
Ruff、compileall、Diff 均退出 0。证据位于 `tmp/memory-engineering-frozen-20261004-03`。
独立 editable 与仓库外 wheel 实际命令验收通过，跨进程学习、召回、真实加载、工具执行、
独立补丁验证、停用及回滚均通过，供应商调用为零。
最终 wheel SHA256 `f90bf0d4f049d8840b277cc04a9f5c5ff52b7b43e5470c6a3298fbc176f17ed0`。
原始 CI run/jobs/artifacts 记录位于 `tmp/memory-ci-98-final-20261004.json`。
以下早期候选失败记录保留为历史，不作为交付证据。
G2 从该精确提交继续，分支 `codex/continuous-dialogue`，尚未完成验证。

### G2 实现与候选检查

新增显式连续会话格式、chat/continue CLI 与 continue_turn API，保留单轮中断恢复入口。
共享历史、工作区、累计预算和固定经验快照，各轮独立归档补丁、轨迹、验证与结果，
新轮重置探索及结束验证；每轮经验请求使用独立幂等收据。
已完成三轮真实修改、pytest、校正要求、独立进程重新进入的定向测试。
稳定定向回归 78 passed，退出码 0，JUnit `tmp/dialogue-targeted-frozen-01.xml`；
补充报告和进程内 CLI 分支后，连续对话专项 7 passed，退出码 0，
JUnit `tmp/dialogue-final-targeted-01.xml`。Ruff 与 staged Diff 检查通过。
尚未通过 G2 冻结提交全量工程检查和精确 CI，不创建 G2 正式 PR。

早期定向失败保留：缺少模拟费用字段导致会计不完整而拒绝继续；CLI 未指定离线模型
在请求前被配置保护拒绝；Windows GBK 输出不能编码费用符号，验收进程改用 UTF-8；
独立验证缺少源码导入字段被拒绝。检查还发现编辑中的实现身份变化正确阻断跨进程继续，
以及记忆函数新增参数不兼容旧替身、旧会话局部变量未初始化，均修复后从稳定代码复验。
此前失败不记为通过，不放宽预算或身份约束。

冻结候选 `960fe6eea43f57d7543f84cf92232b0b7f4dcc5b` 全量 902 tests，898 passed、
4 failed、无跳过；覆盖率 14538/16114 = 90.21968474618345%。Ruff/compileall/Diff 均退出 0。
失败为四个新增 API 测试未显式指定离线模型，在没有本地 .env 的干净 checkout 中
触发请求前凭据拒绝；修正测试配置，不修改生产保护。
原始证据 `tmp/dialogue-engineering-frozen-20261004-01`，该候选不推送。
仓库外 wheel 与独立 editable 三进程验收已通过，零供应商调用，原始汇总及哈希
`tmp/dialogue-packaging-20261004-01.json`，wheel SHA256
`cce9e26cff802a4b6c301559a7902160c76d30e864d1d6772f40054d58a7ae2f`。
本机启动已有 Docker Desktop 后，真实 Linux ordinary 运行及另一个独立容器验证通过，
镜像 ID `sha256:49b75da8b9ba8ffb762e0995422817381db39c379973c8c777d54f2e4e724869`。
运行目录 `tmp/dialogue-docker-ordinary-20261004-01`，独立验证日志
`tmp/dialogue-docker-ordinary-independent-20261004-01.log`；本轮所属容器已清理。

2026-10-04 开始 G1 实现，功能分支 `codex/experience-memory`；尚未通过交付门槛。
本机普通 Python 位于 Anaconda，工程检查采用仓库 `.venv` 的锁定环境。
Docker 本机 daemon 的可用性以执行时探针为准；真实容器验收必须由 Linux 实测证明。
实施与离线注入模型的控制流测试不证明真实模型经验提炼质量或修复成功率。

### G1 本地候选记录

实现仓库隔离的原子经验索引、不可变版本、证据资格、固定版本 Skills 快照、最多三个确定性候选、
一次预算内提炼及持久化请求收据，另提供 list/show/disable/rollback CLI。
自动激活步骤仅允许有实际工具证据的固定操作模板；模型自由表述作为候选保留，不能凭自报信心启用。
证据失效时暂停，冲突不覆盖，候选可在后续取得有效证据时新增已验证版本。

最终定向测试：42 passed，退出码 0，运行目录 `tmp/memory-final-tests-20261004-03`。
关闭功能的既有 runtime/CLI/recovery/checkpoint 定向回归：97 passed，退出码 0。
Ruff 和工作区 Diff 检查通过。wheel 构建日志 `tmp/memory-wheel-build-20261004-05.log`；
安装包 `tmp/memory-wheel-20261004-05/tracefix_agent-0.8.4-py3-none-any.whl`。
仓库外 wheel smoke 位于 `%TEMP%/tracefix-memory-wheel-20261004-02`：供应商调用为零，
学习 PID 38068、召回 PID 4044，两次补丁均独立验收通过，停用和回滚通过。
原始配置、轨迹、结果、退出码与产物摘要由 replay 输出保存；计量为模拟值。

此前全量检查在临时目录权限问题及实现编辑期间运行，不能作为精确版本验收。
将使用冻结候选的独立 checkout 验收；全量覆盖率、Windows 双版本、editable、Linux Docker/MCP
及精确提交 CI 尚未通过，G1 保持未完成，未创建正式 PR。

候选 `50260a5e86f5295110e7970601777e0b94000a21` 的独立 checkout 检查：
892 tests，889 passed、2 failed、1 skipped；精确覆盖率 14161/15787 = 89.70038639386837%，
未过门槛。原始日志、JUnit、覆盖率及哈希位于 `tmp/memory-engineering-frozen-20261004-01`。
失败源于缺少既有 Docker TLS 锁定依赖和运行中补齐依赖造成的环境身份变化；不修改测试断言。
已按原工程锁文件的哈希安装 cryptography 48.0.0 / cffi 2.1.1 / pycparser 3.0。
补齐依赖后的 TLS 与记忆定向测试 46 passed；模拟实验失败单独重跑 1 passed。
新增在进程内运行真实记录适配器和独立验证器的闭环测试，测量子进程未计入的产品路径；
同时保留跨进程测试。增加主动停用不能被自动版本更新覆盖的维护测试，1 passed。
将固定新候选并在依赖不变的环境重新运行完整检查，不降低 90% 门槛。

候选 `2aceba7` 的仓库外 editable 与 wheel 记忆闭环均通过，已安装 wheel 的 memory list 命令通过；
产物为 `tmp/memory-wheel-20261004-06`，日志 `tmp/memory-editable-smoke-20261004-01.log`、
`tmp/memory-wheel-smoke-20261004-06.log` 和 `tmp/memory-wheel-cli-20261004-06.log`。
审查补齐索引容量保护：16 MiB 上限在替换前检查，超限保留旧索引，不写入之后无法读取的记录。
容量与原子写入损坏定向测试 2 passed，退出码 0，目录 `tmp/memory-capacity-tests-20261004-03`。
