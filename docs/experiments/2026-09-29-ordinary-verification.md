# B 批：准备式预检与独立公开测试复跑

日期：2026-09-29。基线为 A 批 `fa4b84265a71af90830826da7576533ca4bc22e0`，分支 `codex/ordinary-validation`。本机 Windows 11、Python 3.12.5；所有模型相关行为仅使用录制响应，供应商调用为零。未访问留出题，不改历史评分或账本。

本批增加 `doctor --prepare`：复用 Runner 克隆与源码导入探针，在临时 checkout 中执行预检并清理；不构造供应商客户端。`tracefix verify --run` 在新的临时 checkout 核对配置、源码提交、补丁和测试环境身份，应用保存补丁，隔离复跑记录的公开 pytest 目标并保留测试审计。报告只在独立验证身份匹配时显示复跑结果。普通运行可配置显式审核的本地 Skills 目录，TOML 可提供预算、上下文、请求视图记录及 Skills 配置，优先级为 CLI、环境、TOML、默认值。

当前原始证据：

- `python -m pytest tests/test_onboarding.py tests/test_recovery.py tests/test_report.py tests/test_skills.py -q -x -p no:cacheprovider --basetemp E:\TFP\b-preflight-verify-final2-20260929`：58 passed，退出 0。包含带空格路径、`src/` 布局、缺失依赖、误导入已安装包、独立补丁复跑和报告身份负例。
- 对 A 批公开离线恢复产物执行 `tracefix verify` 等价 CLI 入口，返回 0，隔离复跑 `1 passed`，产物 `runs/replay-resume-b-20260929/runs/20260929T053544Z-df11ead0/independent-validation.json`；这是本机离线 harness 行为，不是供应商模型自主修复证据。
- 完整本机工程门槛：`python scripts/check_engineering.py --output E:\TFP\engineering-ordinary-b-final-20260929 --python python --diff-base fa4b84265a71af90830826da7576533ca4bc22e0`，退出 0；`summary.json` 记录 `accepted=true`，720 passed，纯 pytest 分支覆盖率 90.14391626689925%，Ruff、compileall、Diff 均退出 0。该检查完成后仅补充了 wheel CI 的 `doctor --prepare` 与 `verify` 调用和本记录，无生产代码修改。

仍待 Windows 双版本和仓库外 wheel CI；真实模型验收须另列付费方案并获授权。独立复跑只验证记录的公开测试，不是隐藏测试或正确性证明。C、D 尚未开始。
