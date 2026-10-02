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

## 第一批收尾

PR #10 head `5a518067889031222da7d2350a452c631883cd5d` 的 [CI #77](https://github.com/wingpeng30/Tracefix/actions/runs/36966132365) 五 job 全通过。Windows 3.11/3.12 各 799 tests，0 failures/errors/skips，纯 pytest 13621/15126 = 90.05024461192649%；Ruff、compileall、Diff、editable、仓库外 wheel 新场景及 Linux 三个容器 job 通过。merge commit `0954c4fc638eb6f43f7fe6403b11fa099af096f4`。

最终本机 wheel SHA-256 `87fecc8979c4fb38caed9452224b53a52aa280923bceadd82698d7c30bd74ee7`，安装环境与 cwd 完全在仓库外，产物 `runs/package outside final/reproduction.json`，原源码未变。较早 `runs/package wheel feedback` 的环境/cwd 位于仓库内的 tmp，虽然导入确认来自 site-packages，但严格仓库外证据应采用最终运行与 CI。其后 inspect 和重复 verify 通过。

本机全量工程检查 `runs/engineering-package-py312/summary.json` 于 830 秒结束，pytest 未完成且执行中有后续代码变化，不作为成功或冻结验收；Ruff、compileall、Diff 通过。精确 CI 才是工程门槛依据。

## 第二批执行证据

实现提交 `cb7e43f62d568cf8fcc8b8cbdd6261c0111b1007` 在干净工作树完成 Windows Python 3.12.5 资格矩阵，`runs/qualify more itertools py312 final/qualification.json` SHA-256 `26e82f869e38c8e27e26cf86f7f06e985efe939b9750df28e9f4b5fe6e16dbd5`。故障/参考各两次、错误补丁诊断及生产参考回放均通过；原上游未变，派生任务提交 `306f7ac169f07540968fd7ad5ac55e915d8985a0`，上游 base 单独保存。任务 manifest SHA-256 `64a8f8d524e7de11b9fdd0dd3a576aba9966a520dab7d51646f5cf60d21acaf4`。

参考回放两次模拟请求、三次 Agent 内 pytest、结束前门通过；双 checkout 独立验证、报告与补丁导出通过。完整 stdout/stderr、JUnit、源码状态、解释器/依赖指纹、退出码与产物哈希在各 case/运行目录保留。早期 `py312 01` 临时目录保护失败、`py312 02` 初步成功均保留。后续跳过/errors fail-closed 检查与最终双版本资格以精确 CI 为准。

本机 Python 3.11.16 缺 `pydantic`，未启动资格脚本，不自动安装或更改其环境；3.11 支持结论等待锁定依赖的 CI。新增七项任务包/故障测试通过。当前只有一个资格化任务，不能宣称已建立多类型任务集，不能把参考补丁回放写为模型自主修复。未访问留出题、未修改历史评分或账本、未续用八次付费授权。
