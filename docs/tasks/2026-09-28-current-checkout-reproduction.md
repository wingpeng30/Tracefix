# 当前 checkout 的复现闭环（2026-09-28）

## 目标和边界

从基准提交 `5a5bf8a24f605b3f2dc5392995b22940be9f3d61` 开始，本工作补全 GitHub checkout 的公开零调用复现入口，贯通 Docker Skills 目录，并为上下文成本和 Token 请求身份增加保护。MCP/Serena不在本轮接入范围；它需要独立的服务生命周期、容器源码视图和工具白名单验证，未证明能力前不加依赖或空壳适配器。

## 实际改动

- ToolRegistry 暴露显式技能目录；Docker bridge 握手只传名称、简介、版本及内容 SHA-256，不传宿主路径或正文。启用时元数据缺失/不一致会 fail closed。
- Skills 默认关闭。按需读取经审查的 Markdown，按身份/相对路径/哈希去重；检查符号链接、路径逃逸、漂移和 UTF-8 字节边界。初始上限为 4 个激活技能、正文 16 KiB、单参考 8 KiB、累计 32 KiB；这是字节限制，不是 Token 硬边界。
- 完整指令只在模型请求时进入一份 system 锚点，工具结果仅给短回执，整批工具响应闭合后才添加指令。压缩保留锚点。轨迹包含目录、激活/重复/拒绝事件及各请求上下文正文的哈希、字节数和估算成本。
- 新增精简 `tracefix-debugging` 技能。它适配现有搜索、读取、补丁、测试和 diff 工具；不运行技能附带脚本，不从 `allowed-tools` 扩权限。
- 请求 Token 计数记录最终序列化 body 哈希；计数与发送之间 body 变化会在客户端构建/网络之前拒绝。离线配对继续标记 `estimate`，不升级为服务端精确上界。
- 新增 `scripts/reproduce_zero_call.py`、Docker smoke 配方、严格构建上下文和手动 CI Linux job。加入参数校验后的 `scripts/pytest10081_diagnostic_matrix.py` 和历史用途标明的 `docker/reverify-pilot/Dockerfile`。
- Skills 参数可独立开关，MCP 保持未安装/未启用；公开 baseline 与 Skills-only 可做单变量合成回放。

## 依赖与冻结身份

- 项目直接依赖仍按 `pyproject.toml` 范围解析；构建使用 setuptools/wheel，TLS extra 固定 `cryptography==48.0.0`。CI 安装 `.[dev,llm,docker]`。当前未提供支持 Windows/Linux 的完整传递依赖哈希锁，pip 源和 apt 构建包亦未全部冻结；干净环境不能声称逐字节相同。
- 本轮 wheel SHA-256 为 `d694f0f56e2c0c1309bc2d9c69fc63cd2d27f279ebdc486120f5bca0d322f1b4`；在独立 Python 3.12 venv 安装成功，并从安装路径发现 skill `tracefix-debugging` 1.0.0，正文 SHA-256 `2275795589fa8bfc5fbad4f8d1666e2d3af95ad85413921ac788a52d39169551`。容器 smoke 固定 Python slim 基础镜像 digest，但构建得到新派生 image ID，不代表此前冻结镜像。
- debugging 指令为本仓库原创内容，Apache-2.0 上游 Superpowers 仅作设计参考；没有复制其代码/整套框架。Serena、MCP Python SDK 均未引入，无新增第三方许可证义务。

## 验证状态与原始结果

本地与 Docker baseline/Skills-only 零调用合成闭环、Requests-1766 实际 Requests HTTPS 和 CA/hostname 检查通过。运行路径及证据目录列在 `docs/reproduction.md`。合成 fixture 证明控制流和隔离属性，不提供离线定位指标或真实修复率证据。

最终 Python 3.12.5 全量 pytest-only 为 602 passed、0 failed、681.40 秒；精确 coverage 89.62%（8392/9128 语句，2415/2926 分支），因此 pytest 命令退出 1，仅 coverage gate 未达 90%。JUnit、coverage JSON/XML、命令日志和退出码位于 `E:\TraceFixRunsActive\reproduce-zero-call-20260928\pytest-only-final-v2`。Ruff、compileall 和 diff 检查单独通过，不能替代该门槛。

Requests Docker Skills/TLS E2E 在最终生产源码树哈希 `430f586726f58f102e4abaac14429be95c861cb7c1c731bf7771d09c4b448020` 上通过；摘要与独立容器报告位于 `E:\TraceFixRunsActive\reproduce-zero-call-20260928\docker-requests-skills-tls-release-candidate`。pytest-10081 的前一个 Python 3.10 点版本对照未做。Sphinx 的基线 `inputs-v4` staging 不在公开 checkout，准确的独立输入缺项是重建输入/recipe 与相关来源 manifest；G5 的旧固定镜像执行不能证明 recipe 可重建。20 道留出题、付费模型和历史评分/账本未触碰。

## 操作与回退

- Skills 开启：`tracefix --skills` 或 `TRACEFIX_SKILLS_ENABLED=true`；关闭：省略参数或置 `false`。字节上限可通过对应 CLI/环境变量配置。握手/目录错误会在运行开始时明确失败。
- Docker smoke 使用 `--network none`；其输出路径必须新建。工作流从 Actions 的 `workflow_dispatch` 手动启动。
- 完整回退：关闭 Skills 开关即可恢复原提示和工具 schema；回退整个代码用 Git revert 本次提交。MCP 没有运行时开关，因为尚未接入。

## 后续比较

先在冻结开发集做 baseline 对 Skills-only 单变量对照，固定源码、任务、镜像、预算和运行顺序，记录 Token 估算、耗时、搜索/读取、重复操作、无效补丁、独立验收和失败分类。只有模型请求与独立资格证据才支持真实修复成功率结论。本次只报告工程接入和零调用合成控制流。
