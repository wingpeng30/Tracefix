# 分页演示与离线报告开发记录（2026-09-28）

## 范围与身份

- 起点：`8353395e1a203f8c1ba73ab2cc9d965738dc2553`，分支
  `codex/p0-checkpoint-p1-review`；最终候选提交与 CI run 待记录。
- 合成任务：`tracefix-synthetic`，分页场景；独立 Git 源 commit
  `d4ceb97aee33151c74b147d443a2cb2057c61a4a`。
- 环境：本机 Windows 11、Python 3.12.5；本地 backend，脚本模型
  `offline/scripted`。Docker daemon 在本机不可用；Linux CI 结果待记录。
- 配置：baseline 为 Skills 关闭；Skills-only 仅启用内置
  `tracefix-debugging`。同一脚本工具序列；零供应商构造、请求与网络连接。

## 实际改动

新增多文件分页合成案例、`tracefix report` 离线 HTML、真实性与安全测试、
Docker 审计及 CI 工件路径；README 和复现文档加入最短运行命令与讲稿。
报告从保存的 `result.json`、`trajectory.jsonl`、`patch.diff` 读取，
不新增评分或伪造模型推理。

## 已验证结果

- 本地两臂输出：`portfolio-demo-baseline-2/`、`portfolio-demo-skills-2/`。
  两臂均完成；第一次公开测试失败、第二次通过；仅修改
  `catalog/pagination.py`；Diff SHA-256 均为
  `4118325c94c9d2d5a491b6e841ad0adc20f438ac25f245d36804ca5e3bb04060`。
  脚本请求分别 8、9 次；供应商构造、供应商请求、网络连接均为零。
- 首轮本机全量工程入口：`portfolio-engineering-1/summary.json`。
  650 项通过、无 skip；纯 pytest 精确覆盖率
  `11383/12650 = 89.98418972332016%`，未达 90%，因此入口判失败。
  pytest、Ruff、compileall 与 Diff 原始返回码均为 0。该检查在新增
  报告边界测试前开始，不能代表最终候选。
- 报告与 Docker 审计定向测试：首次 17 项通过；随后新增的边界测试
  发现夹具空行，已修正并单项通过。最终全量结果待候选 CI 记录。
- GitHub [CI #19](https://github.com/wingpeng30/Tracefix/actions/runs/36427092361)
  对应分支提交 `d8d32f3f1ed41c55a7fa57735e6efc6646a675d0`，PR 检出合并提交
  `bbc7de22d9a7e9cb411eef7b92820bcbf8d66596`。Linux Docker smoke 与
  分页两臂及隔离/清理审计通过；Windows 3.11 已确认 652 项通过、无 skip，
  覆盖率 `11379/12650 = 89.95256916996047%`，工程门槛失败。
  Windows 3.12 的覆盖率及安装检查以该 run 原始工件为准。新增报告错误路径测试后
  已定向验证 7 项通过，新的完整 CI 待记录。
- GitHub [CI #20](https://github.com/wingpeng30/Tracefix/actions/runs/36428036476)
  对应分支提交 `b5456fb212961835134790db7a34395d477346c6`，PR 检出合并提交
  `65f9c3823ff30f13f76a005287b6c9d27a66d261`，全部三个作业通过。
  Windows Python 3.11.9 和 3.12.10 均为 654 passed、0 skipped，纯 pytest
  `11408/12650 = 90.18181818181819%`；Ruff、compileall、Diff 检查通过。
  editable 与仓库外 wheel 的 smoke、分页 baseline 和 Skills-only 均通过。
  Linux Docker 原 smoke 与分页两臂、隔离/清理审计通过。原始工件：
  [3.11](https://github.com/wingpeng30/Tracefix/actions/runs/36428036476/artifacts/10972482328)、
  [3.12](https://github.com/wingpeng30/Tracefix/actions/runs/36428036476/artifacts/10971753047)、
  [Docker](https://github.com/wingpeng30/Tracefix/actions/runs/36428036476/artifacts/10972177406)。
  三份 ZIP 的 SHA-256 依次为
  `9d35dec4bba0b1978a2561869591da4c0eb9f6dd6a680bb7a15eee6baa7c7ce5`、
  `f1b479f3d311fcac34b80d289bcf8c77d9be83c4ba7ba5dc47fd1f92170f4f4c`、
  `2915a8e303c3717c9dccbea4d66238b39c160dd02a0231a13deaba3349ad773c`。
  截图为本地 Skills-only 报告的真实首屏，存于 `docs/assets/pagination-report.png`。

## 限制与下一步

本地 Docker daemon 不可用；真实 bridge 与 Windows 双版本门槛由候选
GitHub CI 验证。Codex 浏览器安全策略拒绝 `file://` 页面；用户在本地浏览器
打开报告并提供了真实首屏截图，已加入 README。折叠面板、长日志和 Diff 的
浏览器交互未由本会话直接观察；自动化证据读取测试覆盖其内容与转义。
该演示只证明合成控制流和工程可检查性，未运行真实模型，也没有独立验收；
不据此声称真实修复成功率提高。
