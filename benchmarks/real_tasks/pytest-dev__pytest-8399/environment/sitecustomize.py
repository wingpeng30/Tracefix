"""为未构建的 pytest 源码注入 setuptools-scm 生成的版本模块。

该文件只属于测试环境，不会复制到 Agent 工作区。上游源码从 Git 检出时
不包含 ``_pytest/_version.py``，但导入 pytest 又需要它；这里按固定 commit
对应的构建结果注入模块，避免为了运行测试而改动被评测仓库。
"""

from __future__ import annotations

import sys
from types import ModuleType

import _pytest

module = ModuleType("_pytest._version")
module.__version__ = module.version = "6.3.0.dev229+g6e7dc8bac"
module.__version_tuple__ = module.version_tuple = (6, 3, 0, "dev229", "g6e7dc8bac")
module.__commit_id__ = module.commit_id = "g6e7dc8bac"
sys.modules[module.__name__] = module
# Python 在子模块已预置于 sys.modules 时不会自动给父包补属性；旧版 pytest
# 的 terminal 插件会直接读取 ``_pytest._version.version``。
_pytest._version = module
_pytest.__version__ = module.version
