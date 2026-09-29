# 2026-09-26 正式 Runner 容器接入与三题查漏

## 结论

正式 `TraceFixRunner → MinimalAgent → Docker 工具桥 → 新容器独立验收` 已在
pytest-10081、Requests-1766、Sphinx-10449 三题完成零费用贯通。三题各自通过五个现有工具、
公开 smoke、产品补丁导出和严格独立验收；每题的 base/gold 资格与产品补丁验收均在全新容器中
重复两次通过。测试前产品 diff 为空，保存补丁分别只改一个预期产品文件。各验收容器使用同一
冻结镜像 ID、没有宿主挂载，Agent checkout 内的隔离哨兵未进入验收容器。Requests 本地服务的
日志保留了测试子进程的请求记录。

这只证明三题冻结镜像和选定验收入口可工作，不证明三个仓库的完整公开测试套件均已具备合格
Linux 环境。更大的公开测试文件均有失败；TraceFix 自身全量 pytest 也被 Windows 临时目录
访问控制阻断。因此容器验收资格为“限定三题的 smoke 与严格目标验收通过”，完整环境资格仍未通过。

## 执行身份与输出

实现运行代码为本工作区状态（未提交）；三题正式结果写入
`E:\TraceFixRunsActive\docker-runner-final-20260926\docker-runner-e2e-summary.json`。
摘要 SHA-256：`d8e94728affb87cd683ef48578a0b015f2f489bda4e55e74cb82989e7eafa5a0`。每题目录各自包含 RunResult、完整 JSONL 轨迹、调用事件日志、
Docker 状态身份、HTTP 服务日志（Requests）、Agent pytest 审计/JUnit、产品补丁、独立验收
报告和严格证据审计。共享冻结输入 manifest 哈希如下：

| 任务 / run ID | 源码提交 | 输入 manifest SHA-256 | Agent 产品文件 | 公共 smoke | 新容器资格/补丁验收 |
| --- | --- | --- | --- | --- | --- |
| pytest-10081 / `20260926T133733Z-fff79b29` | `da9a2b584eb7a6c7e924b2621ed0ddaeca0a7bea` | `976fab5de4d225710440ab9ba23f9c30058ffc5ccb0eaa4b8c25f8dd5de696a6` | `src/_pytest/unittest.py` | 2/2 通过 | 资格 2/2，补丁 2/2 |
| Requests-1766 / `20260926T133831Z-0676c6bb` | `847735553aeda6e6633f2b32e14ba14ba86887a4` | `cc4abcca02ab5c0b056ed705faaa3e597ddf19573fff017dd1d8e929145655c9` | `requests/auth.py` | 2/2 通过 | 资格 2/2，补丁 2/2 |
| Sphinx-10449 / `20260926T133913Z-34d36c42` | `36367765fe780f962bba861bf368a765380bbc68` | `67410449957fa61ca6059ba6f2e97403bd3a8aee29366e05ca7043e376b05979` | `sphinx/ext/autodoc/typehints.py` | 2/2 通过 | 资格 2/2，补丁 2/2 |

E 盘原始增强版汇总写入时已完成全部三题并标记通过。第一次 CLI 打印汇总时，Windows GBK
控制台无法编码 Requests 服务日志中的 `ø`，使 Python 在文件落盘后以 UnicodeEncodeError 退出。
现已把 CLI stdout 切换到 UTF-8 并使用可回退错误处理；磁盘上的 JSON 报告通过独立读取，三题
各项 `checks` 全为 true。没有因这个打印错误重跑 Agent 或验收。

运行没有构造供应商客户端：每题 Agent 恰好进行 3 次实际 pytest（base smoke、补丁 smoke、
较大公开文件诊断），工具状态和 `process_started` 事件写入 E 盘运行目录；模型请求数为 0。
没有进行正式实验、留出题评测、付费调用、账本或历史评分改写。Docker Desktop 镜像仍固定为：

| 任务 | 镜像 ID |
| --- | --- |
| pytest-10081 | `sha256:2cc93cb74dd96de3179e8320b5b99049fdef0545ddfcad97d388ccaf34fb646a` |
| Requests-1766 | `sha256:14b0a27aa9f35af99f4c7b2047f9c24da91ab93dd2b86ef100cea7617ef0c757` |
| Sphinx-10449 | `sha256:f090d53486c3c4bbfc823adc9c52428e66a5f3e94e0010d4a685a0826310cb0e` |

## 正式 Harness 与工具协议

- `RunConfig`/CLI 增加 `local|docker` 选择，默认仍为 `local`。Docker 运行必须指定三道冻结任务
  之一及冻结输入目录；准备失败不会退回 Windows 本地工具。
- Docker 后端复用现有 `search_code`、`read_file`、`apply_patch`、`run_tests`、
  `get_git_diff`。Repo Map 在容器内建立。Windows Git 生成 Linux 源码归档时禁用
  `core.autocrlf`，容器内源码字节与冻结 Git blob 对齐；保留原补丁和文件行尾。
- JSONL 桥核对协议版本、run ID、tool call ID 和结果身份；持续排空 stderr，限制单帧 64 MiB，
  测试 RPC 超时晚于 pytest 自身 timeout。请求派发、请求发送、进程启动、结果到达或状态不明
  按调用 ID 追加到 `tool-events.jsonl`。pytest 只有子进程成功创建后才经回调计额；桥接回调
  出错时会终止容器内 pytest 进程组。
- 每个运行目录原子保存 `docker-run-state.json`，绑定 run/task/container/image/source commit、
  frozen input manifest 与 recipe hash。只读 `scripts/inspect_docker_run.py` 会校验身份并区分
  已完成和状态不明调用。当前没有自动重启或续跑：状态不明调用不重放，完成态只读复用。
- Agent 哨兵现在放在 Agent checkout 中受保护的 `.tracefix-build-tmp`，不混入产品补丁；验收
  容器在 `/work` 与 `/input` 全树扫描该文件。Agent 与验收容器无共享可写挂载。每个成功任务
  的 Agent 容器在导出证据后按 run ID 标签校验再清理；失败现场不作全局清理。
- Request 的 Agent/验收分别启动本地 httpbin 并断开容器网络。Agent 服务日志记录 97 条本地
  pytest 请求（含 `127.0.0.1:8765` 路由），还包含预期失败的 HTTPS-on-HTTP 探测；不继承宿主
  代理或 API 密钥。

## 较大公开测试的环境诊断

公开测试只用于定位镜像/平台适配，不影响保存补丁的严格目标验收结果：

| 任务 | 测试文件结果 | 复现观察 |
| --- | --- | --- |
| pytest-10081 | 71 项：61 通过、1 失败、9 跳过 | `test_plain_unittest_does_not_support_async` 期待未等待 coroutine 的 `RuntimeWarning`；镜像 Python 3.11.16 在更早位置抛出 `DeprecationWarning`，项目 warnings-as-errors 配置令嵌套测试失败。失败由 Python 行为/冻结依赖组合触发。 |
| Requests-1766 | 90 项：88 通过、2 失败 | `test_conflicting_post_params` 使用当前镜像 pytest 下不兼容的旧 `pytest.raises` 调用；`test_mixed_case_scheme_acceptable` 将本地纯 HTTP 服务地址改成 HTTPS，导致 TLS 握手打到 HTTP server（`WRONG_VERSION_NUMBER`）。后者是冻结服务协议未覆盖 HTTPS 的配方差异。 |
| Sphinx-10449 | 31 项：29 通过、2 失败 | 文档输出的 `documented_init` 目标和 default-options 测试在整文件顺序运行时失败；同一 `documented_init` 目标作为隐藏资格节点，在两个全新独立验收容器中各自通过。需调查顺序/模块级交互及镜像依赖，不得把 smoke/目标通过扩大为整文件稳定。 |

公开测试命令与完整 stdout/stderr 保存在轨迹的工具结果中；JUnit、审计 JSON 和插件副本保存在
对应 `test-evidence/`。没有通过
关闭 warnings、跳过失败节点、修改产品源码或增加外网访问来伪造整文件通过。

## 工程检查

- `tests/test_docker_backend.py` 与 `tests/test_builtin_tools.py` 聚焦 pytest 回归：58 passed。
  随后增加冻结输入文件哈希检查；本机 pytest basetemp ACL 又阻止 fixture 创建，故最终 6 个
  `test_docker_backend.py` 测试函数通过临时目录直接调用验证。覆盖调用 ID/事件顺序、RPC
  deadline、异常身份、丢失结果分类、源码 archive 仅暴露指定 commit、输入越界/哈希漂移，及
  现有补丁和测试工具。
- 对 Git 跟踪 Python 文件和本轮新增入口执行 Ruff：退出 0。`compileall` 对 `src`、`scripts`
  及相关测试文件退出 0。相关文件 `git diff --check` 退出 0。
- 全量 pytest 经两次尝试，均被 Windows basetemp ACL 影响。最终干净 basetemp 运行中 215 个
  测试体完成，284 个用例 setup 报错；其后 pytest `cleanup_dead_symlinks` 在枚举 basetemp 根时
  抛 `PermissionError: [WinError 5]`，退出 1。覆盖率 JSON 的 39.970527%（语句 44.614331%、
  分支 25.288600%）是大量 setup 错误导致的不完整统计，不能作为项目覆盖率；未达到也无法
  有效判断 90% 门槛。JUnit 与部分 coverage 数据原样移到 E 盘
  `engineering-checks/`。未改旧 ACL。
- 新 E2E 摘要记录 `model_requests=0`、`formal_experiment=false`、`holdout_used=false`；三道
  frozen manifest 哈希与运行后相同。Read-only run inspector 对三道完成态均核对成功，工具调用
  未解决数为 0。

## 当前资格与下一步

1. **正式 Harness 接通：是。** 三题都由正式 Runner 和 MinimalAgent 走容器工具桥；默认 Local
   路径不变。
2. **工具预算与故障处理：部分合格。** 参数准备先于 pytest 启动计额，进程启动事件和超时清理
   逻辑有回归；事件日志能显示结果未知并拒绝自动重放。实际断连/中断后在 Docker 中杀测试进程
   的端到端故障注入和自动续跑尚未验证，当前采取保守停机检查。
3. **三题环境与独立验收：目标节点/补丁验收合格，完整公开测试环境不合格。** 所有目标严格验收
   重复一致，但三道较大测试文件均有失败，不扩大任务范围。

后续先冻结与原 Windows 任务相匹配的 Python、pytest 和 HTTP/HTTPS 服务版本矩阵；为 Sphinx
增加单独目标与整文件运行的顺序对照。然后在 Windows 容器中用一次真实中断注入验证子进程组终止和
状态检查。TraceFix 全量 pytest 的 basetemp 权限问题需要一个全新可写临时根和隔离 Windows 账户
复查。以上门槛通过后再讨论扩大任务列表。可信 Token 计数契约仍是付费门槛，约 1M 真实请求未测。
