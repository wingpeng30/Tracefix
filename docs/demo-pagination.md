# 分页 Bug 修复演示

这是固定脚本模型驱动的合成演示，供应商客户端构造与请求发送均被禁止。它展示
TraceFix 的工程控制流，不证明真实模型的独立修复成功率。

## 运行

先按[复现指南](reproduction.md)选择平台哈希锁完成安装。每次使用不存在的新输出目录：

```powershell
tracefix-reproduce --scenario pagination --output runs/demo-baseline
tracefix-reproduce --scenario pagination --skills-enabled --output runs/demo-skills
tracefix report --run runs/demo-skills --output runs/demo-skills/report.html
```

打开两个 `report.html`，或复制整个输出目录到别处后重新执行 `tracefix report`。
报告完全离线，不读取轨迹中记录的旧绝对路径；`result.json` 和
`trajectory.jsonl` 是必需输入。运行失败或多运行目录时，命令以非零状态退出。
报告中的源码与日志可能包含业务信息，分享前请自行检查。

Docker 方式沿用已冻结的合成镜像，并传入完整 image ID：

```powershell
tracefix-reproduce --scenario pagination --backend docker --image-id $imageId --output runs/docker-demo-baseline
tracefix-reproduce --scenario pagination --backend docker --image-id $imageId --skills-enabled --output runs/docker-demo-skills
```

生成镜像的命令与依赖边界见[复现指南](reproduction.md)。

## 两分钟讲稿

1. **0–25 秒：问题。** 公开接口在 `catalog/api.py`，分页计算在另一个文件。
   页码从 1 开始，但计算写成 `page * page_size`，第一页跳过第一批。
2. **25–55 秒：证据。** 展开时间线。第一次 pytest 失败；Agent 搜索符号，
   分别读取调用方和实现。页面展示真实保存的工具参数、输出和错误。
3. **55–85 秒：修复。** Diff 只修改 `catalog/pagination.py` 的一个表达式，
   变为 `(page - 1) * page_size`；测试文件不动。第二次 pytest 通过。
4. **85–120 秒：设计取舍。** （一）用真实 Runner、受审计工具和 Docker bridge，
   而非手写成功截图；（二）Skills 默认关闭且按需加载，可对照同一补丁；
   （三）报告从保存的轨迹生成，区分 Agent 结束、公开测试与独立验收，
   缺失证据如实显示。

本演示没有独立验收或真实模型 usage；页面不会把零脚本 Token 当成真实 Token 成本。
