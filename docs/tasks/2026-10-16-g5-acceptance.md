# G5 后台测试作业：阶段验收

G5阶段门已关闭。长测试可以后台运行，Agent可以查询、分段读取日志和取消；
正常暂停后能在新进程、新容器取回旧日志，执行结果未知时不会自动重跑。
G6—G8不属于本次验收；下一阶段按已批准计划从G6.1规则模型和安全读取契约开始。

## 验收身份

- 实现提交：`c02c79dc65eb62f7723fe0a8082ff1407788c493`，分支`codex/harness-runtime`。
- Git tree：`ef2f5c3801d8b4a16aea5ff7a6244051b8434921`，810个跟踪文件。
- 工程文件清单320项：`runs/harness-runtime-start/g5-isolated-fixture-source-manifest.json`，
  SHA256 `41c036d7f0f3cd29061e4dcbf13d2dbf1ca8c56b0bd29c4e06b4eda6bfd20402`。
- [CI169](https://github.com/wingpeng30/Tracefix/actions/runs/38827878945)五个job全部成功。
- [Linux专用验收](https://github.com/wingpeng30/Tracefix/actions/runs/38827878960)成功，同一提交。
- 后续文档提交只记录上述实现；不能将新HEAD说成已经重新完成工程检查。

## 工程门与平台证据

| 检查 | 实际结果 |
|---|---|
| Windows Python3.11.9全量 | 2017 passed，0 failed/error，2项Linux平台skip |
| 3.11精确行分支综合覆盖率 | `27429/30438 = 90.11433077074709%` |
| Windows Python3.12.10全量 | 2017 passed，0 failed/error，2项Linux平台skip |
| 3.12精确行分支综合覆盖率 | `27426/30438 = 90.10447466982062%` |
| 原范围Ruff、compileall、diff | 两版当前提交CI通过，未减少文件范围或降低门槛 |
| 仓库外editable、独立wheel及公开资格 | 两版quality job全部通过 |
| 基础Docker、ordinary Docker、Serena | 三个真实job成功；ordinary含两种安装方式的正式Runner与组件回放 |
| Linux原生与故障契约 | 67 passed，7项Windows平台skip |

已逐项核对JUnit：Windows跳过的两个cgroup测试在本提交Linux实际通过；
Linux跳过的七个Windows原生测试在本提交两版Windows均实际通过。
互补检查保存在`ci169-platform-complement-proof.json`，不是把环境不可用直接记为通过。

本机Python3.11.16/3.12.5都导入候选工作树。G5修复后定向各286 passed/2平台skip，
独立Git夹具修复后的仓库外扩展范围各72 passed/2平台skip；这些是重叠子集，不能相加。
本机两次完整检查均已中止，不能算全量通过；完整阶段门以本次CI169为准。

## 已交付的实现

1. `start_test_job`、`get_job_status`、`read_job_output`、`cancel_job`四工具在会话启动时固定。
2. 同步和后台共享不可变PreparedTest、白名单、最小环境、审计/JUnit及源码判定。
3. Windows Job Object先归属再放行；Linux local使用已委派cgroup v2；ordinary容器是最终清理边界。
4. stdout/stderr持续排空，保存有界raw与脱敏view；UTF-8字符游标、跨块秘密和坏字节有契约覆盖。
5. 宿主持有唯一测试执行账本；worker只有单次票据，不能分配额度。明确未启动才释放，未知不退款、不重放。
6. bridge主循环在测试运行时仍能处理查询/读取/取消，worker不向协议stdout插入消息。
7. 主线程在完整工具批次后消费结果；TaskState只投影证据，不另建预算或完成权威。
8. 活动作业仅写不可恢复的进度收据；正常停止先取消、确认清空、封存和消费，再提交安全checkpoint。
9. 恢复核对收据、执行账本、历史启动调用、ArtifactRef与旧worker身份；不接管旧执行或重放来源工具。

领域模型、服务和端口没有导入Runner、MinimalAgent或DockerBackend；运行时通过明确接口装配。
ContextManager只保留消息中的作业/产物引用，不读取作业或产物存储。
默认关闭时沿用旧工具schema、消息顺序和执行路径；本阶段没有后台常驻服务或任意shell API。
G8共享模型预算和子代理尚未实现，本阶段共享的是根测试计次与既有产物额度。

## 关键真实回放

ordinary的wheel和editable都完成：长测试输出100KB中段→增量读取→取消并确认树退出→
第二个测试自然结束→安全暂停→删除旧容器→新Python进程/容器恢复→取回旧日志，累计测试仍为2。
独立baseline与两个最终目标恰好计3次，最终验证通过。历史读取继续消耗原G2配额，没有恢复重置。

实际创建逃离进程组的后代时，无法证明清理则标为unknown、计1次、不重放/退款，
整个自有容器被删除；日志中的“1 passed”不构成通过证据。
本机17a0生产源码的相同回放及四个明确自有容器缺失检查也已归档；最终安装包证明使用本次c02 CI。

Windows真实宿主硬崩溃回放证明已持有句柄的测试/子进程退出，但恢复仍保留unknown；
不能因为外部看见进程消失或日志passed就修复资格。预算拒绝和未知状态会阻止后续模型请求。
全部回放使用脚本模型或受控替身，供应商调用为0；不代表真实模型主成功率或性能收益。

## 发现并修复的问题

- Windows venv启动器可能在父进程归属前创建真实解释器。新增延迟归属的原生测试先证明旧实现失败，
  再改用宿主基础解释器启动仅含stdlib的`-I -S`可信门，配置的测试Python在归属后启动。
  不可用的基础解释器失败关闭。子进程继承边界参考[微软Job Objects说明](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。
- Docker Desktop tmpfs审计复制异常改为有界专用流式传输，验证路径、类型、大小、哈希和稳定身份；
  不能借日志导出失败撤销测试计次或重新执行。
- checkpoint落盘后中断的序号窗口已修复，未放宽未知调用保护。
- 写盘失败仍执行最终owned-container abort；清理错误保留主要失败，重复close不会把失败改成成功。
- CI168的三个新夹具缺少独立Git初始提交，本机误借父仓库HEAD；已建独立仓库并断言仓库根，
  在真实仓库外复验，不修改产品Git校验。
- 长路径令本机venv Python在用户代码前挂起，改用短临时路径的同一12轮契约通过；
  两次中止的完整检查、此前覆盖率不足的CI和身份漂移执行都保留失败/无效归属，不借旧结果关闭本阶段。

详细时间线、失败前后日志和源码归属见[G5实施记录](2026-10-16-g5-progress.md)。

## 原始产物、镜像和协议

原件已下载到候选忽略目录`runs/harness-runtime-start/`并逐个核验GitHub给定SHA256。
没有上传raw runs、凭据或临时签名下载引用；原始JSON/JUnit只作有界读取，未任意解包路径。

| Artifact ID | 内容 | SHA256 |
|---|---|---|
|12016133587|工程3.11|7183ac09a4ce3fd776241c0f5ca4326d7163462dc3890d5ee76d4fbd5799e065|
|12016892922|工程3.12|7dd1ffadbe74f61946b5fd9c34087d7258a7b8c6d4f1f03acacb545ac44a9d5c|
|12015963296|ordinary组件/正式Runner wheel-editable|bb38801fd612b4625126599cd46839c4f33d614238dbe95371994ff9e1baa543|
|12016315629|基础Docker|16ed02fbf6fbd87045e172f20b24386b4ec33563e8a9c14ef4cc4849a734ea2d|
|12016180831|Serena|b3d0e2cd58e1cc7f4a8eccec59a8f1ba62f052dfd39bf13cbd1a87d02332c2d6|
|12015498393|Linux进程域JUnit|7f7c91bf01e8d4164e8b51cdb63dcb39b7c0f3ecbced2fceee749e9d6dc267f1|

ordinary任务镜像`sha256:6fb3cf375469317a93e8228b64f37d7ca38f947b3e8e76e8521d8dd5b1dbbbae`；
该组合Serena镜像`sha256:d1db84d21dd77404410acb96e834b8f96c399952c41fedcf7a0cfdad14b8d7af`。
独立Serena job镜像`sha256:0bb9775a25e3e6730ecff69f91922e76aa63621f616d02c640b1a5df863d0d46`。
bridge协议及作业协议均为1；执行策略配置hash
`9cb3c8996a24dc7fd91159dfb4b5d8c0c14654a23a01c5a720a33778d27544d6`。
原始`ci169-*.log`、`ci169-verified-artifacts.json`、两版工程summary与bounded proof JSON保留对应源码身份。

## 启用、复现与保留限制

使用示例、四工具和本地/Docker条件见[后台作业说明](../background-jobs.md)。
设置`harness_foundations_enabled=true`和`background_jobs_enabled=true`；CLI为`--harness-foundations --background-jobs`。
ordinary还要求recovery；任务网络保持none，缺能力在模型初始化前拒绝，不退回local或自动安装。
Linux local需预先委派cgroup；local首版不支持自定义environment_recipe/测试环境变量与后台组合。

每checkout最多一个活动测试，无队列；活动时允许纯读取、搜索和计划维护，
补丁、第二次测试、最终验证、Diff/源码导出及MCP延期。作业随会话停止，不建设常驻服务。
每产物raw和view各8MiB（均为双流合计）、根存储64MiB/128产物、单页4096字符、
根64次产物查询/累计128Ki字符等限制沿用G2，恢复不重置。

零供应商入口：`scripts/check_job_runner_docker.py`、`scripts/check_job_bridge_docker.py`，
以及Windows原生`tests/test_job_runner.py`、`tests/test_job_crash.py`；需事先准备对应依赖和固定镜像。
本阶段没有付费模型实验，没有合并main或发布。G6按既定顺序从规则格式、纯模型及安全读取开始。
