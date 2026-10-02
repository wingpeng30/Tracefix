# 公开任务资格筛选：回归反馈功能后的下一步

日期：2026-09-30
状态：候选审查完成；尚未冻结可用于比较的任务集，未运行候选仓库测试，零供应商调用。
TraceFix PR #9：已合并；PR head `cb5571815a09f831cde0fd1f648059b6fdff6877`，merge commit `b18958b8342b0642b7f4fe828c39852ada769319`，GitHub CI #73 成功。

## 为什么下一步先做任务资格，而不是扩功能

结束前回归门已经能在 pytest 失败后把明确证据交回 Agent，但功能正确不代表预先选定的目标本身能公平地测量一次修复。此前外部任务中出现过任务文字没有规定的参数语义、基线与新增测试契约不一致、网络代理和测试环境失败等问题。先冻结小而可信的任务契约，才能判断后续失败来自 Agent 决策、harness、测试选择还是环境。

本记录只处理开发任务候选，不读取 `benchmarks/holdout_candidates/`，也不新增模型调用。已消耗的八次供应商运行额度不续用；所有任务未来若要进行真实模型评估，须另行取得授权。

## 候选筛选结果

候选池及配套配方位于 `benchmarks/real_candidates/` 和 `benchmarks/real_recipes/`。按 issue ID 检索现有开发、实验和交接记录，结果仅用于辨认复用情况，不表示没有命中的任务从未在所有本地试验中出现。

| 候选 | 任务类型与固定源码 | 本地契约证据 | 决定 |
| --- | --- | --- | --- |
| `sphinx-doc__sphinx-10323` | literalinclude 的 dedent 与 prepend/append 次序；base `31eba1a76dd485dc633cae48227b46879eda5df4`；改动范围为 `sphinx/directives/code.py` | 一条针对 `test_LiteralIncludeReader_dedent_and_append_and_prepend` 的新增测试；配方是 Linux/Python 3.12，无额外依赖；问题描述与 gold patch 都指向过滤器顺序 | **优先做资格预检**。在正式冻结前，要证明任务测试及整文件测试在 base/gold 上稳定，并从 base 中挑出已经存在、适合登记为回归目标的测试。新增测试补丁不能误称为 base 中已有回归覆盖。 |
| `psf__requests-1724` | Unicode HTTP method 与 multipart 请求；base `1ba83c47ce7b177efe90d5f51f7760680f72eda0`；改动范围为 `requests/sessions.py` | recipe 标为 Windows/Python 3.9；新增复现测试请求 `httpbin`；task.json 的六个 fail-to-pass 节点与 test.patch 新增的 `test_unicode_method_name` 不一致 | **暂不纳入**。先修正可复现测试服务与目标清单的来源关系；不能把网络失败计作修复失败，也不能把任务错配交给模型解释。 |
| `pylint-dev__pylint-4604` | type comment 中的模块属性使用与 unused-import；base `1e55ae64624d28c5fe8b63ad7979880ee2e6ef3f` | 配方记录 test.patch 在 base 收集阶段因为缺 `pylint.constants.IS_PYPY` 而失败；任务此前在历史开发消融中出现 | **暂不纳入首轮**。它可作为已看过的 harness 故障诊断样本；须先把测试准备步骤和任务源码身份分开固定，并明确这是历史重用，不得当作新样本或效果证据。 |
| `pytest-dev__pytest-10356` | 继承标记的顺序与返回值语义 | 任务历史已显示问题文字允许改测试，且曾出现生成器/列表契约歧义 | **排除首轮**。除非先从 issue 与原始测试中明确任务约束，否则再次运行只会复现选择歧义。 |

当前只有 Sphinx-10323 满足“历史记录中未见、任务文字与代码改动范围一致、环境配方简单”的初筛条件。它尚未通过本轮 base/gold 复验，也尚未证明有现成回归目标。因此现在宣称已经冻结了“多类型任务集”是不准确的。首轮不为凑数量引入 Requests 的联网不确定性或 Pylint 的基线测试准备缺口。

## 下一阶段的明确动作和停止条件

1. 在一次新的、受控的只读源码副本中准备 Sphinx-10323：绑定 base commit、仓库许可证、recipe 哈希、test patch 和 gold patch 哈希；原始候选文件及既有实验保持不变。
2. 在同一固定环境、独立 checkout 中运行目标测试和整个 `tests/test_directive_code.py`，分别验证 base 与 gold。保存 pytest 原始输出、JUnit、退出码、checkout 源码状态和环境指纹。任意结果不稳定或 import 来源不符时，将此候选标为不合格并停止，不开始 Agent 运行。
3. 只从 base commit 中原已存在的测试挑选回归目标；逐个确认：base 通过、gold 通过、目标和测试文件均在冻结源码中、测试不依赖外部网络。若没有能约束邻近行为的目标，记录该任务只能用于目标修复诊断，不用于验证“修复一处是否破坏已有行为”的回归门。
4. 并行以静态元数据审查一至两个新的公开候选，要求 issue 描述与补丁语义一致、测试契约可以从固定源码中表达、环境不依赖外部服务，且不属于 20 道留出题。候选来源、许可证、base SHA、任务文字和测试身份均核实后，才写入真正的冻结清单。
5. 任务资格闭环全部通过后，形成至少三个不同缺陷类型的公开任务集合并冻结内容哈希；把“已使用/未使用”与任务数据分开标注。零调用 qualification 只证明任务和验收器可复现，不证明 Agent 修复效果。

若没有三个符合条件的任务，不降低条件；交付已经合格的子集并将缺口保留为待办。若之后要比较模型或 Agent 行为，先冻结配置、预算、次数、失败分类和停止条件，再为新的供应商调用另行申请授权。本轮不更改产品代码，不访问留出题，不更新旧成绩或账本。

## 当前验证与限制

- 只读核对：本地 `codex/gated-regression-feedback` HEAD 为 `cb5571815a09f831cde0fd1f648059b6fdff6877`，跟踪文件修改前干净；该分支对应已合并 PR #9 的 head。
- GitHub PR #9：`merged=true`，merge commit `b18958b8342b0642b7f4fe828c39852ada769319`；精确 head CI #73 成功。
- 本记录的候选筛选读取了公开候选包的 `task.json`、`candidate.json`、`problem.md`、test/gold patch 和 recipe，以及已跟踪历史文档。**没有运行**候选仓库测试、模型、留出任务或付费请求。
- 工作区另有历史未跟踪材料；本记录不纳入、不移动、不删除这些文件。

## 后续交付

完成 Sphinx-10323 的 base/gold qualification 与第二、第三个合格候选发现后，再新增带原始日志位置及 SHA-256 的冻结 manifest。只有 manifest 中的任务具备可复现测试契约，才据此决定是否值得进入真实模型诊断批次。

2026-10-02 当前执行顺序更新：先交付安装包内 `regression-feedback` 入口，再冻结 more-itertools #462 空输入任务并执行资格矩阵；此前 Sphinx 初筛保留为历史，本批不重建环境。所有调用均离线，原八次付费额度不续用。执行记录见 `docs/tasks/2026-10-02-package-and-public-task.md`（文档内路径相对仓库根目录）。
