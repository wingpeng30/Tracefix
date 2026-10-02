# 安装包闭环与公开任务资格

开始基线：PR #9 merge `b18958b8342b0642b7f4fe828c39852ada769319`。保留开始时的四份文档修改及历史未跟踪材料；只逐文件提交。全批零供应商调用。

## 第一批实现

包内 `tracefix.regression_replay` 使用录制客户端、真实 LiteLLMAdapter 解析、生产 TOML 参数解析、Runner、工具、回归门及独立验证。第三次请求必须含两个回归失败，否则客户端拒绝返回修复。使用六步、八次测试、300 秒预算。原源码保留在输出的 `source/`，运行在 `runs/`，自动报告及导出。

命令：`tracefix-reproduce --scenario regression-feedback --output "runs/regression feedback"`。仅本地；Skills、Docker、image ID 被拒绝。usage 为模拟值、成本未知。示例脚本同一入口委托调用，Windows wheel CI 从仓库外使用安装命令。

本机早期两次运行因沙箱临时目录权限被拒绝而失败，记录保留在 `runs/package regression feedback 01`、`02`；第三次经授权执行通过，记录在 `runs/package regression feedback 03/reproduction.json`：四请求、八测试、门通过、独立验证通过、原仓库未变。此时工作树有修改，不能作为最终干净提交证据。

## 第二批边界

more-itertools #462，冻结故障提交 `8a27da60d70fbfc348b124ce89733d8a7104977a`，参考 `db4630bc3175d6a884fb07092c645417c9e62d49`。准备只增加测试。两次故障/修复对照、诊断错误补丁及生产参考回放均通过后才称资格通过，不写成 Agent 自主修复或效果证据。

完整运行目录不提交。后续新增真实模型实验需重新取得次数、金额与停止条件授权。

第一批提交 `56bb47895985dc396772a1361d269319567f447d`，PR #10。仓库外安装 wheel 后实际通过，产物 `runs/package wheel feedback/reproduction.json`；wheel SHA-256 `14c160647473e9e3021e7db31b99377e125d6dbd4dbd654df8f966b3ad918c43`。七项新增测试通过，旧演示兼容测试十四项通过、其中新配对断言修正后已通过。全量工程与精确 CI 尚待最终核验。
