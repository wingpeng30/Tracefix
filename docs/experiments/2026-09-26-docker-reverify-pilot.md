# 2026-09-26 Docker 独立验收试点

## 范围与身份

本轮只重放 `pytest-dev__pytest-10081` 的固定 base、gold 和已生成的第 26 份产品补丁；没有模型请求、正式实验或留出题。原批次协议 SHA-256 为 `a98782c1dd266ed8658513332fd718b15dabe6bbd3bf186385e59b394fae0a15`。旧评分和账本未写入。TraceFix 当前工作区含尚未提交的改动，不能用 HEAD 单独代表执行版本；试点代码为本记录对应的工作区文件。

Docker Desktop 4.92.0 的 Linux 引擎可用。试点镜像由 [`docker/reverify-pilot/Dockerfile`](../../docker/reverify-pilot/Dockerfile) 构建，基础镜像固定为 `python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e`；本次生成的镜像 ID 为 `sha256:2cc93cb74dd96de3179e8320b5b99049fdef0545ddfcad97d388ccaf34fb646a`，本地大小约 365 MB。精确的 Python 包清单、OS 信息、源码提交、配方和补丁哈希见 [`pilot-result.json`](../../runs/docker-reverify-pilot-20260926-v3/pilot-result.json)。既有任务配方声明 Windows 平台；这次是独立的 Linux 试点，没有改动原配方或声称与历史环境相同。

## 执行与结果

容器以 `--network none` 运行。整个 `D:\Tracefix` 只读挂到 `/input`；实际测试 checkout 位于容器 Linux 文件系统 `/work`；审计日志复制到新建的 `runs/docker-reverify-pilot-20260926-v3/evidence`，摘要写在同目录。输入补丁与协议的执行后 SHA-256 与冻结记录一致。

| 变体 | 第一次 | 第二次 |
| --- | --- | --- |
| base | 目标断言失败，资格成立 | 目标断言失败，资格成立 |
| gold | 通过，审计完整 | 通过，审计完整 |
| 第 26 份产品补丁 | 通过，严格验收合格 | 通过，严格验收合格 |

执行入口为 [`scripts/docker_reverify_pilot.py`](../../scripts/docker_reverify_pilot.py)。它复用 TraceFix 的 `validate_real_task_behavior` 与 `validate_agent_patch_strict`，并在任何一轮不合格时停止。容器每次运行都应指定全新的 `/output` 目录；原始输入始终只读。复制回来的证据目录与摘要中的容器路径按同名试次对应；容器内 checkout 本身是临时的，未冒充可在 Windows 直接执行的目录。

在 PowerShell 中从 `D:\Tracefix` 运行，已配置 Docker PATH 时可复现该入口（把 `v4` 换成新的目录名）：

```powershell
docker build --pull=false -t tracefix/reverify-pytest10081:pilot -f docker/reverify-pilot/Dockerfile docker/reverify-pilot
New-Item -ItemType Directory runs/docker-reverify-pilot-20260926-v4
docker run --rm --network none --security-opt no-new-privileges `
  --mount 'type=bind,source=D:\Tracefix,target=/input,readonly' `
  --mount 'type=bind,source=D:\Tracefix\runs\docker-reverify-pilot-20260926-v4,target=/output' `
  tracefix/reverify-pytest10081:pilot python scripts/docker_reverify_pilot.py
```

前两次试跑另存于 `runs/docker-reverify-pilot-20260926-v1` 和 `v2`，均未计为合格：第一次发现 CRLF 测试补丁无法直接应用到 Linux checkout；第二次发现旧 pytest 在 Windows 挂载的 checkout 上捕获文件报 `FileNotFoundError`。已让 base/gold 调用已有的行尾规范化补丁应用入口，并改为在 Linux 文件系统执行测试。这些失败及修正均有独立输出，未覆盖旧证据。

## 限制与下一步

这一题证明了保存补丁可在容器内重复严格验收；它不证明原 Agent 当时在容器中会得到同样反馈，也不等于全部 48 项已经迁移。当前镜像只适用于这个试点；pip 的传递依赖已由本次镜像 ID 和 `pip_freeze` 固定，但 Dockerfile 尚未提供可重建的完整锁文件。下一步应把路径映射、每题镜像/依赖锁定、离线证据复制和结果比较抽成通用入口，再选 Requests 与 Sphinx 各一题验证服务、代理和构建差异。未通过这些跨项目试点前，不扩大到全部任务，更不启动新的付费比较。
