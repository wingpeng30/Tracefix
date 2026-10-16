# G6 分层仓库规则：阶段验收

G6阶段门已关闭。Agent会在访问相关文件前收到适用的仓库规则；规则有来源和版本，不能扩大权限。
搜索可以在新规则处暂停并继续，压缩和恢复后仍能核对规则及搜索游标。新功能默认关闭。

## 已验收身份

- 实现提交：`a5683b3826e06675656b078a324233090934dc53`，分支`codex/harness-runtime`。
- Git tree：`3631bd3e16b140d0301f7cefe5ea8135494fb307`。
- [CI170](https://github.com/wingpeng30/Tracefix/actions/runs/38852551739)：五个job全部成功。
- [同提交Linux补证](https://github.com/wingpeng30/Tracefix/actions/runs/38852551729)：成功。
- 工作区338工程文件清单：`runs/harness-runtime-start/ci170-source-manifest.json`，SHA256 `a8e25d1e06c046f9235bf570115f089ab5db29b68437d30d345fc254ed43c3c4`；实际平台文件字节另随各CI产物保存。
- 后续文档提交只记录上述实现；不能把新的文档HEAD说成重新完成了全量工程验收。

## 完整工程门

| 检查 | 实际结果 |
|---|---|
| Windows Python3.11.9全量 | 2469 passed，0 failed/error，3项Linux平台skip |
| 3.11精确行分支综合覆盖率 | `30228/33450 = 90.3677130044843%` |
| Windows Python3.12.10全量 | 2469 passed，0 failed/error，3项Linux平台skip |
| 3.12精确行分支综合覆盖率 | `30225/33450 = 90.35874439461884%` |
| 原范围及新增源码Ruff、编译、diff | 两版通过，未缩小范围或降低门槛 |
| 仓库外editable、独立wheel、原公开资格检查 | 两版quality job全部成功 |
| 基础Docker、ordinary Docker、Serena | 三个真实job全部成功，保留全部原回归 |
| 新规则真实Docker组合 | wheel和editable都完成两宿主/两任务容器恢复、Serena补丁前后查询及真实pytest |
| Linux原生与进程域补证 | 518 passed，0 failed/error，9项Windows平台skip |

原始JUnit已逐条互补：Windows的2项cgroup及1项FIFO在Linux通过；Linux的7项Windows Job Object及2项Windows文件系统用例在两版Windows通过。
`ci170-platform-complement-proof.json`记录每个完整测试名，未解释skip为0。

## 已交付能力

1. 祖先`AGENTS.md`与`.tracefix/rules.toml`路径规则，固定顺序、内容哈希和作用域；只读仓库内获准普通文件。
2. 无跟随、有界文件读取及元数据枚举；补丁先纯词法检查全部路径，再读取preimage；Git名字元数据覆盖删除路径。
3. 权限检查先于规则门；规则门先于缓存、测试prepare和计次。新规则先返回pending，完整批次闭合后追加，下一真实请求收到才授予该版本访问资格。
4. 逐文件搜索及opaque cursor，绑定run、查询、候选、规则与文件中段内容；源码MCP正文必须能核对来源，遇到新作用域先扣住正文。
5. 版本化canonical规则/游标检查点，恢复核对历史、请求收据、工具身份及引用；组件全部验证后才发布。TaskState只保存规则引用。
6. CLI `--repository-rules`、TOML/Python `repository_rules_enabled`；ordinary要求recovery，宿主状态唯一，容器只提供版本化私有只读端口。
7. 工具目录分开保存：容器原执行目录与宿主分页搜索适配器不混算。只读执行器bundle、hello及恢复共同核验策略和协议身份。

开启规则时不注入启动Repo Map源码摘要，相关源码改走规则门。关闭时保留原工具schema和入口。
用法、配额、范围及零供应商入口见[仓库规则说明](../repository-rules.md)。

## 真实组合与产物

CI的ordinary规则回放在wheel和editable两种安装方式下均为：步骤2安全暂停，另一宿主及任务容器恢复后步骤10完成；测试1次，旧2个cursor保留，error为null，供应商调用0。
Serena实际查询分别核对补丁前后的函数正文；原ordinary回归及独立补丁验证同时保留。本机另在第三个全新容器重应用规则回放补丁验证通过，详情保留各构建身份。

CI镜像：

- 基础：`sha256:bcd7ad7b7b4b2c0c63686f5b5ec0ef1b37513b0ba4f03826c0aec50b353c3bff`。
- 独立Serena job：`sha256:dcd075be87f83c351a5b8a9865af60b06e2a33d6ba9a967a00ca745d1b6fb3d9`。
- ordinary：`sha256:717ebc72019048a14fc0a9fa2a22e0a4ecd0edbfa384aee0626f6b85bc3925ac`。
- ordinary组合Serena：`sha256:a03deb7ca5be55cfcdc3226a3432f5eb1bd7ddd18397c9c9687e79799638a250`。

六类官方ZIP都已下载并核对SHA256，位于候选忽略目录`runs/harness-runtime-start/ci170-*`：

| 产物ID | SHA256 |
|---|---|
| 12026506928 / Python3.11 | `fbada80d2ab92ae6ed6a213ad358cf891e963babd543545f82e17c3dfe2ced06` |
| 12026857884 / Python3.12 | `955002e14a258c1c907f36a276d52b60b9b70f6397b662b054c7f3250500ca4f` |
| 12025343240 / 基础Docker | `b3ae2ad243b59a67a5074e06c39d2bdb2d728d9b6646e80861d87738a54c9841` |
| 12025789156 / ordinary | `7536bbb9bc3bbcc3ef994293d73b79d87c929f97de46b11d66b5daa203131978` |
| 12026145269 / Serena | `6d311716674f6b517acd8649891084e9ec39dc6bb49047b14b9fe9a98aa83713` |
| 12025527719 / Linux | `772dbe193789a155059791ab39ff9ac1c3a080c9da012287e40d1ead229545fa` |

六个job的解码日志、两版原始Coverage JSON/JUnit、平台逐项证明及镜像清单也已归档。下载票据用后删除，文档不保存临时签名URL。

## 失败、限制与下一阶段

本机旧身份的两轮全量检查为修复已确认问题主动中止，不能记通过；最终工程门以本提交CI170为准。
实施期间的回放patch夹具、独立Git夹具、宿主/容器工具身份混算、恢复报告旧error及格式门失败均保留原归属，详见[G6实施记录](2026-10-16-g6-progress.md)。
本机旧wheel及EOF空白整理后的新wheel各保留构建身份；没有用旧wheel或旧CI代替当前提交。
规则正文超限/损坏明确拒绝；不是权限来源。ordinary仍要求recovery及network none；跨实现旧检查点不自动迁移。
G7扩展生命周期和G8子代理不在本阶段验收内；按既定顺序下一步进入G7.1 Skills兼容性与能力契约。
这些是零供应商控制流与工程证据，不是新的模型自主修复成绩；历史19/30等实验归属不变。
