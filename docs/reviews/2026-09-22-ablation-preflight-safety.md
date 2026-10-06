# 消融离线预检安全修复

本次代码基线为 `f352e4b`，验证对象为其上的未提交消融实现；本记录仅覆盖
`scripts/ablation_preflight.py`、新增受控 httpbin 启动脚本与定向测试。

实际改动：扩充依赖清单前核验 V0.8.9 原始 recipe-lock 与 holdout-freeze 的绑定，逐题核验
配方哈希、固定提交、base/gold 目标节点、依赖指纹，并由完整版本清单重新计算指纹。价格预检
校验模型、币种和本地 HTML 的原始哈希。每次新预检必须使用不存在的目录；显式 `--resume`
核验 manifest 中已冻结的输入及输出，不重新生成或覆盖预检证据。失败的新目录原样保留；
没有完成 manifest 的目录不能当作可恢复成功预检。

httpbin 身份通过只读 Windows 进程和监听端口查询核验。2026-09-22 实际观察到虚拟环境
启动器 PID 250844 的直接子进程 PID 244432 监听 `127.0.0.1:8765`；基解释器与
`pyvenv.cfg` 的 home 相符，两者 Flask 参数一致。脚本将其标为
`verified_process_chain`，同时明确当前磁盘依赖清单不等于历史启动时内存快照。受限权限无法
查询时如实保存 unknown 与原因。新增 `controlled_httpbin.py` 提供明确启动/复用身份端点，
拒绝覆盖现有身份文件及占用端口；没有启动该备用服务，没有停止原服务或变更验收 URL。

验证环境为本机 Windows、仓库 `.venv` Python 3.12.5；目标服务解释器为 Python 3.9.21。
测试配置：`tests/test_ablation_preflight.py -q -p no:cacheprovider`，独立 basetemp
`.test-tmp-ablation-preflight-review-v3`，结果 **9 passed in 0.44s**。此前沙箱执行因
Windows 临时目录 ACL 错误失败；提升权限后的定向测试通过。Ruff 对三个实现/测试文件检查
通过；实际读取全部 20 题原始锁及价格 HTML 校验通过。服务目标 Python 执行备用脚本
`--help` 通过，未开启监听。定向测试原始临时工件保留在上述 basetemp，工具执行输出保留在
当前任务记录；尚未生成新的 120 项模拟运行证据。

没有付费调用，没有改写共享 CNY 账本，没有提交。9 个测试及旧锁复核仅证明预检边界；
不代表真实修复成功率、离线定位提升或合成任务修复效果。下一步由主执行任务在全部代码稳定后，
使用新输出目录运行完整离线预检与 120 项模拟/恢复验收，并冻结对应原始结果。
