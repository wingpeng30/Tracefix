# 普通仓库 Docker 零调用集成记录（2026-09-29）

功能执行提交：`f67d41a5e84ea3baf0be925b706772d2947f0469`；PR #5 CI 检出合并提交 `64f0c0e6fcbe11e31ca2eaf8b292ddfd7e100ee5`。[CI #38](https://github.com/wingpeng30/Tracefix/actions/runs/36549319954) 的 `docker-ordinary-zero-call` job 成功，原始 [artifact #11023998128](https://github.com/wingpeng30/Tracefix/actions/runs/36549319954/artifacts/11023998128)，ZIP SHA-256 为 `7daf41cfa12ea8ac48fb909b91836f83e53981f838d3f148398d5fced0407767`。环境为 GitHub Ubuntu 24.04、Docker daemon、宿主 Python 3.11.16；用仓库外 wheel 安装 TraceFix。此记录不使用供应商模型或隐藏留出题。

复现步骤由 `.github/workflows/ci.yml` 的 `docker-ordinary-zero-call` job 固定：安装哈希锁定宿主依赖并从 wheel 安装，执行 `tracefix docker-prepare`，运行两次 `examples/replay_ordinary.py --docker-ordinary-image-id <image_id>`，对第一轮执行 `tracefix doctor --prepare`、`tracefix verify`、`tracefix report`、`tracefix export`，然后执行 `scripts/test_ordinary_docker_failures.py`。脚本模型有 6 次离线响应，真实供应商请求为零。

镜像 ID：`sha256:e77c9511cdf11b70e36a508b422a6015e63b8728519d263e1797e45384607682`；Dockerfile SHA-256：`4208288458710c307040246b6d24681b3e1eefa3dd679937d2d05d9e10323224`；基础锁文件 SHA-256：`acb230774d35ce2e1fd6c14adfb635829414d08ac36fa7e7cf4823e0d33c9489`。镜像解释器 Python 3.11.16，pytest 8.4.2。公开 fixture 源提交 `ad02be2e521cb91916c82e1f397708605b50bfcc`，补丁 SHA-256 `0b7ac0aa8c1d75d306fa2a554fdcdc87c3640ebc1ca9d5d014c66778390feec0`。

两次普通 Docker 运行均为 `completed` 且公开测试状态 `verified`，原始源仓库仍干净。`doctor --prepare` 为 `ok: true`。独立验证在新容器应用保存补丁，对记录的测试目标复跑成功，结果绑定源提交、补丁哈希和同一镜像 ID。故障脚本分别确认测试超时返回 `timed_out: true`、桥接关闭后拒绝工具调用、源码导入缺失阻断准备；这些容器均完成清理。CI 最后检查没有遗留的 `tracefix-` 容器。报告和导出补丁保存在同一 artifact 中。

初始 [CI #34](https://github.com/wingpeng30/Tracefix/actions/runs/36546951977) 暴露只读根文件系统下 `docker cp` 写入失败；改为通过 `docker exec -i` 向 tmpfs 流式传输。[CI #35](https://github.com/wingpeng30/Tracefix/actions/runs/36547468687) 发现镜像默认工作目录在挂载后被 root 创建；改为根目录。[CI #36](https://github.com/wingpeng30/Tracefix/actions/runs/36547875768) 两次运行通过，但独立验证缺少 Docker 配置身份文件；已补 `session.json` 并明确不可 checkpoint。随后 [CI #37](https://github.com/wingpeng30/Tracefix/actions/runs/36548438952) 的真实超时返回 `timed_out: true`，故障断言已修正。上述失败记录保留，不作为成功证据。

此结果证明隔离工具、公开 pytest、补丁和证据流程可运行，不能推断真实模型修复率或跨仓库通用兼容性。Windows Docker Desktop 未实测。复杂构建、外部服务以及目标仓库依赖未按锁文件准备的情形不在首版承诺内。普通 Docker 会话不支持 `resume`；结果未知时不自动重放。Windows 双版本覆盖率门槛应按 PR #5 最终提交的 CI 判定，不能用本 job 代替。
