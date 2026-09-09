# TraceFix 合成任务

每个子目录都是一个可独立初始化为 Git 仓库的 Python Bug：

- `task.json`：任务描述、测试命令和预期修改文件。
- `repo/`：带有缺陷的最小仓库模板。
- `gold.patch`：用于验证任务本身可解决的标准补丁。

任务模板不保存嵌套 `.git` 目录。评测器会为每次运行复制模板、创建初始提交，
再交给 `TraceFixRunner` 建立隔离克隆。
