# G4 本地补齐与验收记录

用户要求：补齐第四项，只在本地修改和测试，不上传；环境限制如实记录，最后用通俗语言说明完成与未完成。
没有修改GitHub分支、触发CI、合并、发布或发起供应商请求。本轮在既有候选工作树继续，原项目源码未合入候选改动。

## 本轮补齐的实现

1. `tools/invocation.py`新增单次`PreparedInvocation`：固定检查后的参数，一次派发后不重放，进程启动最多计次一次，
   回调只绑定本次调用并在结束后恢复；没有prepare的旧工具继续使用原execute协议。Agent主循环、最终验证和bridge使用该约定。
2. `validation.py`新增`ValidationBackend`协议与local/Docker适配器及当前任务适配器。Runner负责装配，适配器不导入Runner，
   负责取得/释放checkout或容器，并在资源创建前校验策略。预算及通过资格仍由原Agent与审计决定。
3. `features.py`新增纯功能resolver及稳定原因码。RunConfig保留字段/路径形状校验，运行、preflight、doctor和恢复配置统一调用resolver。
4. `ToolDispatcher`加入受策略约束的准备与执行入口。独立验收获得明确来源和有限能力子集，策略升为v3，身份漂移继续拒绝恢复。
5. 修复ordinary Docker带回归目标时被旧local-only独立验收分支拒绝的问题；独立新容器现在验证主目标及全部追加目标，
   继承相同显式环境和执行profile，并在结束后清理。未将宿主目录或Docker socket暴露给任务容器。
6. doctor支持新的Skills快照及ordinary recovery组合，正确读取CLI/TOML/环境中的Skills开关，检查Serena前提；
   inspect profile不能通过doctor prepare运行工作区准备代码。
7. 本地workflow为editable追加与wheel同等的完整Docker/Serena组合及原始产物归档。本轮不上传、不执行该CI。
8. 工程检查枚举当前源码、测试、脚本中的获Git忽略规则允许的新文件，避免未提交的新模块漏掉Ruff和编译范围。
   新使用说明及后端矩阵见[能力策略](../capability-policy.md)。
9. Serena缺SDK、缺Docker CLI、镜像探测超时/启动失败或身份不符时使用稳定的配置错误与原因码。
   实际缺环境探针确认在模型初始化前停止，模型初始化和测试次数均为0。

## 验证与环境

- 相关Python3.12回归241 passed，原件`runs/g4-completion-integration3.xml/.log`；
  doctor/CLI最终修正后的相关集合102 passed，原件`runs/g4-completion-final-contract.xml/.log`。这些集合有重叠，不相加。
- 复用已有Python3.11.16环境的核心契约256 passed，原件`runs/g4-completion-py311.xml/.log`。
  另有不重叠的Agent/模拟MCP/bridge集合83 passed，原件`runs/g4-completion-py311-extra.xml/.log`；合计339项相关测试，不代表全量门。
- 本机沙盒内最小`asyncio.run(asyncio.sleep(0))`在10秒超时；在获准的沙盒外本地测试路径成功，确认是执行环境限制。
- 沙盒外能发现Python3.11.16；裸解释器缺pytest，已有wheel环境缺pytest-cov/coverage/Ruff/LiteLLM/MCP，
  因而只能报告上述核心子集，不能称双版本完整工程验收。没有联网补装依赖。
- Docker CLI存在，但Docker Desktop Linux engine的命名管道不存在。未启动Docker、拉镜像或伪造真实容器通过。
- 当前3.12环境也没有MCP SDK；本轮Serena测试使用模拟协议会话，不能据此证明真实SDK/镜像集成通过。
  实际测试版本在`runs/g4-completion-dependencies-py312.json`：Python3.12.5、pytest8.4.2、pytest-cov7.1.0、
  Coverage.py7.16.0、Ruff0.16.6、Pydantic2.10.1、LiteLLM1.75.5.post2；依赖锁未改动，本地既有版本不冒充CI锁环境。
- 环境探针原件在`runs/g4-completion-environment-unsandboxed.json`和`runs/g4-completion-environment.json`，分别对应沙盒外和沙盒内。
- Python3.12最终wheel及隔离editable环境均在候选仓库之外通过基础修复、回归反馈、记忆、连续对话、中断恢复五组回放；
  原件位于`D:\Tracefix\runs\g4-completion-outside-smoke`，最终前缀`wheel-current-`和`editable-current-`。
  另有Python3.11 wheel基础回放通过。editable环境复用系统已安装依赖，wheel通过全新target目录安装，未修改系统依赖。
  最终各场景报告入口汇总于`runs/g4-completion-smoke-summary.json`；各场景结果结构不同，按各自契约核验。
- 最终wheel位于`runs/g4-completion-wheel-current/tracefix_agent-0.8.4-py3-none-any.whl`，SHA-256为
  `426d9185bdeac9a7bbc512bb0a8c000124047e3899b35d6b9c97782a242963f9`；85项打包Python文件与当前源码逐字节一致，
  证据在`runs/g4-completion-wheel-current-source-check.json`。此前`wheel-final`是MCP错误分类修正前的包，不能替代最终包。
- 关闭组合开关时，与原工作区的默认五工具名称/顺序/参数schema相同，SHA-256均为
  `a7189b375a7a621eab151ddedb311f81026c2960e6773fa9271d3373f684cc08`；原件`runs/g4-schema-original.json`和`runs/g4-schema-candidate.json`。
- 低层新模块没有导入Runner、MinimalAgent或具体DockerBackend，静态核对原件`runs/g4-completion-boundaries.json`。
- 代码/测试/脚本/锁文件/示例/workflow等242项文件清单在`runs/g4-completion-source-manifest.json`，基底HEAD为
  `9a3d756a84c22ee17ff9d27959f2716c8ab2d493`，最终清单SHA-256为`20a01fe02bb1c35c5e139382bb855b2725e78ad2671b1a917e0b193fd3bec101`。
  该清单是本地未提交代码的身份，不声称存在新的Git提交。动态进度文档不计入此代码清单。
- Python3.12全量原始运行1683 passed、0失败/错误、1 skipped，耗时1901.78秒；
  原件`runs/g4-completion-gate-py312/`，Ruff、编译、提交差异检查返回0；另行对当前未提交差异执行`git diff --check`通过。
  跳过项为`test_real_path_aliases_cannot_reach_dotenv_or_git_metadata`的Windows真实符号链接创建权限不足（WinError1314），不是通过。
- 全量运行期间仅`mcp_serena.py`及其测试发生最终错误分类修正，前一清单另存
  `runs/g4-completion-source-manifest-before-mcp-delta.json`（SHA-256 `f1f52af1ba8b89af149d1db8f523ff45feec42729292ec8975c0febf8dd6f38c`）。
  逐文件比对确认其他源码未改变。全量summary保留原`accepted=false`及`23267/25908=89.80623745561216%`，没有覆盖或改写为通过。
- 对最终代码另跑80项相关回归，全部通过（`runs/g4-completion-final-delta.xml/.log`）。
  在独立目录复制全量CoverageData，清除发生变化的`mcp_serena.py`旧行号数据，再用同一80项集合重新测量并追加，
  保留其余未变源码的全量数据。最终covered lines/statements为`17730/19240`、covered branches/branches为`5588/6668`，
  精确综合`23318/25908=90.00308784931295%`，达到原90%门槛；这属于“全量＋最终增量”的本地证据，不声称新HEAD完整重跑通过。
  原件`runs/g4-completion-current-coverage/`；两轮JUnit去重合计1685 passed、0失败/错误、1 skipped，不将重叠集合累加。
- 最终MCP修正另在3.11通过19项回归（`runs/g4-completion-py311-mcp-delta.xml/.log`），与此前339项有重叠。
  汇总、原始证据哈希及阶段未通过原因在`runs/g4-completion-local-verification.json`，生成脚本`runs/g4_finalize_evidence.py`。

## 中间失败与修复边界

首轮相关回归16失败/157通过：15项是沙盒TEMP拒绝访问，一项是bridge旧夹具缺少新显式工具spec。
将测试临时目录定向到候选runs、修正夹具后相关集合通过。另有新单测先错误地把reason_code当错误正文匹配，
改为核验结构化context；没有放宽策略。两次准备轮完整运行因继续发现配置测试/doctor接口不一致而主动中断，
不作为通过或环境挂起证据；最终完整门使用单独目录。早期wheel构建同样遭沙盒TEMP拒绝，离线本地构建路径解决，保留失败日志事实。
最终MCP新增测试第一次Ruff发现一处长行，已换行修正并重新通过，未改检查规则。

## 当前退出条件

本地已知接口与组合代码缺口已补齐，3.12全量＋最终增量证据达到覆盖率门槛，仓库外editable/wheel本地回放通过。
G4完整阶段门仍需要Python3.11全量工程、可创建真实符号链接的平台证据、
editable/wheel完整Docker＋Serena组合及真实容器销毁重建回放；环境缺失的项目只能记为未验证。
恢复环境后先固定最终候选源码身份，重新执行双版本完整工程和真实组合，不借本轮增量证据宣称新提交已验收。
不使用旧G0—G3 CI或历史19/30证明本轮G4通过或模型收益。
