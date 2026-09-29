# 可选 Serena MCP 符号查询（C 批）

本功能只适用于本地 Agent 后端；查询服务在独立 Docker 容器内运行。Agent 只看到三个固定工具：`mcp_serena_symbols`、`mcp_serena_find_symbol` 和 `mcp_serena_references`。Serena 本身还提供写文件、命令执行和记忆工具，这些均不注册给 Agent。服务返回值是未受信任的工具数据。

## 安装与准备

先按 [普通仓库指南](ordinary-repository.md)准备受信任 Python/pytest 仓库和测试解释器，再安装可选依赖：

```powershell
python -m pip install ".[llm,mcp]"
docker build -f docker/serena-mcp.Dockerfile -t tracefix-serena-mcp:1.7.0 .
$imageId = docker image inspect --format '{{.Id}}' tracefix-serena-mcp:1.7.0
```

Linux Shell 用 `image_id="$(docker image inspect --format '{{.Id}}' tracefix-serena-mcp:1.7.0)"`。镜像构建阶段安装锁定的依赖和 Pyright；运行阶段不联网下载。将镜像的不可变 `sha256:` ID 写入 TOML 或传入 CLI。不能传可变标签。

```toml
[run]
repo = "../my-python-repo"
task = "修复公开 pytest 失败"
test_python = "../venv/Scripts/python.exe"
test_target = "tests/test_example.py"
source_import = "mypackage"
model = "deepseek/deepseek-v4-flash"
output_dir = "../runs"
mcp_serena_image_id = "sha256:把 docker image inspect 返回的完整 ID 填在这里"
```

```powershell
tracefix run --config tracefix.toml
# 也可在原有 run 命令后加 --mcp-serena-image-id $imageId
```

每次调用复制当前独立 checkout 中的文件至临时快照，容器仅只读挂载该快照。容器无网络、无 Docker socket、非 root、只读根文件系统，使用临时空间存放 Serena 索引；请求结束即关闭会话并删除容器和快照。工具结果含快照 SHA-256，可与轨迹中的调用 ID、源码补丁和测试结果追溯。源码修改后下次查询使用新快照。快照上限为 5,000 个文件和 100 MiB；该限制是产品支持边界，不是对任意不可信仓库的安全保证。

零供应商调用的复现入口：

```powershell
python examples/replay_ordinary.py --output runs/mcp-replay --mcp-serena-image-id $imageId
```

此命令让脚本响应驱动实际 Runner、官方 MCP 客户端、隔离容器、Serena 符号工具、补丁与 pytest。它验证 harness 的调用链，不证明真实模型能自主定位和修复。

版本身份：官方 MCP Python SDK `1.28.1`，Serena `1.7.0`，Pyright `1.1.403`，宿主与镜像依赖分别锁在 `requirements/locks/mcp-py311.txt` 和 `requirements/locks/mcp-serena-image-py313.txt`。Serena `1.7.0` 的许可为 MIT；当前上游主分支许可不同，所以不使用浮动主分支。已核验的 Serena wheel SHA-256 为 `6dbf1459670d96fb0595f84932adef34260a6fe14ba5135b901fdb3c8c76e891`，官方 MCP SDK wheel SHA-256 为 `2726bca5e7193f61c5dde8b12500a6de2d9acf6d1a1c0be9e8c2e706437991df`。

当前 Windows Docker Desktop daemon 不可用，容器路径以 Linux CI 为验收；本机只完成官方协议与真实 Serena 进程的直接探针。普通本地运行默认关闭 MCP；未安装可选依赖或 Docker 时不受影响。显式启用时预检失败会在模型构造前停止，并将原因保存到结果中。服务中断或清理失败作为工具或运行错误记录，不自动重试不确定结果。
