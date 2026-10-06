# D 盘空间方案（2026-09-26）

初始盘点为只读。随后用户要求将选定 run 目录迁到 E；四目录现已复制、逐文件 SHA-256 验证、建立 D 路径 junction 并移除 D 重复副本。D、E 均为本机固定 NTFS 盘。初始 D 可用约 14.4 GiB，E 约 145.4 GiB；`D:\Tracefix\runs` 可读取文件约 93.07 GiB、311 万个，1840 个子目录访问拒绝，故这是下界。迁移结果与哈希见 `runs/docker-foundation-20260926-v1/space-migration-final.json`，逐目录初始统计见 `space-inventory-runs.json`。

## 为什么原清理清单很小

原 `docker-migration-cleanup-candidates.md` 仅覆盖三个容器验收题目的源码副本和 Windows 虚拟环境，并采用“旧入口不依赖、无独有证据、可恢复”标准，约 354 MiB 也仍未达到当前可删条件。它不是全项目空间盘点。主要容量来自每轮实验留下的 `agent-runs/workspace` 与独立验收 checkout；轨迹、补丁、评分和账本与这些目录混放，不能整目录当缓存删除。

| 目录组 | 可读取大小 | 处理原则 |
| --- | ---: | --- |
| `runs/ablation*`（31 个） | 23.44 GiB | 历史开发实验；优先考虑搬迁，保留证据。前三个单目录均约 7.6–7.8 GiB，但有访问拒绝，先不作为第一批。 |
| `runs/p2-agent-simulation*`（7 个） | 22.25 GiB | 多轮模拟与工作副本；优先选择无访问拒绝的旧目录验证迁移。 |
| `runs/holdout*`（37 个） | 12.04 GiB | 留出题封存；本轮不移动、不清理。 |
| `runs/evidence-rescue-20260926-v1` | 3.72 GiB | 新近派生验收证据；本轮保留。 |
| 其他 `runs` | 31.62 GiB | 先分开历史证据与可重建工作副本。 |

Docker Desktop 当前数据文件为 `D:\dockerspace\DockerDesktopWSL\disk\docker_data.vhdx`，约 4.37 GiB，**在项目目录之外，但也占 D 盘**。

## 推荐顺序与本次结果

1. **第一批历史模拟已迁到 E。** `E:\TraceFixRunsArchive` 保存四个目录；每个目录的路径集合、目录集合、文件大小与 SHA-256 均匹配，合计 464,618 个文件、15,639,884,240 字节（约 14.56 GiB），零差异。D 下原路径现在是指向 E 的 NTFS junction；`summary.json`、`protocol.json` 及样本 Git checkout 可从旧路径读取。D 盘对应重复副本已移除。
2. **Docker 数据盘仍在 D。** 当前约 4.37 GiB。需要进一步腾空间时，使用 Docker Desktop 的 `Settings → Resources → Advanced → Disk image location` 迁到 E 新目录，由 Docker Desktop 搬迁；不要运行中手动移动 VHDX。迁移前后核对镜像 ID 与三题离线验收入口。官方[WSL 2 指南](https://docs.docker.com/desktop/features/wsl/)和[设置说明](https://docs.docker.com/desktop/settings-and-maintenance/settings/)记录了支持的设置入口。
3. **后续新 run 输出指定 E。** TraceFix 多个命令默认仍写相对路径 `runs/`，即 D。下一次运行前显式指定 E 上新目录或先实现统一输出根配置；不要假定这次四个 junction 会改变其他 run 的默认写入位置。
4. **第二批先评估，再迁移。** 消融目录约 23.44 GiB，多份模拟目录约 22.25 GiB；其中包含审计、测试环境与独立 workspace，且一些目录有访问拒绝。先确认报告引用和复现依赖，按完整目录迁移并哈希复核。留出题仍封存，不在本轮移动或清理。

切换后 D 可用空间为 31,601,991,680 字节（约 29.43 GiB），E 为 139,709,583,360 字节（约 130.12 GiB）。只读盘点测得项目中 `runs` 仍约 78.5 GiB 位于 D。若把剩余 runs 和 Docker 当前约 4.37 GiB 数据一并迁到 E，按逻辑大小估算 E 还剩约 47.2 GiB；实际可用空间还会受文件系统分配与 Docker 后续增长影响。建议按需迁移经过审查的旧记录，并把未来新输出放到 E。
