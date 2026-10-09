# 2026-10-09 A/C 实验续跑交接

用户最新批准：供应商超时或响应未知计为失败，保留最高预留、永不重发，继续其他任务。
身份、计数、证据或工具异常仍暂停。首要目标尽快完成；不重复已有未变化的资格验收。

## 当前真实进度

同题 A/C 各 30 次，共 60 次；已启动 23 次，已结束 21 次，主指标成功 12 次，
两次未知响应永久不重发。还剩 37 次（024–060）。旧两批分别报告，不能称原协议完成。

- 原批 `campaign`：5 启动、4 结束、4 成功；005 A pytest #7324 未知，预留 5.104606。
- 第一续批 `continuation-campaign`：18 启动、17 结束、8 成功；023 A Requests #6028 未知，预留 3.571850。
- 已知保守费用 57.848134，未知预留 8.676456，合计 66.524590；300 元授权余额 233.475410。
- 这不是账户实际余额。最近余额读取在这 118 次续批已知请求之前，须重新只读查询。

全部原始数据在 `E:\TracefixExperiments\20261009-ac-30-180s`，`storage-owner.json` 标注清理范围。
两个批次 `summary.json`、`report.md`、逐运行/逐任务表与索引已生成。勿改旧账本或未知成本。

## 新控制器已写好但未冻结、未运行付费

未跟踪文件 `scripts/complete_comparison.py`、`tests/test_comparison_completion.py`。
新控制器使用每次运行独立 sealed ledger，将所有已知成本与未知最高预留绑定到下一次预算。
原 engine/product 不变。后续 provider unknown 可验证已收集补丁，状态仍为 unknown，主指标失败。
保留 `agent-unknown.json` 与 pending 原始账本；不重发。其他异常仍停止。
按块只读查询钱包，实际不可用/零余额停止，没有额外最低余额门槛。

最新本地验收：9 项全通过，真实补丁与独立 pytest、未知故障注入、保留费用、续跑与不重发、
记录重入、重复授权复制拒绝、并发锁、损坏/预算/不完整工具状态拒绝；Ruff 与 Diff 通过。
证据 `completion-tests-v3`；v1/v2 失败保留。当前没有新的付费请求。

下一步：完成新控制器 37 次零调用离线完整排程与独立进程重入；提交仅新文件与必要文档，
推 `codex/ac-30-180s`，精确 CI 五门通过，再冻结新的 controller worktree/approval，续跑 024–060。
原全量真实 30 题资格、60 次与55次旧演练、官方 tokenizer212请求校准均已通过，不需重复。
新精确 CI 仍必须覆盖 Windows311/312全量、未取整覆盖率≥90%、wheel/editable、Linux Docker/MCP。
尚未创建新正式 PR。

## 冻结代码与调用方式

- 当前主 HEAD `dccd30fcb440cb2cfd03602c3d4a3b1977953861`，分支 `codex/ac-30-180s`。
- 不碰用户已修改的 `docs/development-history.md` 与 `docs/interview/README.md`。
- immutable engine：`C:\Users\PengZixuan\.codex\worktrees\ac-thirty-frozen\Tracefix`，HEAD `e602c74d707e33d72cfd7cd8d832a8b0198a6a2d`。
- immutable previous driver：`C:\Users\PengZixuan\.codex\worktrees\ac-continuation-frozen\Tracefix`，HEAD dccd30f。
  `scripts/continue_comparison.py` CRLF SHA `0789afdc4ea75801206db0855e73238ae797b7a089f0649f00ecb68547fbee96`。
- 两个已冻版本精确 CI 五项全通过，收据 `ci-evidence-e602c74.json`、`ci-evidence-dccd30f.json`。
- 所有真实执行用 D `.venv\Scripts\python.exe`，显式 `PYTHONPATH` 指向 immutable engine `src`。
  本地新测试显式 `PYTHONPATH=D:\Tracefix\src`；venv installed package 是旧版本，不可依赖默认导入。
- 新 approval kind `tracefix_unknown_terminal_suffix`、future_provider_unknown `fail_hold_reserve_continue`，
  parent `continuation-campaign`、协议/请求 rawSHA、pending `023-01:3.571850`、user instruction、
  parent_driver 上述 frozen路径/SHA、driver_ci_evidence 新精确 CI 收据路径。准备时只允许 canonical parent claim 一次。
- 新控制器 `plan` 用旧 immutable driver 对原 anchor 的 plan 重建上一续批，核验全部身份；再取未开始37次。
- 主报告生成 `complete_comparison.py report` 复制 raw segment evidence供未改 engine reporter 使用。
- 保留 owned httpbin 服务：8768，nonce `a76cace3c7c34b8b943f28e5b27762f0`，Python PID115048，勿提前杀。
- official tokenizer V4.1 与 recipe0.1.1不变；单请求180秒、单题3600秒、单题10元、总300元。
- git push 禁用坏代理：`git -c http.proxy= -c https.proxy= push origin codex/ac-30-180s`。
- 普通 sandbox PowerShell 常卡住；E读写/网络/git用明确授权的 require_escalated。

新批次须单独报告；全部60次可以描述性列明各协议变更，不能冒称同一原冻结协议全部完成。
