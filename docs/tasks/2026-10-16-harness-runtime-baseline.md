# G5—G8起点核验完成（2026-10-16）

实施工作树D:/Tracefix/runs/harness-foundations-worktree，分支codex/harness-runtime。
验收实现提交[ac64510](https://github.com/wingpeng30/Tracefix/commit/ac64510ffd98ee931a4bc3d028d8cf72bc97ff43)，
[CI #159](https://github.com/wingpeng30/Tracefix/actions/runs/38773570888)所有5个job通过。

## 修复和证据

- CI157实际发现资格脚本_test缺dispatcher，以及ordinary在RUNNER_TEMP用相对docker构建路径失败。
- 资格脚本的测试和产品补丁均经过独立验收策略；显式local配置，未通过可选参数省略策略。
- 两个Serena构建入口均使用GITHUB_WORKSPACE绝对上下文，新增实施分支CI触发范围，不删原检查或改锁。
- 新增真实小仓库基线/补丁两项用例先失败复现，再修复；双Python相关35项均通过，Ruff/编译/diff通过。
- CI159 Windows3.11.17/3.12.10各1694 passed、0失败/错误/跳过；精确综合覆盖率23358/25931=90.07751340094867%、23356/25931=90.06980062473487%。
- 两版完整工程、仓库外editable/wheel、冻结公开任务资格及安装包恢复通过；基础Docker、ordinary和Serena真实job通过。
  ordinary包含wheel/editable真实两镜像组合、恢复/身份故障和资源清理，供应商调用0。
- 本机Python3.11.16/3.12.5各1693 passed、1 skipped、0失败/错误；精确覆盖率23368/25931=90.11607728201766%、23366/25931=90.10836450580386%。
  跳过仅为Windows符号链接WinError1314，同一当前用例在CI无跳过通过；本机Ruff/compileall/diff通过，两份summary.accepted=true。

## 验收身份与原件

当前验收源码清单742文件，SHA-256 `5651b624a74d44c7bdd05197e7c97bfd430ab3f0ee975bcde426e72bd9e97def`，原件`runs/harness-runtime-start/accepted-source-manifest.json`。
本地完整工程`D:/Tracefix/runs/hre311`及`hre312`；定向/CI原始日志、job及artifact元数据、汇总收据在候选`runs/harness-runtime-start/`。
GitHub五份artifact的ID/digest见该目录ci159-artifacts.json；没有把旧wheel/旧CI借来证明此提交。

推送首轮自动审批引用早期不上传限制；核对新授权及9文件无凭据清单后，同一审批批准原推送，未换渠道绕过。
Docker宿主Linux29.8.2可用；沙箱内管道访问拒绝不能解释为daemon未运行。原工作区源码未合入候选。

## 退出与下一阶段

起点门关闭，允许开始G5.1；G5尚未交付，G6—G8未开始。计划见[完整实施清单](2026-10-16-harness-runtime-implementation.md)。
本记录与进度同步仅为文档修改，验证归属于实现提交ac64510，不声称后续文档HEAD重新执行完整门。
没有供应商调用、合并main或发布。
