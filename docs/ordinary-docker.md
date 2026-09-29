# 普通 Python 仓库的 Docker 路径

此路径用于干净的 Git 仓库和 pytest 项目。它与冻结历史任务的 Docker profile 分开；依赖必须在显式准备镜像时安装，运行时不联网、不安装依赖。本地 Agent 控制进程仍会读取任务源码并向模型发送选中的内容；容器只隔离工具和测试代码。当前验证目标为 Linux Docker。Windows Docker Desktop 未实测，不能据此认定可用。

## 准备镜像

在 Linux 主机上安装 TraceFix、Git 和 Docker，确认 Docker daemon 可用。对没有额外依赖的纯 Python 项目：

```bash
python -m pip install -e .
tracefix docker-prepare --output ./ordinary-image.json
```

有项目依赖时，先人工审阅并提供适用于镜像 Python 3.11/Linux 的完整哈希锁文件；构建使用 `pip --require-hashes`，不会读取目标仓库的任意安装脚本作为自动配置：

```bash
tracefix docker-prepare --requirements ./project-requirements.lock --output ./ordinary-image.json
```

准备命令生成 `.build.log` 和 JSON，其中 `image_id` 是运行时必须指定的不可变 `sha256:` ID，另有 Dockerfile、基础锁文件和项目锁文件哈希及解释器探针。构建阶段按锁文件访问包索引；执行阶段容器禁用网络。

## 运行与检查

配置文件放在目标仓库外：

```toml
[run]
repo = "../my-python-project"
task = "修复 tests/test_widget.py 中的分页错误"
test_target = "tests/test_widget.py"
source_import = "my_package"
model = "your-provider/your-model"
output_dir = "./runs"
execution_backend = "docker"
docker_profile = "ordinary"
docker_image_id = "sha256:此处填写ordinary-image.json中的完整ID"
```

```bash
tracefix run --config ./config.toml
tracefix verify --run ./runs/<run-id>
tracefix report --run ./runs/<run-id>
tracefix export --run ./runs/<run-id> --output ./fix.patch
```

`run` 把指定提交的 Git archive 复制到独立容器，探测 `source_import` 是否来自该 checkout，然后才开始模型循环。容器为非 root、只读根文件系统、独立 tmpfs、无网络、无宿主源码挂载或 Docker socket，限制 CPU、内存和进程数。测试目标应是仓库中现有的 pytest 目标；TraceFix 不会替用户准备外部服务或复杂构建环境。`verify` 使用同一镜像 ID 建立**新容器**，重新应用结果中的补丁并执行记录的公开测试，保存与源码提交、补丁哈希、镜像 ID 绑定的独立证据。原仓库不会被修改。

不能在普通 Docker profile 上使用 `resume`。桥接断连、执行结果未知或容器丢失时应检查保存的轨迹和容器身份，并从新运行开始；不得自动重放可能有副作用的工具。正常与异常退出都会尝试清理 TraceFix 拥有的容器；如果 daemon 不可达导致清理失败，结果会标为 `container_cleanup_failed`，`docker-run-state.json` 记录待人工核对的容器 ID。清理前必须验证容器的 `tracefix.run_id` 标签。

零供应商调用回放：

```bash
image_id="$(python -c 'import json; print(json.load(open("ordinary-image.json"))["image_id"])')"
python examples/replay_ordinary.py --output ./ordinary-replay --docker-ordinary-image-id "$image_id"
```

该回放使用录制响应驱动真实 Runner、工具、容器和测试，不证明实时模型的修复能力。模型供应商凭据在该回放中不需要。
