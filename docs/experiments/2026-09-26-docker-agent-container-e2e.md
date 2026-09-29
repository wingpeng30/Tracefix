# 2026-09-26：三题 Agent 容器工具与新容器独立验收

## 范围与身份

本轮只做零费用脚本化 Agent 闭环，没有调用供应商、没有正式实验、没有触碰留出题，
也没有改写历史评分或账本。脚本复用 TraceFix 的 `search_code`、`read_file`、`apply_patch`、
`run_tests`、`get_git_diff` 五个工具，但模型控制环仍留在 Windows；本轮没有把正式
`TraceFixRunner` 的付费模型路径切换到 Docker。

宿主机只把已验证 Git bundle、TraceFix 工具源码和必要的运行配置复制进新容器，没有挂载
宿主机 checkout。Agent 容器只收到源码 bundle、任务配方中的非敏感环境变量和已有产品补丁；
不接收 gold、隐藏测试补丁、API 密钥或 Docker socket。验收容器从同一个不可变镜像 ID 新建，
单独接收冻结的验收输入，并从 base commit 重建 checkout。Requests 服务在 Agent 和验收阶段
分别于各自容器内启动，容器网络关闭，代理变量清空。

| 任务 | 固定镜像 ID | 源码提交 | 旧容器复验记录 |
|---|---|---|---|
| pytest-10081 | `sha256:2cc93cb74dd96de3179e8320b5b99049fdef0545ddfcad97d388ccaf34fb646a` | `da9a2b584eb7a6c7e924b2621ed0ddaeca0a7bea` | `runs/docker-foundation-20260926-v1/replays/pytest-dev__pytest-10081/run-2/report.json` |
| Requests-1766 | `sha256:14b0a27aa9f35af99f4c7b2047f9c24da91ab93dd2b86ef100cea7617ef0c757` | `847735553aeda6e6633f2b32e14ba14ba86887a4` | `runs/docker-foundation-20260926-v1/replays/psf__requests-1766/run-2/report.json` |
| Sphinx-10449 | `sha256:f090d53486c3c4bbfc823adc9c52428e66a5f3e94e0010d4a685a0826310cb0e` | `36367765fe780f962bba861bf368a765380bbc68` | `runs/docker-foundation-20260926-v1/replays/sphinx-doc__sphinx-10449/run-1/report.json` |

所有 Agent/验收新报告写入 `E:\TraceFixRunsActive`。D 盘约有 29 GiB、E 盘约有 126 GiB
可用空间；Docker Desktop 的 `docker_data.vhdx` 位于 `E:\dockerspace\DockerDesktopWSL\disk`，
本轮未构建镜像。

## 结果

| 任务 | Agent 读取/补丁/测试 | 仅测试后的产品 diff | 导出产品文件 | 新验收容器 | 与旧复验匹配 |
|---|---|---|---|---|---|
| pytest-10081 | smoke test 前后通过 | 空 | `src/_pytest/unittest.py` | qualification 2/2 合格；产品补丁 2/2 通过 | 是 |
| Requests-1766 | 本地 httpbin 健康；6 项 smoke 测试通过 | 空 | `requests/auth.py` | qualification 2/2 合格；产品补丁 2/2 通过 | 是 |
| Sphinx-10449 | smoke test 前后通过 | 空 | `sphinx/ext/autodoc/typehints.py` | qualification 2/2 合格；产品补丁 2/2 通过 | 是 |

产品补丁均由容器内 `get_git_diff` 导出。它们与输入中的保存补丁在文本格式/hash 上
不完全相同（CRLF 统一为 LF 且由 Git 重建 diff），但三题新验收的目标节点、返回码、测试数、
依赖指纹均与既有容器复验两次一致。Agent 结束前在其 checkout 写入额外哨兵文件；新验收容器
不含该文件。脚本每次记录 run ID、两个容器 ID、源码提交、输入清单 SHA-256、配方指纹和镜像 ID。

每个成功运行目录包含 `run.json`、`agent-product-patch.diff`、Agent pytest 审计/JUnit，
以及 `independent-acceptance/report.json` 与复制出的严格验收证据。例如：

- `E:\TraceFixRunsActive\docker-agent-e2e-pytest-dev__pytest-10081-11bf2d5f`
- `E:\TraceFixRunsActive\docker-agent-e2e-psf__requests-1766-41d965fb`
- `E:\TraceFixRunsActive\docker-agent-e2e-sphinx-doc__sphinx-10449-78b5dc17`

首轮/调试失败尝试也保存在相邻 E 盘目录，不覆盖或删除。它们发现了桥接模块 Linux
`PYTHONPATH`、测试临时目录登记顺序和全文件 smoke 不稳定问题；后续通过修正桥接并缩小到
公开 smoke test 完成验证。pytest-10081 的整文件 smoke 曾有一个异步测试因该镜像关闭
pytest 插件自动加载而失败；独立验收目标测试仍稳定通过两次。该差异作为环境限制保留。

## 工程改动与验证

- 新增 `docker/agent-bridge.py`：在 Linux 进程中实例化现有五工具，JSON Lines 转发工具调用；
  `run_tests` 的审计文件写在仓库外的容器 evidence 目录，测试临时目录由工具自身登记。
- 新增 `scripts/docker_agent_e2e.py`：校验 staged input 全部哈希和镜像身份；创建无网络、无
  host bind、无密钥/Socket 的 Agent 容器；导出产品 diff；再用相同镜像 ID 新建验收容器。
  Windows 默认产物根为 `E:\TraceFixRunsActive`，可由 `TRACEFIX_RUNS_ROOT` 或 `--output-root`
  覆盖。`run`/CLI 的新 Agent 输出现在也读取 `TRACEFIX_RUNS_ROOT`；既有显式
  `TRACEFIX_OUTPUT_DIR` 优先级不变。
- `scripts/docker_reverify.py` 增加外部 Agent 补丁参数，仍验证原输入 manifest 不变，且报告
  单独记录 Agent 导出补丁哈希。
- Windows 上新 Agent 运行默认写入 `E:\TraceFixRunsActive`，Linux/macOS 仍默认 `runs`；
  `TRACEFIX_RUNS_ROOT` 可覆盖平台默认，CLI 显式 `--output-dir` 与既有 `TRACEFIX_OUTPUT_DIR`
  仍优先。
- 针对镜像与恢复身份变化、验收重复不一致、Evidence 缺失、测试命令拒绝、测试超时、仅运行
  测试 diff 为空，以及输出根配置增加回归。

定向回归：9 passed；失败用例单独修正 fixture mock 后 1 passed。最终全量 pytest 为
`492 passed`、退出码 0、640.41 秒。改动文件范围 Ruff 与 compileall 均退出 0，
`git diff --check` 退出 0。全仓 Ruff 扫描退出 1：它递归扫描了项目内已有的 pytest 临时
仓库/旧 `.tfx*` 现场和权限拒绝目录，报告 28 个测试夹具格式项与大量 `Access denied`；
这些生成目录未纳入本轮改动，因此使用改动文件范围的 Ruff 结果。全树 compileall 返回 0，
但也报告若干既有临时夹具不可遍历；改动文件 compileall 无此问题。没有运行覆盖率，本轮
结果不用于判断 Agent 修复能力。

执行后再次核对 3 个 staged input manifest，source bundle、配方身份及全部文件哈希仍通过；
历史输入未改。`docker ps -a` 未发现遗留 `tfx-agent-*` 或 `tfx-accept-*` 容器。D 盘约
33.9 GiB、E 盘约 124.9 GiB 可用；Docker VHDX 在 E 盘。

## 限制与下一步

脚本化 Agent 是“读源码—套用已存在补丁—跑公开 smoke—导出 diff”的确定性驱动，不是模型
修复效果。它验证了 Linux 工具桥和分离验收，但尚未接入 `TraceFixRunner` 的通用可切换
`container` 执行模式；实际模型仍会由 Windows 控制进程调用，尚未进行任何模型调用。本地已有
镜像已验证，不能外推到全部题目的 Linux 依赖可复现性。容器中断时记录身份；自动恢复/续接
容器进程尚未实现，用户可安全地从冻结 bundle 重新启动一个新 run，不能把中断状态伪装为完成。

下一步先把已验证的 `DockerToolSession` 代理纳入正式 `RunConfig` 开关，编写不创建 LLM 客户端
的模拟 `TraceFixRunner` 端到端测试；随后再次对这三题做零费用回归。只有模型接口、费用保护、
独立验收和恢复门槛都能在不发请求的 fixture 中通过，才讨论开启真实 Agent 执行。Token 硬计数
契约仍是独立的付费实验门槛，约 1M 请求仍未实测。
