# Docker 迁移后的 D 盘副本清理候选（2026-09-26）

判定时间：2026-09-26 16:31（北京时间）。

本文件只列目录，不执行删除。判定基于本轮三道开发题的只读盘点及从 Git 存档独立恢复的容器验收。**当前可删除：暂无。** 现有 CLI、P2 协议默认值和旧复验脚本仍指向原 Windows checkout/虚拟环境；在这些入口完成切换前，移除目录会破坏可复跑性。任务原始工件、旧评分、轨迹、账本、补丁和审计日志都不在清理范围。

| 状态 | 绝对路径 | 用途与逻辑大小 | 替代证据及剩余障碍 |
| --- | --- | ---: | --- |
| 满足条件后可删除 | `D:\Tracefix\runs\real-candidate-validation-v080b\pytest-dev__pytest-10081` | 固定源码，53,967,210 字节 | `runs/docker-foundation-20260926-v1/inputs-v2/pytest-dev__pytest-10081/source.bundle`；新容器 `replays/pytest-dev__pytest-10081/run-2` 脱离原目录复验通过。旧 CLI/P2/复验入口仍引用该源码根。 |
| 满足条件后可删除 | `D:\Tracefix\runs\real-candidate-validation-v080b\psf__requests-1766` | 固定源码，17,788,316 字节 | 对应 `inputs-v2/psf__requests-1766/source.bundle`；`replays/psf__requests-1766/run-2` 通过。旧入口仍引用源码根。 |
| 满足条件后可删除 | `D:\Tracefix\runs\real-candidate-validation-v080b\sphinx-doc__sphinx-10449` | 固定源码，148,255,403 字节 | 对应 `inputs-v2/sphinx-doc__sphinx-10449/source.bundle`；`replays/sphinx-doc__sphinx-10449/run-1` 通过。旧入口仍引用源码根。 |
| 满足条件后可删除 | `D:\Tracefix\runs\p1-revalidation-20260917\environments\pytest-dev__pytest-10081` | Windows 测试环境，28,357,557 字节 | 容器已有 Python/依赖身份；旧 CLI/P2/复验入口仍引用环境根。 |
| 满足条件后可删除 | `D:\Tracefix\runs\p1-revalidation-20260917\environments\psf__requests-1766` | Windows 测试环境，28,270,667 字节 | 容器已分离 Python 3.11 控制环境与 Python 3.9 测试环境；旧入口仍引用环境根。 |
| 满足条件后可删除 | `D:\Tracefix\runs\p1-revalidation-20260917\environments\sphinx-doc__sphinx-10449` | Windows 测试环境，94,463,593 字节 | 容器依赖和重复验收已记录；旧入口仍引用环境根。 |
| 必须保留 | `D:\Tracefix\runs\no-effect-recovery-paid-development-20260926-v1` | 原始 48 项实验、轨迹、补丁和验收 | 冻结证据，不是可清理的源码副本。 |
| 必须保留 | `D:\Tracefix\runs\evidence-rescue-20260926-v1` | 派生旧补丁与复验证据 | 包含新旧结果关联，不能整体移除。 |
| 必须保留 | `D:\Tracefix\runs\docker-foundation-20260926-v1\inputs-v2` | 三份恢复存档、题目工件与哈希清单 | 删除原 checkout 前必须保持可用，并重新核验清单。 |
| 必须保留 | `D:\dockerspace\DockerDesktopWSL\disk` | Docker Linux 镜像与运行数据 | Docker 数据盘，不属于文件夹副本清理对象。 |

三个源码目录合计 220,010,929 字节（约 210 MiB）；三个 Windows 环境合计 151,091,817 字节（约 144 MiB）。这约 354 MiB **全部仍是有条件候选，不是当前可释放空间**。三份新 Git 存档合计约 159 MiB，镜像与构建缓存也占用 D 盘。当前 Docker VHDX 文件为 4,700,766,208 字节；`docker system df` 显示镜像约 1.114 GB、构建缓存约 901 MB，两种统计口径不可直接相加。没有迁移前同口径的 VHDX 快照，因此不能把磁盘余量变化全部归因于本轮。

每个原源码 checkout 的 `git status --porcelain --untracked-files=all` 为空，bundle 含所需提交和标签；但仓库中还存在被 Git 忽略的 `build/`、egg-info、`__pycache__` 或版本生成文件。它们未作为历史实验结果归档，也未逐文件证明可丢弃。因此即使旧入口改接容器，仍需在用户手工清理前核对这些文件、Git worktree 关系和运行中进程，并重新确认恢复验收可以在原目录不可访问时完成。本轮未检查留出题目录，不列入候选。
