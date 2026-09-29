# Runtime Skills 与 MCP 评估（2026-09-27）

## 执行身份与前置条件

- 目标：把按需 Skills 接入 TraceFix 自己的 MinimalAgent，评估 MCP，不预设真实修复收益。
- `docs/goals/2026-09-27-container-qualification.json` 的 G0–G7 均 accepted；最终交接确认没有必需工作残留。指定验收聊天最近一轮 completed。此前同仓 MCP 调研只读，没有仓库实现。
- 环境：Windows 11、Python 3.12.5、pytest 8.4.2；零供应商调用。执行 HEAD `5258dc44e324626cc9ab5345d5f7b68cc5d47613` 加 dirty worktree。环境 Goal 的既有改动均保留。
- 全量 pytest 原始结果：`E:\TraceFixRunsActive\skills-mcp-integration-final-20260927\pytest-full.log` 与 `pytest-full.exit-code`；TEMP/TMP/basetemp 位于 `E:\TFP\skills-mcp-integration-final-20260927`。

## 接入和配置

- `AgentConfig.skills_enabled` 默认 `false`。CLI 单次启用用 `tracefix run ... --skills`；环境变量 `TRACEFIX_SKILLS_ENABLED=1` 也可启用；停用时省略开关并 unset 或设为 `0`。API 可设 `AgentConfig(skills_enabled=True)`。
- 关闭时不扫描技能、不增加 system prompt 或工具描述。开启后提供名称/简介目录和 `load_skill` 工具；模型按需激活正文。references 可单独按需读取。
- 技能只从 TraceFix 随包的 `src/tracefix/skills/` 发现，与目标仓库、验收源码和隐藏材料分开。只返回审核过的 SKILL.md/Markdown references；不执行脚本，不根据 `allowed-tools` 扩权。越界、符号链接、重复或格式错误、缺失引用均返回明确工具错误。
- 内置解析器无外部依赖，接受常见 Agent Skills frontmatter 标量字段及 `metadata.version`，不支持任意复杂 YAML、自定义技能安装目录。
- Skill 主文正文加载后写入激活事件，记录身份、版本、相对路径、SHA-256 和实际进入模型上下文的内容；内容也写入 system 锚点。上下文折叠后该锚点继续保留；重复加载返回 `already_loaded`。reference 内容另记哈希。
- Docker runner 通过同一开关将参数传给既有 Agent bridge；没有重写 JSONL bridge。技能读取于 TraceFix 源码包，不从 `/work/agent` 读取。
- Requests 本地 HTTPS 验收使用 Docker 可选依赖 `cryptography==48.0.0`，每次运行生成 24 小时有效的临时证书和私钥，仅复制进隔离容器；宿主临时文件自动删除。仓库不保存静态私钥。
- 启用 Docker HTTPS 时安装 `pip install -e ".[docker]"`；Skills-only 使用不需要额外依赖。

```python
from tracefix import AgentConfig, RunConfig

config = RunConfig(
    repo=repository_path,
    task="修复指定的测试失败",
    agent_config=AgentConfig(skills_enabled=True),
)
```

## 已制作 skill 与参考材料

- `tracefix-debugging`，版本 `1.0.0`；SKILL.md SHA-256：`2275795589fa8bfc5fbad4f8d1666e2d3af95ad85413921ac788a52d39169551`。步骤：读失败证据、搜索实现与调用路径、提出可验证根因假设、做最小补丁、检查结果、运行最相关安全测试、查看 Diff。没有引入强制人工讨论或整个 Superpowers 框架。
- 参考 [Agent Skills specification](https://agentskills.io/specification) 的目录/frontmatter、名称/描述及渐进披露；参考 [客户端接入指南](https://agentskills.io/client-implementation/adding-skills-support) 的目录元数据、按需激活、去重和压缩后上下文管理。
- 调试流程参考 [Superpowers systematic-debugging](https://github.com/obra/superpowers/blob/main/skills/systematic-debugging/SKILL.md)，只改写流程要点，没有复制源文件。
- `verification-before-completion` 暂缓。已有 `require_tested_completion`、完成提醒、测试额度与结果、最终 Diff 检查；历史验证闭环正式比较未达到采纳条件。再叠加同义提醒会增加上下文，但当前没有证据表明它能补充新的验证能力。
- Skills 自身没有运行时第三方依赖；Docker HTTPS 的新增依赖单列在 `docker` extra。SKILL.md 是项目自己的文本，不含外部脚本或二进制。

## MCP / Serena 评估及决定

- [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 当前文档将 v2 列为稳定主线；要求 Python 3.10+、MIT。v2 提供 async `Client`，可用 stdio 子进程或 Streamable HTTP，`async with` 管理连接，并执行工具发现与调用。TraceFix 当前 `BaseTool.execute`、ToolRegistry 和 Docker JSONL bridge 是同步调用。
- Serena [官方能力表](https://github.com/oraios/serena#programming-language-support--semantic-analysis-capabilities) 列出 `find_symbol`、文件符号概览和 `find_referencing_symbols`。这些语义引用查询可能比当前 `search_code` 的文本匹配、Repo Map 的保守 AST 调用关系更精确地回答“哪些符号引用了此符号”。这是从上游能力和本地工具差异作出的适用性推断；本轮没有在 TraceFix 固定容器中实际比较结果或 Token。
- 查看了 Serena 的 [pyproject.toml](https://github.com/oraios/serena/blob/main/pyproject.toml)：当前 main 声明 `serena-agent==2.0.0.dev0`、Python `>=3.11,<3.15`、`mcp==2.2.0`，另有较大的 LSP、HTTP、UI 和其他运行依赖。README 将 Serena 主体标为 GPL-3.0-or-later、SolidLSP 为 MIT。TraceFix pilot 中 pytest 解释器有 Python 3.9/3.10，控制端 Python 3.11；若走单独控制端 sidecar 有可能满足版本要求，但本轮未构建验证。
- Serena 有潜在定位增益，但尚未冻结可部署 stable release/commit 与完整依赖，也未验证只读白名单、实际源码 mount/版本一致、stdio 会话生命周期、超时/断连、未知结果不重放、轨迹和资源清理。接入需要隔离环境/派生镜像并重验，不适合先放一个不能实测的适配空壳。因此本轮**不接入 MCP SDK 或 Serena**，没有新增 MCP 依赖/许可证/SHA，也没有启用 MCP-only 开关。
- [Context7](https://github.com/upstash/context7) 提供版本相关的远端库文档；其 README 提示文档来自社区且后端服务不在该仓库。本轮保留后续日常开发候选。[GitHub MCP Server](https://github.com/github/github-mcp-server) 可查询 Issue/PR/仓库信息，但可能泄漏冻结任务的修复历史，继续排除在基准之外。

## 验证

| 检查 | 结果 | 范围/备注 |
| --- | --- | --- |
| Skills fixture | 7 passed | 零供应商调用；含开关、按需正文、去重、reference 边界、格式校验、压缩后锚点。 |
| 定向回归 | 91 passed | Skills、MinimalAgent、Runtime、Docker backend，140.70 秒。 |
| 全量 pytest | 550 passed、0 failed、678.59 秒、0 warnings | 修正 package metadata 后的最终代码；E 盘隔离临时根；原始日志如上。 |
| Ruff | exit 0 | 本次涉及 Python 文件。首轮两处 E501 已修复后复跑。 |
| compileall | exit 0 | 129 个 tracked Python 文件及本次变更源文件。 |
| `git diff --check` | exit 0 | 仅有工作区换行符提示。 |
| `skill-creator` `quick_validate.py` | exit 0 | `tracefix-debugging` 格式校验通过。 |

后续按用户要求剔除静态 TLS 私钥，并改成运行时生成临时密钥；定向复验 `tests/test_docker_tls.py tests/test_docker_backend.py tests/test_skills.py` 为 **40 passed**（含真实 localhost TLS 握手），Ruff、compileall 与 `git diff --check` 通过。原先记录的 550 项全量 pytest 在此安全调整之前运行；本次调整后没有重跑全量套件。

静态检查命令与退出码摘要保存在 `docs/experiments/2026-09-27-skills-mcp-integration-static-checks.json`；pytest 原始结果在上列 E 盘目录。

本轮没有运行 Coverage.py，因此**没有**本次工作区的覆盖率门槛结论；此前 G6 覆盖率属于较早源码状态，不能挪作本次结果。工程接入与合成控制流有测试支持；离线定位收益、真实独立修复成功率都仍未知，没有声称提高。

## 执行时代码哈希

| 路径 | SHA-256 |
| --- | --- |
| `pyproject.toml` | `c7c7cd28223c713d814299117ff1f7461f188a9362604ce5ca1b857f7d26d247` |
| `src/tracefix/agent/base.py` | `534a0afe861300e4169a75e7de6a01242f1ea0e666c3ab36e9f23d4602e9f88b` |
| `src/tracefix/agent/minimal.py` | `479b14299d862c6734ead63826d2c20969d4eb0772cd8d5bc2e96bcd7f7f7a54` |
| `src/tracefix/cli.py` | `a0272190a4e7c925e719277076334adcb99fdf1db1fa9ff659e5db58423a81aa` |
| `src/tracefix/docker_backend.py` | `6a4164dbdea748fe3d4e0a91ac3d35f0c08052cadda663313e1287159b326cf2` |
| `src/tracefix/docker_tls.py` | `db72700c625299e175a9f85a8ec11697435977bcb9431b2c36cd090b7c2fe82c` |
| `scripts/docker_runner_e2e.py` | `c3fec6af05f2adaba7e8b162d43d137c3773d42c66d0871068e8d32514232b8b` |
| `tests/test_docker_tls.py` | `5ffb2b2ce6a4b9c658b1f24fcb2c56644f4ef071d0e851cfb1e564d5ac369b8d` |
| `src/tracefix/runtime.py` | `85112a7c73ab5156654aa3c7d45d83c5a2a8ff20b3487829840db30fec55b98a` |
| `src/tracefix/tools/skills.py` | `0cd7a7f1d7d412a483903b7c16d543b88879772b355046d833bce409f140c158` |
| `src/tracefix/skills/tracefix-debugging/SKILL.md` | `2275795589fa8bfc5fbad4f8d1666e2d3af95ad85413921ac788a52d39169551` |
| `tests/test_skills.py` | `3fe58d5e6bab899de99584f38a159ab59770810274f107b703d6b739aa0ca87b` |

## 后续单变量方案

用 10 道冻结开发题固定模型、预算、环境和独立验收，预先登记采纳线；先比较 baseline vs Skills-only。只有 Serena 获得独立派生容器、源码身份与三只读工具端到端验证后，再单独比较 baseline vs MCP-only。组合组仅在单项采纳后设计。对照报告输入/输出 Token、工具调用数、定位和 Agent 耗时、diff 与独立测试结果；不访问 20 道留出题、不自动启动付费运行，并分别报告工程接入、合成控制流、离线定位和真实修复效果。
