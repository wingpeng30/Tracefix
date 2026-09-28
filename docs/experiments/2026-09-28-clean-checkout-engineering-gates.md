# 2026-09-28 干净 checkout 工程门槛与零调用复现

## 身份与范围

- TraceFix 候选提交：`e72139ddd39b1b78ca07e7cb99de6ca16700bb85`。
- 验收 checkout：`C:\Users\PengZixuan\.codex\worktrees\tracefix-gate-clean\Tracefix`，测试开始和结束时均为干净 tracked tree。
- 平台：Windows x64，Python 3.11.16 和 3.12.5；独立哈希锁虚拟环境。结果分别写入 `E:\TFP\tracefix-engineering-py311-e72139d` 与 `E:\TFP\tracefix-engineering-py312-e72139d`。
- 本次没有供应商模型调用、付费实验、留出题访问、MCP 接入或历史评分/账本/原始证据修改。

## 工程验收

使用 `scripts/check_engineering.py` 分别对两个解释器执行全量 pytest、Ruff、py_compile 和 diff 检查。pytest 使用独立 coverage 数据，不合并历史运行；退出码和验收结论分别记录。两版结果相同：632 passed、0 failed、0 errors、0 skipped；coverage 为 8665/9383 条语句和 2474/2992 个分支，合计精确值 `11139/12375 = 90.0121212121%`。pytest、Ruff、compileall 与 `git diff --check HEAD^` 均返回 0，两个报告 `accepted=true`。

原始结果：

| Python | 汇总 | pytest 日志 | JUnit | Coverage JSON / XML |
|---|---|---|---|---|
| 3.11.16 | `E:\TFP\tracefix-engineering-py311-e72139d\summary.json` | `pytest.log` | `pytest.junit.xml` | `coverage.json` / `coverage.xml` |
| 3.12.5 | `E:\TFP\tracefix-engineering-py312-e72139d\summary.json` | `pytest.log` | `pytest.junit.xml` | `coverage.json` / `coverage.xml` |

SHA256：3.11 `summary.json` 中 pytest log `2614bdbf8106e8b27db782fb6d2fcb55e4c91b46a3b11b3b34bb9f955d40500d`、JUnit `6d9115f49951bfc9701ccc6b873256d65c9a25a6710bfeea23b85d6a258a166b`、coverage JSON `ccd9aec8f954672186fde0ceebe692c47a17dd52518d04e925852daf67bb3efe`；3.12 pytest log `933f966885c371d48544fad9088d3d169e4499b0848eea3d6d12db29a8095cea`、JUnit `9314c399c6a8abddcfba3ba723256d65c9a25a6710bfeea23b85d6a258a166b`、coverage JSON `f8ff628b7d9e399057f60354708da1737f12bf2499bdd60c1b00487afec18015`。权威完整哈希清单以各自 `summary.json` 为准。

过程中的两个候选运行都保留在 `E:\TFP`，没有覆盖或合并：`29fb2f8` 为 89.97979798%，`c5f762e` 为 89.99595960%，均低于门槛；最终只依据 `e72139d` 报告通过。

## 干净安装与本地合成流程

editable baseline 与 Skills-only 结果位于 `E:\TFP\tracefix-editable-smoke`；wheel baseline 与 Skills-only 结果位于 `E:\TFP\tracefix-wheel-smoke`。wheel 安装环境为 `E:\TFP\tracefix-wheel-py311`，导入位置在其 `Lib\site-packages\tracefix`，通过仓库外运行并清空 `PYTHONPATH`。wheel SHA256 为 `6f654faafa959597b9ffd8c2bf1497956ec430d8216200169725e4107fd80132`。最终提交相对 wheel 构建时的源码仅新增测试和 README/工程脚本一行调整，`src/tracefix` 运行实现未改变；wheel 结果支持包内资源与入口可用，但它不是从 `e72139d` 重新构建的 wheel。

editable 与 wheel 两种安装的 baseline/Skills 两臂均完成；每臂均运行一个合成 pytest 测试并生成 app.py diff、result 和 JSONL trajectory。脚本模型请求分别是 6/7；供应商客户端构造、供应商请求和网络连接尝试分别均为 0。Skills-only 激活 `tracefix-debugging` v1.0.0，SHA256 `2275795589fa8bfc5fbad4f8d1666e2d3af95ad85413921ac788a52d39169551`。结果证明的是合成控制流和零调用保护，不证明定位质量或真实修复成功率提升。

Docker backend 的公开合成 prepare 测试以 mock Docker CLI 和 bridge 响应，同时真实生成 Git fixture source archive；检查了完整镜像 ID、固定合成身份、Skills 目录、repo map、`--network none`、无挂载以及异常/正常清理。它不是 Docker daemon 端到端执行。

## 尚未完成与下一步

- 当前机器没有可用的 Docker daemon；推送后需在 Linux Docker CI 运行 baseline/Skills 两臂，核实镜像实际源码与 Skills 哈希、轨迹和容器清理。只有该 CI job 通过，才能报告真实 Docker bridge smoke 通过。
- GitHub Windows CI 保留 Python 3.11/3.12 工程门槛以及 editable/wheel smoke；Linux Docker smoke 支持 PR、main 和 `workflow_dispatch`。本地尚未获得远端运行结果。
- Requests 历史 TLS 容器复验、pytest-10081 Python 点版本诊断、Sphinx 基线 recipe 重建和完整 Token 契约均留待后续任务。

## 验收结论

本次工程门槛和公开本地零调用控制流通过；wheel smoke 与源码版本关系按上文说明。Docker 流程已接入 CI，但远端 smoke 待验证。没有获得离线定位效果或独立真实修复成功率的对照证据，不对成功率作提升结论。

## 后续 GitHub CI 复核（2026-09-28）

GitHub Actions run [36411826884](https://github.com/wingpeng30/Tracefix/actions/runs/36411826884) 对 PR #1 执行了 `b7addcb28dc269344da96b87e6b6003a011f304b`；PR runner 实际检出的合并提交为 `b7f4990abd8bc3b32eaa16bb663d791205663616`。该合并提交相对分支没有额外文件差异。

- Linux Docker baseline/Skills 两臂通过；镜像 ID 为 `sha256:71f996647b24844e6bffa6366e3f73de41e3726242f421718025a4b10012df29`，两臂供应商构造、供应商请求和网络连接尝试均为 0。此 run 的 artifact 尚未完整证明删除后容器不存在，因此只确认 smoke 成功，不把清理审计记为通过。
- Windows Python 3.11.9/3.12.10 各为 632 passed、0 failed、0 errors、0 skipped；pytest 子进程返回 0，Ruff 与编译检查也返回 0。两版精确 pytest-only coverage 均为 `89.91515151515152%`，未达 90%，工程验收因此失败。
- Diff check 两版均返回 128：actions/checkout 的浅克隆没有 `HEAD^`。因质量检查失败，原 workflow 随后的 editable/wheel smoke 被跳过。pytest coverage artifact 获取遇到 HTTP 403，具体代码缺口尚无法从 run 15 的远端 JSON 还原。

上述 run 15 原始结果保留在 GitHub Actions，不覆盖或改写。当前修订为 diff 检查显式解析比较提交、保存 coverage 缺口列表、让安装 smoke 在工程门槛失败时仍运行，并在真实 Docker 运行报告中保存隔离配置及清理核查。修订代码的候选 SHA 和新 run 结果应在推送后追加；在此之前不声称当前候选达到覆盖率门槛或已完成复现验收。

## GitHub CI run 16 与后续修正（2026-09-28）

候选分支提交 `2edb4b6544590dde2f2c171d0885e886de430448`，GitHub Actions [run 36419210411](https://github.com/wingpeng30/Tracefix/actions/runs/36419210411) 的 PR checkout 合并提交为 `4b1af7b42e9a44495496129aaf89c877abaeabe3`。该运行整体失败，原始 Actions job 日志和 artifacts 保留于 GitHub，不覆盖 run 15。

- Windows Python 3.11、3.12 各 642 passed、0 failed、0 errors、0 skipped，Ruff、compileall 和 Diff 检查通过。pytest-only coverage 均为 `11140/12388 = 89.9257345818534%`，离 90% 需再覆盖 10 个语句或分支项目；因此两个工程门槛均失败。此次 workflow 尚未包含 coverage gap 摘要输出，需在下一次 CI 日志定位具体未覆盖路径。
- 两个 Windows 版本的 editable smoke 与 wheel 构建、仓库外 wheel 安装及 baseline/Skills-only smoke 均通过。3.11 wheel SHA256 为 `f734a442a4d49c06db484df70750c6fba2ff17a4975a2c59ee8a446d4d4bdbbd`；安装导入自临时环境的 `site-packages`。这些安装结果不使整体工程门槛变为通过。
- Linux Docker 镜像构建、baseline/Skills-only 两臂运行均完成，报告显示脚本模型请求 7 次、工具结果 6 个、测试退出码 0，供应商客户端构造、请求发送及网络连接尝试均为 0。审计阶段因代码读取 `sha256`、实际 Skills 事件字段为 `content_sha256` 而失败；因此该运行的 Docker 清理审计未通过，不能把它记作完整 Docker 验收。
- artifact：Windows 3.11 ID `10969475060`（zip SHA256 `6fb0c080b019e8e19608162a5aadb89f42721d953827e4bfaad5d5adf3ff2650`），Windows 3.12 ID `10968717714`（`544478aaeb79d4edd5ca069fb77c3d44d1bb6631937bc3dcf27545eaa6e78f62`），Docker ID `10968841539`（`b30af3a30e4e1e8297a7d54b277fc68c89adad96c7b6b7da3a00d54bfbe13db5`）。完整下载内容尚未成功本地读取；以上摘要来自 Actions job 输出与 artifact 元数据。

run 16 后的未提交修正包括：Docker 审计改读 `content_sha256` 并更新回归夹具；新增 `export_evidence()` 无容器、成功复制（含可选服务日志）、Docker cp 失败三项行为测试；工程摘要将未覆盖最多的文件、行号和分支输出到 CI 日志，帮助下一次按具体路径补测。本地 Python 3.12.5 对这些定向测试得到 63 passed，Ruff 通过。pytest 报告一条本地 cache 路径权限警告，不影响这 63 项的测试结果；这不是 Windows 双版本工程门槛证据。尚需推送候选并由全新 CI 运行提供完整判定。

## GitHub CI run 17：候选完整通过（2026-09-28）

候选分支提交 `1882cc92ce2218a254665e5210cb4fe0a77ebe95`；PR #1 的 [GitHub Actions run 36420487054](https://github.com/wingpeng30/Tracefix/actions/runs/36420487054) 对该 SHA 完成，整体 `success`。PR base 是 `main` 的 `2c81ea1567d37e543e30d44dc1a0ea1dd587258b`。

- Windows x64 Python 3.11.16 与 3.12.5 各运行 646 项 pytest，0 failed、0 errors、0 skipped；pytest-only coverage 都是语句 `8678/9392`、分支 `2480/2996`，精确合计 `11158/12388 = 90.07103648692282%`，达到 90% 门槛。两个版本的 Ruff、compileall、Diff（解析基准为 `origin/main`，commit `2c81ea1567d37e543e30d44dc1a0ea1dd587258b`）均通过，engineering summary `accepted=true`。
- 两个版本的 editable smoke 与 wheel 构建、隔离环境安装、仓库外 baseline/Skills-only 流程均通过。实际导入路径位于新建 venv 的 `site-packages`，并验证 wheel 含 reproduce 入口、bridge 和 Skills 资源。3.11 wheel SHA256 `a0072d6dff21ff5a6f7c9c98be95847c817984425387477c847bb7ac6e277e75`；3.12 wheel SHA256 `9b60a7a1563a8886e1ed43732542fabacdc3ab34dbbd45f070a7aea6de96f5a5`。两种安装的 baseline / Skills-only 脚本模型请求数均为 6 / 7，pytest return code 为 0，供应商客户端构造、请求发送及网络连接尝试均为 0；Skills-only 激活版本 `1.0.0`，内容 SHA256 `2275795589fa8bfc5fbad4f8d1666e2d3af95ad85413921ac788a52d39169551`。
- Linux Docker 两臂都使用完整镜像 ID `sha256:ced19b43e55e9e124b61f40b801d8a4173544f9fdab8ee421680325a8fd31882`，容器网络为 `none`、挂载列表为空；baseline 与 Skills-only 的 pytest 均返回 0，供应商构造、请求和网络尝试均为 0。审计 `accepted=true`，两个容器都验证已删除；Docker artifact 同时记录源码和 Skills 身份。
- 原始 artifacts：engineering 3.11 ID `10969064961`，zip SHA256 `4bdeda75ad00879afbc909f37cc1b71da4defde4df5ee4c904d8c651812402d2`；engineering 3.12 ID `10969492061`，zip SHA256 `abd775ae4eaef3fc4012f207e65959d142a3fe162bfefa831357cb8a1b326811`；Docker ID `10969108622`，zip SHA256 `41864ac5d55aed4db0b61edd931bde1da6f364096967df1cfa60bddde3dd62a9`。pytest JUnit、coverage JSON/XML、缺口清单、Ruff 和 compile 日志及 smoke 原始轨迹均在对应 artifact；工程 summary 中记录各原始文件哈希。

本次通过证明候选版本在锁定的 Windows 双版本工程门槛、editable/wheel 仓库外合成运行和 Linux Docker bridge 隔离 smoke 上可复现；合成修复流程不构成离线定位效果证据，也不证明真实独立修复成功率提升。下一批仍按既定顺序处理 Requests 历史 TLS 复验、pytest-10081 点版本诊断、Sphinx recipe 重建及 Token 计数契约。
