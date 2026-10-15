# G4 环境补齐与本地完整验收

用户授权：启动Docker，联网下载必要依赖和镜像，继续G4验收；不上传项目、不触发CI。
本轮沿用隔离候选工作树`D:\Tracefix\runs\harness-foundations-worktree`，基底HEAD
`9a3d756a84c22ee17ff9d27959f2716c8ab2d493`；原工作区源码保持原状，不调用付费模型或重发历史请求。

本轮结论：G4已完成本地功能与工程验收。双版本完整检查、真实editable/wheel普通Docker＋Serena组合、
必要故障回放及仓库外安装检查全部通过；Windows同一符号链接用例因权限跳过，已有当前源码的Linux实际补证。
没有上传、推送镜像、触发CI、合并、发布或创建G4提交。原计划要求的远端候选CI退出项仍未执行，
不能把本地结论写成“某个新提交已经通过CI”。

## 顺序、目标和原始证据

1. 启动并核验Docker Linux engine。已完成：本地Docker Desktop启动后，`docker info`返回Linux/29.8.2，
   context端点是本机`npipe:////./pipe/dockerDesktopLinuxEngine`；不是远程构建器。
2. 建立隔离Python3.11完整工程环境，按现有锁文件安装，并补齐MCP SDK；同时准备独立3.12工程环境。
3. 通过本地最小构建上下文准备ordinary任务镜像和Serena镜像，核验不可变ID与运行前提。
4. 固定当前源码清单，执行双Python全量工程门、仓库外editable/wheel、本地真实Docker/MCP及完整组合恢复回放。
5. 已按实际结果更新本报告、current-status、roadmap、development-history；没有原始证据的检查不记通过。

初始源码清单沿用`runs/g4-completion-source-manifest.json`，SHA-256
`20a01fe02bb1c35c5e139382bb855b2725e78ad2671b1a917e0b193fd3bec101`。
本轮新证据放在候选目录`runs/g4-environment-acceptance/`，安装环境及仓库外回放放在`D:\Tracefix\runs/`。
历史增量覆盖率与G0—G3的CI不替代本轮验收。下列记录保留定位过程，本轮最终结论以本报告的完整门与原始证据为准。

## 已定位并处理的问题

- Windows安装MCP时，原`mcp-py311.txt`只锁Linux依赖，缺`pywin32>=310`，带哈希安装正确拒绝。
  增加Windows x86-64/Python3.11 companion锁文件，包含原锁并固定pywin32 312及下载wheel哈希。
  双工程环境和独立MCP环境均通过pip check；工程环境保持原锁版本，不混用MCP较新pytest/Pydantic。
- Python3.11全量收集发现桥接测试f-string中`!r`后空格不兼容，已修正；初次收集失败原件保留。
- 完整组合预置的用户决策记忆缺少存储证据信封字段，首次重读被拒绝。
  回放夹具改为显式无测试、无工具结果及空补丁指纹，没有放宽记忆校验；新增重开/召回契约。
- Windows checkout的Serena启动脚本使用CRLF，Linux容器不能执行shebang。
  规范为LF，并增加精确路径的.gitattributes规则；重建镜像后真实MCP查询成功。
- 真实组合随后发现TaskState结果被Presenter通用裁剪包成字符串，丢失下次更新所需revision。
  Presenter及上下文二次裁剪现保留revision/工作状态角色/新增条目ID，正文仍有界；113项相关回归通过。
  因运行源码发生变化，主动中断尚未完成的两版完整检查并清理所属子进程；不会使用旧覆盖率作为最终门。
- 固定Skills恢复检查的elif分支把“已从snapshot恢复”的Skills误报为关闭，修正为先区分是否启用。
  82项相关回归通过，新增契约证明活目录变化不会影响原快照的恢复资格；随后Ruff长行修正通过。
- 离线组合原先只给6次测试预算，两轮实际需要1次baseline＋2次Agent测试＋两轮各2个验证目标，共7次。
  实际执行在第6次后正确中断；保留该负向记录。仅将组合夹具设为8次明确上限，不修改产品预算或跳过验证。

## 已完成的真实本地检查

| 检查 | 结果及原件 |
|---|---|
| 固定版本工程环境 | `g4-accept-py311`/`g4-accept-py312`，pip check通过，版本与源码身份见`environment-frozen-py311/py312.json` |
| MCP集成环境 | `g4-accept-mcp-py311`，MCP1.28.1/pywin32 312及原MCP锁版本，pip check通过；未改动系统Python |
| ordinary镜像 | `sha256:ce624b4e2895a687e79892290164131d91b621bf4e170b58b783832607a422af`，Python3.11.16/pytest8.4.2，`ordinary-image.json/.build.log` |
| Serena镜像 | LF修正后`sha256:bbcdf8388446bb1d99034efcaeef862b11fd4b8507c12eb609c9e4fbc6c42eee`，`serena-image-lf-build.log`及ID文件 |
| editable完整组合 | 通过，16次合成请求/0供应商调用、7次累计测试、2次真实Serena查询、第二轮完成、独立验收主目标与追加目标通过，原容器及MCP容器清理；`composite-editable-summary.json` |
| wheel完整组合 | 同上全部通过；宿主实际导入隔离wheel，`wheel-runtime-path.log`及`composite-wheel-summary.json` |
| 基础Docker/Skills | 同一最终源码镜像双臂通过，隔离与清理审计`basic-ready-audit.json` |
| ordinary恢复 | read/patch/test/twice四种独立新进程/新容器恢复及快照故障全部通过，`D:\Tracefix\runs\g4-acceptance-outside\recovery-faults\recovery-summary.json` |
| 产物与身份故障 | 删除、模式、用户自有cache、工具/解释器身份漂移、替代根链接拒绝通过，`recovery-products/acceptance.json` |
| 超时/断线/清理 | 通过，`ordinary-failures/failure-audit.json`；路径基准同上仓库外目录 |
| Windows符号链接补证 | 没有改Windows开发者模式或系统权限；同一个测试在受限Linux容器1 passed、0 skip，`linux-symlink.junit.xml/.json`，容器已清理 |
| 仓库外安装回归 | 双Python分别通过editable/wheel的基础、回归门及wheel恢复，共10组，`py311/py312-*-*.log`及仓库外对应reproduction.json |

最终wheel为`wheel-ready/tracefix_agent-0.8.4-py3-none-any.whl`，SHA-256
`61b174bc770296f0f16b3de60aeeae114c5effdb1065a0a1a1e87f51c13859bc`；85项Python文件与当前源码逐字节一致，`wheel-ready-identity.json`。
基础镜像为`sha256:247102ff7b31aea574ec8fbcc140da0b204c135d8f88a44f6ba0dc9c5e5a1576`。
两版全量启动时使用同一清单SHA-256 `ce7618dbd4ac0828e17a91ecc93529f0a07aa84d28e14ecfb90697c9c89ea6ce`，
本轮清单新增纳入根docker构建输入及.gitattributes；不与上一轮242文件清单直接比数量。
全量初次固定目录过深，遇到本机Windows虚拟环境的长临时路径问题：265字符的TEMP/TMP令最小`print(123)`超时，
同一解释器使用短临时目录正常；长HOME或长cwd配短TEMP也正常。原件`long-path-python-probe.json`、
`venv-minimal-environment-probe.json`和`venv-long-temp-probe.json`保留定位过程。
该故障导致离线严格验收的收集子进程300秒超时；主动中止`engineering-frozen-py311/py312`并清理所属进程，
没有修改最小环境白名单、测试资格或超时门槛。改为短输出目录`D:\Tracefix\runs\g4e311`和`g4e312`从头运行全范围检查，
源码清单仍为同一`ce7618...`，环境与清单保存为`environment-short-py311/py312.json`及对应source-manifest。
这两版短目录全量已完成，原始失败与被中断运行未覆盖。汇总脚本`runs/g4_environment_report.py`
生成`acceptance-summary.json`，最终`local_acceptance_passed=true`，`ci_executed=false`、`project_uploaded=false`。

## 最终完整工程门

| 解释器 | pytest | 行计数 | 分支计数 | 精确综合覆盖率 | 原始产物 |
|---|---|---|---|---|---|
| Windows Python3.11.16 | 1691 passed、0 failed/error、1 skipped | 17763/19253 | 5605/6678 | 23368/25931 = 90.11607728201766% | `D:\Tracefix\runs\g4e311` |
| Windows Python3.12.5 | 1691 passed、0 failed/error、1 skipped | 17762/19253 | 5604/6678 | 23366/25931 = 90.10836450580386% | `D:\Tracefix\runs\g4e312` |

两版pytest进程均返回0，Ruff、编译、工程脚本diff检查均返回0，summary accepted=true。
另保存当前未提交工作树`git diff --check`返回0（`working-diff-check.json/.log`），避免仅比较HEAD冒充工作树检查。
全量pytest耗时分别1582.014秒、1608.406秒。两版唯一skip均为
`test_real_path_aliases_cannot_reach_dotenv_or_git_metadata`的WinError1314；没有其他不明跳过。
同一用例在当前受限Linux容器中1 passed、0 skipped，不声称Windows权限限制已解除。
两版完整覆盖率分别从各自干净数据生成，没有拼接上一轮数据或使用SDK环境较新的Coverage.py。

258项冻结源码/测试/构建输入逐文件复核没有漂移；清单SHA-256仍为
`ce7618dbd4ac0828e17a91ecc93529f0a07aa84d28e14ecfb90697c9c89ea6ce`。
最终汇总记录原始工程summary/JUnit、组合、故障、镜像、环境及安装包哈希；复现与使用说明见下节。
目前剩余边界是远端CI按用户指示未执行、真实Windows符号链接权限未改变，以及Windows深临时路径仍需避免。
这些不覆盖或抹去本地实际通过的结果，也不等于原工作区源码已经合入候选实现。
验收结束后只更新进度/使用文档及忽略目录汇总，运行源码没有再改动；没有新HEAD重新验收的主张。

用户追加要求：补做G0—G3因本机Docker缺失而未做的最必要实验。历史G0—G3真实CI均通过，
因此只补当前代码的基础Docker/Skills、ordinary恢复故障及完整组合；不重启历史付费或整批实验。

## 复现与实现范围

本轮直接修改3处运行源码：`agent/presentation.py`、`context/manager.py`、`docker_recovery.py`；
另修复回放示例、5个相关测试文件、Windows MCP依赖锁与入口脚本换行。上一轮实现不机械重做。
清单公共路径差异在`source-delta-from-local-completion.json`；新增纳入清单的14个原有docker输入不算作新实现。

使用本轮专用MCP环境及准备好的镜像，可在新的输出目录重放完整组合：

```powershell
Set-Location D:\Tracefix\runs\harness-foundations-worktree
& D:\Tracefix\runs\g4-accept-mcp-py311\Scripts\python.exe `
  examples/replay_artifact_recovery.py --composite `
  --image-id sha256:ce624b4e2895a687e79892290164131d91b621bf4e170b58b783832607a422af `
  --mcp-image-id sha256:bbcdf8388446bb1d99034efcaeef862b11fd4b8507c12eb609c9e4fbc6c42eee `
  --output D:\Tracefix\runs\NEW_G4_REPLAY
```

`NEW_G4_REPLAY`必须不存在。响应为脚本替身，供应商调用为0，不证明真实模型自主修复率提高。
本轮专用工程环境中MCP未安装是有意保持engineering锁，真实MCP使用第三个隔离环境；不能把系统默认python当作该环境。
构建发生在本机Docker Desktop端点，镜像只保留本地；没有push、GitHub写入或CI调用。
容器清理审计`container-cleanup-audit.json`确认本轮没有待清理容器，其他历史停止容器保持原状。
