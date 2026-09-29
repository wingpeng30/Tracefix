# Serena MCP 零供应商调用验收，2026-09-29

执行代码：`5ece893dad64d9388dd336820ceea0e5d49487b6`；GitHub PR 检出合并提交 `645626cdc3549f0e410d3c20d67df37119ec8961`。CI [#32](https://github.com/wingpeng30/Tracefix/actions/runs/36544457821) 的 `mcp-serena-zero-call` job 成功，原始 [artifact](https://github.com/wingpeng30/Tracefix/actions/runs/36544457821/artifacts/11021373713) ZIP SHA-256 为 `d05c89f2328f5ff5ae01599882752a7c74d0eea1e88d66a0258ad9d07d89e7d2`。不可变镜像 ID：`sha256:f1f167decfa034e2af8130be5f5babce85106817e2f2f7843c8ea5d585348dd3`。

任务：`examples/replay_ordinary.py` 创建的普通 Python/pytest 仓库，源码提交 `1f6f4edfc6ade9a84de4c6fbf2a2818625760c8d`，错误为 `widget.next_page(1)` 返回 `1` 而测试要求 `2`。模型为 `offline/replay`，固定脚本响应驱动实际 LiteLLM 适配器；供应商请求数为零。宿主为 Linux Python 3.11，Serena `1.7.0`、官方 MCP SDK `1.28.1`、Pyright `1.1.403`，依赖哈希锁分别见 `requirements/locks/mcp-py311.txt` 与 `requirements/locks/mcp-serena-image-py313.txt`。容器执行时无网络、无宿主凭据及 Docker socket，只读源码快照，非 root。

原始 `trajectory.jsonl` 中，`replay-1` 调用 `mcp_serena_symbols` 返回 `{"Function": ["next_page"]}`；`replay-2` 调用 `mcp_serena_find_symbol` 返回 `widget.py` 中 `next_page` 的定义位置。两次 `success=true`，同一查询快照 SHA-256 为 `899f51529d4c84d7e48f32e222aef0796e12b097ee1a3467d047c0eb58fa0a1e`。随后工具链完成补丁、2 次公开 pytest 测试和 Diff，`result.json` 为 `completed`、`verified`，修改文件仅 `widget.py`，补丁 SHA-256 为 `0b7ac0aa8c1d75d306fa2a554fdcdc87c3640ebc1ca9d5d014c66778390feec0`。CI 还检查无残留 `tracefix-mcp-*` 容器。

前一轮 CI 曾出现假阳性：Serena 语言服务因无网络容器内找不到非 root 可用的 Node 而查询失败；shell 管道的 `tee` 把 Python 非零状态吞掉。已改为镜像预装系统 Node、启用 Pyright 全局 Node，并在 CI 使用 `set -euo pipefail`。本记录只引用修复后的原始成功轨迹。

这是**协议、隔离、工具调用和合成修复流程**的验收，不是当前真实模型的自主修复效果；没有独立于公开 pytest 的隐藏验收。Windows Docker Desktop 本机 daemon 不可用，未验证 Windows 容器路径。Linux 服务正常路径已验证；超时、断连、输出截断和清理错误另由无供应商调用的故障测试覆盖，不把 mock 结果等同于真实服务故障率。
