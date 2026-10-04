# 在自己的 Python 仓库运行 TraceFix

同机原 checkout 的任务检查和安全恢复见 [`recovery.md`](recovery.md)。

此路径支持**受信任、干净的 Git 仓库**，使用本地 Python 3.11/3.12、pytest 和调用者预先安装的项目依赖。TraceFix 克隆源提交，在独立 checkout 修改及测试；本地工具和 pytest 会执行项目代码，本地后端不是安全沙箱。含编译步骤、特殊构建流程、必须自动加载的 pytest 插件或外部服务的仓库，需要自行验证环境配方。普通仓库容器路径另见 [`ordinary-docker.md`](ordinary-docker.md)；本机 Windows Docker Desktop 尚未通过验证。

## 安装和配置

从 GitHub 获取本仓库后，使用独立虚拟环境安装。PowerShell 示例：

以下命令使用已合入 PR #1–#10 的 `main`（核验提交 `0954c4fc638eb6f43f7fe6403b11fa099af096f4`）。本项目尚未发布稳定版本。

```powershell
git clone https://github.com/wingpeng30/Tracefix.git
cd Tracefix
git switch main
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[llm]'
.\.venv\Scripts\tracefix.exe --help
```

目标仓库另用一个已经安装其依赖和 pytest 的测试虚拟环境。不要把 API Key 写入 TOML。可在配置目录放 `.env` 并在 TOML 中指定；凭据也可由进程环境提供。以下 `config.toml` 位于目标仓库之外，相对路径均相对配置文件：

```toml
[run]
repo = "../my-python-project"
task = "修复 tests/test_widget.py 中的分页错误"
test_python = "../my-python-project/.venv/Scripts/python.exe"
test_target = "tests/test_widget.py"
source_import = "my_package"
output_dir = "./runs"
model = "deepseek/deepseek-flash"
env_file = "./.env"
```

`repo` 必须是 Git 根目录且工作树干净。**输出目录必须位于源仓库之外**；如果在目标仓库内启动命令，请显式设置仓库外的 `--output-dir`。`source_import` 是安装后应从 TraceFix 新 checkout 导入的模块名；探针在构造模型客户端之前执行。`test_target` 是该 checkout 中已有的相对测试文件或 pytest node ID。目标虚拟环境如安装了旧版项目，必须先排除路径冲突；探针会拒绝导入来源落在 checkout 外的结果。配置示例使用 DeepSeek [官方模型名 `deepseek-flash`](https://api-docs.deepseek.com/quick_start/pricing/)；本版本已有小批真实模型闭环证据，范围与限制见 [`experiments/2026-09-29-live-model-acceptance.md`](experiments/2026-09-29-live-model-acceptance.md)。运行 `doctor` 不会发送供应商请求。

可为结束前验收声明额外已有测试目标：

```toml
regression_targets = ["tests/test_pagination_edges.py", "tests/test_public_api.py"]
```

也可重复传入 `--regression-target`；CLI 提供列表时整体覆盖 TOML 列表。启用后，TraceFix 在构造模型客户端之前，于独立源码 checkout 为每个追加目标记录基线。断言失败作为“原有失败”留档；超时、收集失败、导入错误或证据不全会阻止模型启动。模型提出完成后，harness 运行原目标和所有追加目标；失败证据会返回下一轮请求。同一源码状态最多自动验收一次，代码变化后才能再次验收。所有目标都是任务契约的一部分，Agent 不得通过修改或删除目标测试绕过失败。

基线与结束前每个 pytest 进程都计入 `max_test_runs` 和活动时间。启动至少需要 `2 × 追加目标数 + 1` 次测试预算（每个追加目标一次基线，原目标与所有追加目标各一次结束验收）；剩余次数供 Agent 调查、修复和测试。超出预算会中断任务，不能显示验收通过。首版只支持普通本地后端，验收结论只覆盖明列目标。

本地受审核的技能目录可在仓库外设置为 `[run].skills_dir = "./approved-skills"`，并通过 `tracefix run --skills` 显式启用。TraceFix 不会自动信任目标仓库里的技能；恢复时逐份复核已加载正文和参考文本的身份与累计字节数。

```powershell
.\.venv\Scripts\tracefix.exe doctor --config config.toml --json
.\.venv\Scripts\tracefix.exe doctor --config config.toml --prepare --json
.\.venv\Scripts\tracefix.exe run --config config.toml
.\.venv\Scripts\tracefix.exe run --config config.toml --regression-target tests/test_public_api.py
.\.venv\Scripts\tracefix.exe verify --run .\runs\<run-id>
.\.venv\Scripts\tracefix.exe verify --run .\runs\<run-id> --regression-target tests/test_existing_behavior.py
.\.venv\Scripts\tracefix.exe report --run .\runs\<run-id>
.\.venv\Scripts\tracefix.exe export --run .\runs\<run-id> --output .\fix.patch
```

命令行参数优先于 `TRACEFIX_*` 环境变量，后者优先于 TOML，最后使用默认值；密钥仅从进程环境或 `.env` 读取。快速 `doctor` 返回检查项、布尔结果和修复建议；`doctor --prepare` 额外在临时独立 checkout 中执行与运行一致的 pytest／源码导入探针，结束后清理，仍不调用模型或安装依赖。失败退出码为 2。`run` 的终态、结果目录及补丁路径会打印到终端。配置的结束前验收与 Agent 内验收分别记录；其目标会自动加入后续 `verify --run`。独立验证在新 checkout 中复跑冻结目标，保存不可覆盖的记录。隐藏测试与未列目标不在结论范围内；回归验证报告见 [`experiments/2026-09-30-regression-verification.md`](experiments/2026-09-30-regression-verification.md)。`report` 生成单文件 HTML；`export` 只复制补丁和校验文件，不会将补丁应用于原仓库。输出目录默认是当前目录下的 `runs`；可用 `TRACEFIX_RUNS_ROOT` 或 `--output-dir` 覆盖。

没有 Key 时先运行公开离线演示，验证安装、工具、Skills 与报告：

```powershell
.\.venv\Scripts\tracefix-reproduce.exe --scenario pagination --output runs/demo-baseline
.\.venv\Scripts\tracefix-reproduce.exe --scenario pagination --skills-enabled --output runs/demo-skills
.\.venv\Scripts\tracefix.exe report --run runs/demo-skills
```

另一个普通仓库接入回放使用录制的 LiteLLM 形态响应，实际经过普通 Runner、适配器解析、源码导入探针、pytest 和导出：

```powershell
python examples/replay_ordinary.py --output runs/ordinary-replay
python examples/replay_ordinary.py --validation-gate-example --output runs/validation-gate-replay
```

该脚本生成自己的干净 Git 仓库和独立运行目录；输出中的 `result` 路径可传给 `tracefix export --run <result 所在目录> --output fix.patch`。它不调用供应商，原仓库保持干净。

结束前回归演示会先应用一版通过主测试但破坏负数哨兵与身份函数的补丁；离线回放收到自动测试反馈后再修复，随后在新 checkout 独立复验。它验证 harness 控制流，不验证供应商模型的决策质量。普通 `run` 的测试通过只说明列出的公开测试有通过证据，不等于完整正确性。产物中的完整历史、请求视图、估算 Token、供应商 usage 和成本各有不同含义；请求视图默认不记录。普通本地运行支持符合身份约束的 checkpoint 与安全恢复，详见 [`recovery.md`](recovery.md)。

## 仅凭安装包验证回归反馈

```powershell
tracefix-reproduce --scenario regression-feedback --output "runs/regression feedback"
$summary = Get-Content "runs/regression feedback/reproduction.json" -Raw | ConvertFrom-Json
tracefix inspect --run $summary.run_path --json
tracefix verify --run $summary.run_path
tracefix report --run $summary.run_path --output "runs/regression feedback/another-report.html"
tracefix export --run $summary.run_path --output "runs/regression feedback/another-export.patch"
```

输出目录必须尚不存在。此入口创建并保留 `source/`，运行工作副本在其外部；退出后可重复独立验证，每次生成新记录。默认自动导出 `export.patch` 和哈希、生成 `report.html`。两次基线加两轮三目标验收共八个 pytest 进程；独立验证不计入 Agent 预算。录制客户端在返回第二版修复前确认请求含两个回归失败。仅本地；Docker、Skills、image ID 显式拒绝。无需安装 LiteLLM 可选依赖，但需 Git、pytest 及基础包依赖。所有 usage 是录制模拟值，费用缺失；不发送模型或网络请求。
# 公开任务资格入口

普通仓库路径可用于公开任务资格和生产参考回放。当前三类任务的来源、准备依赖、
命令、平台证据与限制见[三类型任务记录](tasks/2026-10-02-three-public-task-types.md)。
`scripts/qualify_public_task.py --task <审核过的任务 ID>` 支持显式选择任务；不传
`--task` 保留 more-itertools 默认值。任务包需要仓库文件，运行时 Agent 和工具可来自
仓库外安装的 wheel；资格回放不是模型自主修复，新的付费验收另行授权。

## 普通 Docker 恢复候选

当前 G3 候选正在完成工程资格，不代表阶段正式交付。运行时显式使用
`--execution-backend docker --docker-profile ordinary --docker-image-id <完整镜像ID>`
和 `--docker-recovery`；TOML 对应 `docker_recovery = true`。默认关闭。
安全暂停后用 `tracefix inspect --run <目录> --json` 检查，随后
`tracefix resume --run <目录>` 重建容器继续。追加要求使用 `tracefix continue`
或 `tracefix chat --run`，初次连续会话用 `tracefix chat --config`。
连续会话共享累计预算，不因等待输入或重建容器增加预算。

```powershell
tracefix-reproduce --scenario docker-recovery --backend docker --image-id <完整镜像ID> --output runs/docker-recovery-check
```

此离线入口要求真实 Linux Docker、Git 和已准备的 ordinary 镜像；输出目录必须不存在。
它运行读取后、补丁后、测试后中断及二次恢复，逐次核对新宿主进程、新容器、删除结果，
再用独立容器验收补丁。供应商访问禁止，模型 usage 为模拟值。快照上限 1 GiB，
越界链接、损坏文件和未决模型或修改结果会阻断恢复。长期经验由宿主保存，容器加载
固定版本的 Skills 快照；工程回放不验证真实模型提炼或决策质量。

启用 ordinary Docker 恢复时，容器私有 Git 排除未跟踪的 Python/pytest 缓存，
使差异与产品快照一致；目标仓库无需新增 `.gitignore`。已经由 Git 跟踪的
缓存目录内产品文件仍保存并恢复。文件新增、删除、执行权限及 Skills 状态
的真实 Linux 检查由 `scripts/check_docker_recovery_products.py` 和 CI 执行。
