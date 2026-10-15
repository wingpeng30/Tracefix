# 后台测试作业（G5 候选开发中）

当前候选已接通本地 Runner/API/CLI 的后台 pytest、增量读取、取消和安全暂停恢复。
**G5 尚未完成工程验收。** 候选已接通ordinary Docker正式Runner/inspect/resume、基线产物交接和根预算。
本地真实回放通过取消/自然完成、新进程恢复、最小3次baseline/finalgate及逃离组后代中止整个容器；
5398a7b的CI167正式入口wheel/editable已通过，但双版本综合覆盖率89.80%左右未过90%。
之后又修复Windows venv启动器归属竞态；该新实现仍需重新验收，使用边界以当前状态和G5记录为准。

## 本地启用

新会话配置 `harness_foundations_enabled = true` 和 `background_jobs_enabled = true`，默认均关闭。
CLI 对应 `--harness-foundations --background-jobs`。Windows 使用 Job Object；
Linux 必须通过 `job_cgroup_root`／`--job-cgroup-root` 指定宿主已委派的 cgroup v2，程序不自动提权。
首版沿用现有可恢复本地环境限制：配置了 environment_recipe 或自定义测试环境变量时拒绝启用后台作业。
已经开始的会话不能临时打开开关或增加工具 schema。

```toml
[run]
repo = "./example-project"
task = "修复失败测试，并保留既有行为"
execution_backend = "local"
harness_foundations_enabled = true
background_jobs_enabled = true
```

Python API 使用 `RunConfig(..., execution_backend="local", harness_foundations_enabled=True,
background_jobs_enabled=True)`。实际运行的模型及其预算仍由原配置决定；本轮检查全部使用脚本替身，
没有发起供应商请求。

## 四个工具与限制

| 工具 | 行为 |
| --- | --- |
| `start_test_job(command, timeout_seconds)` | 仅接受既有 pytest 白名单；一个 checkout 最多一个活动测试，不排队 |
| `get_job_status(job_id)` | 查询本会话的 opaque ID；完成只说明执行结束，不授予最终通过资格 |
| `read_job_output(job_id, cursor, limit)` | 读取已提交脱敏视图，每页最多4096字符；保留起点与下一页游标 |
| `cancel_job(job_id)` | 提交取消请求；需查询终态和整树退出确认，重复取消不增加次数 |

后台和同步 `run_tests` 共用准备、审计/JUnit、源码判定与根测试额度；基线和最终门也计入同一账本。
活动作业期间允许纯文件读取、搜索和计划维护；补丁、第二次测试、最终门、Diff/源码导出及 MCP 查询延期。
原始日志仅供审计，模型只能读取脱敏视图；增量与最终产物共用 G2 的64次查询、128Ki字符返回等总额度。
超出展示长度时，可以用 `start_cursor` 缩小该页重新读取，每次仍扣共享查询额度。
磁盘或额度耗尽继续排空进程输出，记录 incomplete，不重跑测试补日志。

## 暂停与恢复

运行中的批次只写不可直接恢复的进度收据，保留上一个安全 checkpoint。
正常停止依次取消、确认进程树退出、封存日志、消费终态，再提交安全 checkpoint。
恢复核对作业回执、执行账本、原始启动调用、产物和 TaskState 引用；新进程不会重新执行历史测试。
启动结果或清理未知时保持 `outcome_unknown`，不退还未确认额度、不发模型请求，也不从旧 checkpoint 重放。
即使日志出现 passed，或外部观察到进程已退出，也不能据此修复未知状态。

## 当前零供应商验证入口

在开发环境中运行 `python -m pytest tests/test_job_runner.py tests/test_job_crash.py -q -o addopts=`。
Windows 用例会建立并清理自己的临时仓库与子进程；其他平台保留实际 skip 原因。
这里覆盖正式本地 Runner 的暂停/新进程恢复、共享基线/最终门及真实宿主硬崩溃。
其中 Serena 组合只验证静态目录身份，明确使用 transport 替身，不是实际 MCP 调用。
ordinary独立wheel/editable正式入口已在CI167通过，底部给出真实Docker复现脚本。
此结果归属5398a7b，不能替之后的进程归属修复关闭完整阶段门。
实际计数、源码身份和限制见[G5进度](tasks/2026-10-16-g5-progress.md)。

## ordinary Docker候选组合

使用已准备的固定镜像，设置`execution_backend="docker"`、`docker_profile="ordinary"`、
`docker_image_id`、`docker_recovery_enabled=true`、`harness_foundations_enabled=true`、
`background_jobs_enabled=true`，并填写`source_import`及`test_target`。
CLI沿用ordinary参数并追加`--harness-foundations --background-jobs`；网络仍为none。
每个回归baseline使用独立新容器，测试额度和产物/读取额度由根会话共享，不为容器复制额度。
如果后代逃离进程组且无法确认清理，作业标为unknown并停止整个自有容器，不报告取消成功。

零供应商正式入口回放（需要Docker及已准备的固定镜像）：

```powershell
python scripts/check_job_runner_docker.py --image-id $imageId --output ./runs/g5-runner-replay
```

该脚本使用本地脚本模型，覆盖长测试中段读取/取消、短测试自然结束、新Python进程恢复、
一个baseline和两个最终目标，以及逃离组后代的unknown/整容器清理。输出目录须尚未存在。
底层协议/固定身份/审计链接负向和产物配额恢复probe为`scripts/check_job_bridge_docker.py`。
这两个入口的通过不能代替全量工程和真实供应商成绩。
