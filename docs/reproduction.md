# 当前源码的安装与零模型调用复现

本文区分公开合成控制流、容器复现与受控历史材料。成功运行这些流程只证明工程路径可用，不代表模型离线定位效果或真实独立修复成功率提高。

## 从 checkout 安装

需要 Python 3.11 或 3.12、Git，以及 pip 可访问的包源。基础安装和开发检查：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

若测试 HTTPS 的 Docker backend，再安装 `.[dev,llm,docker]`。`docker` extra 固定
`cryptography==48.0.0`，用于运行时生成临时测试证书；真实产品源码不包含测试私钥。
项目直接运行依赖见 `pyproject.toml`。目前仓库**没有跨平台、哈希锁定的完整传递依赖锁文件**；
所以这段流程可重跑，但尚不能声称 pip 从任意空缓存环境解析出逐字节相同的环境。CI 使用其
配置的 Windows/Python 3.11、3.12 矩阵并执行 pytest-only 90% 覆盖率门槛。

构建和安装 wheel（技能 Markdown 应出现在 wheel 内）：

```powershell
New-Item -ItemType Directory -Force .artifacts\wheel
.\.venv\Scripts\python.exe -m pip wheel --no-deps --no-build-isolation . -w .artifacts\wheel
.\.venv\Scripts\python.exe -m zipfile -l .artifacts\wheel\tracefix_agent-0.8.4-py3-none-any.whl
```

另建干净虚拟环境安装生成的 wheel，再运行下一节的合成 smoke。输出目录必须是新的空目录。
本轮新建独立 wheel `tracefix_agent-0.8.4-py3-none-any.whl`（SHA-256
`d694f0f56e2c0c1309bc2d9c69fc63cd2d27f279ebdc486120f5bca0d322f1b4`），在全新 E 盘 venv
通过 pip 安装，并从安装目录发现 `tracefix-debugging` 1.0.0（技能全文 SHA-256
`2275795589fa8bfc5fbad4f8d1666e2d3af95ad85413921ac788a52d39169551`）。这验证当前 Windows 3.12
wheel 的分发，不提供其他 Python/平台的锁定证明。

## 公开合成闭环

该入口用固定脚本模型替身，经过真实 `TraceFixRunner`、隔离 Git clone、工具注册表、pytest、Diff 和 JSONL 轨迹。替身创建时不加载 LiteLLM；供应商调用计数应为 0。

```powershell
.\.venv\Scripts\python.exe scripts/reproduce_zero_call.py --output runs/reproduction-baseline
.\.venv\Scripts\python.exe scripts/reproduce_zero_call.py --skills-enabled --output runs/reproduction-skills
```

首个输出记录 baseline，第二个只改变 Skills 开关。两边都应完成同一个合成修复并运行一次测试；
检查 `reproduction.json`、`result.json`、`patch.diff` 和 `trajectory.jsonl`。这不是定位质量或真实修复能力实验。

## Docker 容器 smoke

手动触发 `.github/workflows/ci.yml` 的 **Docker zero-call smoke** job。容器基础镜像固定为
`python:3.11-slim@sha256:e41613d42d4891e4930f79523f93f81bbc7632584ec65e36ab055f41a800b41e`；构建时安装
`.[dev]` 并通过 apt 安装 Git。构建工具与 apt 仓库当前未完全锁定，因此新构建得到的是新镜像，
不冒充历史镜像 digest。容器运行时用 `--network none`，仅挂载独立输出目录。严格 `.dockerignore`
只把 `pyproject.toml`、README、`src/` 和该 smoke 脚本送入构建上下文，排除 runs、benchmarks、
本机 `.env` 与验收输入。

等价的本地 Linux 命令：

```sh
docker build --pull=false -f docker/zero-call-smoke.Dockerfile -t tracefix-zero-call-smoke:local .
mkdir -p /tmp/tracefix-zero-call
docker run --network none --rm -v /tmp/tracefix-zero-call:/output \
  tracefix-zero-call-smoke:local --output /output/baseline
docker run --network none --rm -v /tmp/tracefix-zero-call:/output \
  tracefix-zero-call-smoke:local --skills-enabled --output /output/skills
```

这是在 Docker 内运行 TraceFix 的零调用合成流程；它不代表冻结 SWE-bench 任务容器的桥接验收。
冻结 Requests/pytest/Sphinx 镜像仍需各自的本地输入 manifest 和受控材料，不能由公开 smoke 替代。

## 文件与材料清单

| 项目 | 分类 | 发布/复现边界 |
|---|---|---|
| `src/tracefix/`、`pyproject.toml`、`scripts/reproduce_zero_call.py` | 当前公开运行必需 | wheel 包含 TraceFix debugging skill 文件；复现 fixture 在脚本内 |
| `docker/agent-bridge.py`、冻结配方与审核脚本 | Docker/历史复现代码 | 使用时必须配套相同任务身份、manifest、镜像和外部 checkout |
| `docker/reverify-pilot/Dockerfile` | 历史 pytest 试点构建 recipe | 仅重建 2026-09-26 pilot；不是当前通用 smoke 配方 |
| `scripts/pytest10081_diagnostic_matrix.py` | 冻结任务诊断入口 | 需要受控 pytest-10081 输入、独立产品补丁和 Docker image，公开 checkout 不含这些受控材料 |
| `runs/` 下的 task source、gold/test patch、独立验收和原始轨迹 | 外部/本机运行材料 | 被 Git 忽略，不是 GitHub 发布内容；缺少原始材料时无法重建对应历史证据 |
| `docker/requests-test-tls/test-key.pem` | 本地临时测试材料 | 被 `.gitignore` 排除；实际复验在临时目录生成密钥，源码分发不需要此文件 |
| DeepSeek V4.1 tokenizer 与 `deepseek-recipe` wheel | 可选外部计数材料 | wheel hash 在 `docker/token-counter/Dockerfile`；tokenizer hash/recipe 版本在 `src/tracefix/models/input_bounds.py`。原始 wheel/tokenizer 未随 checkout 提供，独立计数镜像需外部获取；当前计数仍是 `estimate` |
| `.venv/`、`.env`、`.pytest_cache/`、临时 coverage/JUnit 与 Docker 构建缓存 | 本机临时状态 | 不作为发布输入 |

`docker/reverify-pilot/Dockerfile` 和诊断脚本作为历史配方与受控诊断入口纳入交付。禁止批量添加 `runs/`、候选留出题或未审查的历史结果。

## Requests TLS 受控复验

TLS 集成检查从冻结 Requests 输入复制出**新的派生输入目录**，只把本地测试服务 URL 改为
`https://localhost`，并写入新的配方及 manifest 哈希；Agent 容器的 HTTP health check 仍走
localhost 服务。运行时通过 `TRACEFIX_TEST_CA_BUNDLE=/opt/tracefix/request-test/test-ca.pem`
将临时 CA 路径传给容器，并由 `sitecustomize.py` 配置 Requests 的默认 CA；握手日志和临时证书
SHA-256 在新输出中记录。Agent 容器与 qualification/patch
验收容器各自生成密钥；TLS 测试材料在容器运行目录创建并在清理时移除。

从当前 checkout 复跑需先取得被授权的冻结 Requests `inputs-v2`、其匹配的源 checkout 和固定
Docker 镜像，再运行：

```powershell
python scripts/docker_runner_e2e.py --task-id psf__requests-1766 --skills-enabled --requests-tls `
  --output-root E:\TraceFixRunsActive\reproduction-requests-tls-<new-id>
```

当前本机结果保存在 `E:\TraceFixRunsActive\reproduce-zero-call-20260928\docker-requests-skills-tls-verified`；
记录只用于本轮验证，不随 GitHub checkout 分发。

## 本轮验证证据（2026-09-28）

- 本地零调用合成 baseline 与 Skills-only 均完成，分别 6 与 7 次 fixture 请求，1 项 pytest 通过；
  Skills-only 加载去重并保留压缩锚点。Docker `--network none` 同样完成两臂，镜像 ID 为
  `sha256:1b9173d675c3214aecebc6f687736ab9401e06a2915f33cd4b190820c595b911`。摘要位于
  `E:\TraceFixRunsActive\reproduce-zero-call-20260928\local-final-baseline`、`local-final-skills`
  与 `docker-container-smoke-final`。
- Requests-1766 正式 Runner Skills/TLS 复验为零供应商请求，触发 7 次上下文压缩，验证真实
  Requests 测试进程的 CA 信任/主机名校验；Agent 与四个新独立验收容器各用不同临时证书身份，
  所有隔离检查通过。摘要在 `E:\TraceFixRunsActive\reproduce-zero-call-20260928\docker-requests-skills-tls-verified`。
- 最终代码 pytest-only 全量运行：Python 3.12.5，602 passed、0 failed，681.40 秒；精确
  Coverage.py 为 89.62%（JSON `covered_lines=8392/9128`，分支覆盖 82.54%），退出码 1 仅因现有
  90% 门槛。JUnit、日志及 coverage 文件位于
  `E:\TraceFixRunsActive\reproduce-zero-call-20260928\pytest-only-final-v2`。终端整数显示的“90%”
  不能当作通过。
- 同版本覆盖率不代表真实修复成功率。pytest-10081 的 Python 点版本对照、Sphinx 基线 recipe
  重建与全平台哈希依赖锁仍未完成；它们的精确材料缺口见下节及交付清单。

## 历史环境复验限制

- pytest-10081 的已记录 18 格矩阵中，官方目标节点资格通过；async warning 节点与公开整文件仍因
  `PytestUnraisableExceptionWarning` 失败。当前冻结环境前一 Python 3.10 点版本单变量对照尚未完成，
  所以不将公开整文件标记合格。
- 当前 checkout 下 `runs/docker-foundation-20260926-v1/inputs-v2` 的 Sphinx recipe 是
  Python 3.11/Windows，不能当作基线 Python 3.10/Linux 的 `inputs-v4`。被审计的 inputs-v4
  staging 在本机输出盘而非公开仓库；新配方重建需重新获得并哈希校验该独立输入，不能用旧 G5
  固定镜像运行追认可重建。
- 历史 patch、精确容器依赖快照、Token counter tokenizer 和外部源码若未公开重新取得，应报告缺项；
  不从旧摘要推断当前代码的全量验证结果。

## 启停和回退

- Skills：默认关闭。使用 `--skills` / `TRACEFIX_SKILLS_ENABLED=true` 开启；移除开关或设环境变量为
  `false` 回到原始工具注册和提示。超限或目录不合法时，技能启用的运行会报错并关闭，不静默回退。
- Docker Skills：与本地共用启停开关和字节上限；bridge handshake 必须返回有效的目录身份，否则
  运行失败关闭。可用同一固定任务分别跑 baseline 与 Skills-only。
- Docker smoke：运行可删容器使用 `--rm`；复现文件是输出证据，不覆盖已有路径。
- MCP/Serena：本轮未接入。后续单独评估，不影响 Skills 开关和内置工具基线。

## 后续比较方案

先冻结当前开发集任务、补丁、容器输入与产品提交，使用相同脚本模型或另行批准的固定模型预算，
仅比较 baseline 与 Skills-only；随机化/交替顺序并记录请求字节与 Token 估算、耗时、搜索/读取次数、
重复操作、无效补丁、独立验收和失败分类。只有经授权的真实模型对照和独立复验支持时，才报告真实
修复成功率；合成控制流、离线定位指标和模型修复成功率分开报告。
