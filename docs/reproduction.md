# 当前源码的安装与零模型调用复现

本文描述当前提交的公开复现入口。合成流程只验证安装、Agent 控制流、工具桥接和隔离，不证明离线定位质量或真实独立修复成功率提高。历史 G5–G7、Requests TLS 和其他旧运行记录保留作历史证据，不代表当前候选提交已通过本批门槛。

## 哈希锁定安装

支持 Windows x64 Python 3.11/3.12 及 Linux amd64 Python 3.11（容器 smoke）。Windows 3.12 示例：

    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements/locks/build-tools.txt
    .\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements/locks/engineering-py312.txt
    .\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
    .\.venv\Scripts\python.exe scripts/check_engineering.py --output runs/engineering-py312 --python .venv\Scripts\python.exe --diff-base HEAD^

Python 3.11 改用 engineering-py311.txt。工程检查入口会保存 pytest 日志、JUnit、coverage JSON/XML、coverage-gaps.json、每个子进程的返回码和独立验收结论；覆盖率按语句及分支的整数计数判断 90% 门槛。`--diff-base` 必须能在本地 Git 对象中解析；浅克隆需先获取目标提交，入口不会跳过或猜测比较范围。每轮使用全新输出目录和 coverage 数据。文件哈希与依赖许可证摘要见 [dependency-locks.md](dependency-locks.md)。

## 公开本地合成流程

tracefix-reproduce 是包内入口。用固定脚本模型修复独立生成的合成 Git 仓库，并经过正式 Runner、工具、pytest、Diff 和 JSONL 轨迹。宿主 Runner 阻止供应商客户端构造、供应商请求及网络连接；脚本模型请求单独计数。输出路径必须不存在。

    .\.venv\Scripts\tracefix-reproduce.exe --backend local --output runs/reproduction-baseline
    .\.venv\Scripts\tracefix-reproduce.exe --backend local --skills-enabled --output runs/reproduction-skills

两次运行只切换 Skills；检查 reproduction.json、result.json、patch.diff 和 trajectory.jsonl。scripts/reproduce_zero_call.py 是兼容包装，和已安装命令使用同一实现。

## Wheel 安装流程

在 checkout 根目录构建 wheel：

    New-Item -ItemType Directory -Force .artifacts\wheel
    .\.venv\Scripts\python.exe -m pip wheel --no-deps --no-build-isolation . -w .artifacts\wheel

CI 在新虚拟环境里先按锁安装依赖，再以 --no-deps 安装 wheel；验证 tracefix 实际导入自该环境的 site-packages，核对 tracefix.reproduction、tracefix.agent_bridge 和 Skills Markdown 均在 wheel 内，并从 checkout 外运行 baseline 与 Skills-only。此 wheel smoke 不读取仓库目录或开发机 PYTHONPATH。

## 真实 Docker bridge 合成流程

需要 Linux Docker daemon。docker/zero-call-smoke.Dockerfile 固定 Python 3.11.16-bookworm 基础镜像 digest，以哈希锁安装隔离 smoke 依赖；任务镜像含 Git。先构建并读取本次构建的完整镜像 ID，再运行两臂：

    docker build --pull=true -f docker/zero-call-smoke.Dockerfile -t tracefix-zero-call-smoke:local .
    $imageId = docker image inspect --format '{{.Id}}' tracefix-zero-call-smoke:local
    .\.venv\Scripts\tracefix-reproduce.exe --backend docker --image-id $imageId --output runs/docker-baseline
    .\.venv\Scripts\tracefix-reproduce.exe --backend docker --image-id $imageId --skills-enabled --output runs/docker-skills

宿主 TraceFix Runner 编排合成容器，通过 Docker backend、既有 JSONL bridge 与 ToolRegistry 执行；合成 profile 只接受固定任务身份和空环境变量配方，不接受任意 shell。容器使用 --network none，宿主源码、Docker socket、历史题目材料均不挂载。bridge 验证源 commit、包源码和技能目录身份；报告保留镜像 ID、fixture 源 commit、包树哈希、网络/挂载检查、轨迹与结果。CI 运行结束后由 `scripts/audit_docker_smoke.py` 核对两臂身份、供应商零调用和容器已清理。每个输出目录须为新路径。PR、main、手动入口都运行 Linux Docker baseline/Skills 两臂并上传证据。

Docker 构建需要联网取得固定基础镜像与锁内 PyPI wheel；任务运行阶段断网。新派生镜像 ID 由 CI 输出，不能替代或冒充历史镜像身份。当前本机 Docker daemon 未启动，因此本地真实 bridge 尚未验收；以推送提交的 Linux CI smoke 结果为准。

## 文件与材料清单

| 分类 | 文件/材料 | 复现边界 |
|---|---|---|
| 当前运行与发布必需 | src/tracefix/、pyproject.toml、package 内 reproduction.py、agent_bridge.py、skills/ | wheel 内含 bridge 和技能文本；依赖见哈希锁 |
| 检查与安装 | scripts/check_engineering.py、scripts/audit_docker_smoke.py、requirements/locks/、.github/workflows/ci.yml | 工程入口和 CI 使用同一覆盖门槛；Docker 审计核对隔离和清理；外部结果需另存 |
| Docker bridge | docker/zero-call-smoke.Dockerfile、docker/agent-bridge.py | 历史 wrapper 保留；容器调用包内 bridge |
| 历史 recipe | docker/reverify-pilot/Dockerfile、scripts/pytest10081_diagnostic_matrix.py | 历史构建/受控诊断，不属于公开合成 smoke 必需输入 |
| 受控外部材料 | 历史源 checkout、patch、image、隐藏测试、tokenizer 缓存 | 不含在公开 GitHub 流程；按单独授权、哈希和外部获取说明复验 |
| 禁止发布的本地状态 | runs/、临时证据、密钥、受控验收输入、候选留出题 | 不批量 stage，不加入 wheel 或 Docker context |

当前候选的 Windows 双版本门槛、仓库外 wheel smoke 与新增 Docker 清理审计正在通过下一次 GitHub CI 复验；以候选 SHA 对应的 Actions 报告为准。pytest-10081 Python 点版本对照、Requests 历史 TLS 容器复验、Sphinx 基线 recipe 重建及完整 Token 契约不在本批。它们不能由本合成 smoke 推断通过。

## 启停和回退

Skills 默认关闭。baseline 命令不加 --skills-enabled；只需停用时移除该 flag。Docker 也复用相同 Skills 开关；bridge identity 或目录错误时 fail closed。恢复代码可回退本批 Git 提交；保持基线运行时仍可仅关闭 Skills。Context7、GitHub MCP 和 Serena 本批均未接入。

下一步效果比较先用冻结开发集做 baseline 对 Skills-only 单变量对照，固定任务、源码、镜像和预算，并记录 Token 估算、耗时、搜索/读取、重复工具操作、无效补丁与独立验收。没有真实对照和独立资格证据时，只报告工程能力及合成控制流，不报告真实修复成功率提升。
