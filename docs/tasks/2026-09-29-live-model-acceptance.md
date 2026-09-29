# 当前版本真实模型最小验收预案

状态：**未授权、未运行**。此预案只规定一次公开 fixture 的付费调用边界；离线回放及 CI 不能替代真实模型验收。执行前重新确认模型可用性、单价、测试命令和工作树提交身份，并取得用户对费用的明确授权。不得访问冻结留出题。

- 任务：使用 `examples/replay_ordinary.py` 生成的公开 `widget.py` 分页 fixture；`source` 为干净 Git 仓库，公开测试为 `tests/test_widget.py`。以新运行目录执行，原仓库保持不变。第一次仅用本地受信任后端，禁用 MCP 与 Docker，以隔离模型链路。对照同 fixture 的离线回放结果，二者分别标注。
- 模型：`deepseek/deepseek-flash`。官方当前 API 名称为 [`deepseek-flash`](https://api-docs.deepseek.com/guides/harness)；本地 LiteLLM 解析返回 provider `deepseek`、model `deepseek-flash`，尚未构造供应商客户端或发送请求。
- 次数：**仅 1 次 Agent run**；`--llm-max-retries 0`，`--max-steps 10`，`--max-test-runs 2`，`--llm-timeout-seconds 45`，`--wall-time-seconds 300`。任何鉴权、协议、模型返回或计费异常均停止，不自动改模型或重跑。
- 预算配置：`--max-input-tokens 12000 --max-output-tokens 2000 --per-request-output-tokens 512 --context-window-tokens 6000 --context-trigger-tokens 4800`。这是 TraceFix 的请求前估算与响应后用量检查，**不是供应商计费硬上限**；单次请求可能超过配置估算。执行前按[供应商实时价格](https://api-docs.deepseek.com/quick_start/pricing/)核算并记录用户批准的最高费用；若要求绝对硬上限，先配置供应商账户侧限制，否则不运行。
- 凭据：只从进程环境或指定 `.env` 读取 `DEEPSEEK_API_KEY`，不进入 Git、TOML、运行报告或实验记录。预检执行 `tracefix doctor --prepare --json`；不发送模型请求。
- 可观测性：运行时显式启用 `--record-request-views`，保存脱敏有效配置、`result.json`、`trajectory.jsonl`、`patch.diff`、模型 usage、重试／错误、公开测试证据、独立验证、HTML 报告和各产物哈希。请求视图可能包含任务源码、工具输出和模型可见上下文，只在受控输出目录保存。
- 成功条件：模型至少执行一次真实 function call，补丁修改独立 checkout，公开 pytest 有与最终补丁对应的通过证据，`tracefix verify` 在新 checkout 通过；`tracefix export` 生成同哈希补丁；原仓库仍干净。若模型没有修复，照实记录失败、预算和工具轨迹，不通过调整 fixture 或重复运行冲淡结果。

示意命令（在取得付费授权后才执行，`<...>` 需替换为新建 fixture 路径）：

```bash
tracefix doctor --repo <fixture-source> --test-python <python-with-pytest> --test-target tests/test_widget.py --source-import widget --model deepseek/deepseek-flash --output-dir <new-runs-root> --prepare --json
tracefix run --repo <fixture-source> --task "修复分页函数，使公开测试通过" --test-python <python-with-pytest> --test-target tests/test_widget.py --source-import widget --model deepseek/deepseek-flash --output-dir <new-runs-root> --max-steps 10 --max-input-tokens 12000 --max-output-tokens 2000 --per-request-output-tokens 512 --max-test-runs 2 --wall-time-seconds 300 --llm-timeout-seconds 45 --llm-max-retries 0 --context-window-tokens 6000 --context-trigger-tokens 4800 --record-request-views
tracefix verify --run <new-runs-root>/<run-id>
tracefix report --run <new-runs-root>/<run-id>
tracefix export --run <new-runs-root>/<run-id> --output <new-export.patch>
```

执行前还需核对 CLI 对所选模型的当前参数兼容性、供应商账户余额／限制及授权金额。未获授权时，以上只是一份可审查预案，验收项保持“未验证”。
