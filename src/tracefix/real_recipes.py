"""真实任务的版本化环境配方与安全加载逻辑。"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tracefix.exceptions import BenchmarkError


class ExpectedBaseFailure(BaseModel):
    """经审查、只允许在 base 收集阶段出现的精确失败特征。"""

    model_config = ConfigDict(extra="forbid")

    stage: Literal["collection"]
    exception_type: str = Field(min_length=1)
    module: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class EnvironmentRecipe(BaseModel):
    """描述一类真实任务可复现所需的解释器、依赖、构建及平台约束。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    task_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    python_versions: tuple[str, ...] = ()
    extra_dependencies: tuple[str, ...] = ()
    # 每项是无 Shell 参数；{python} 会替换为选中的环境解释器。
    build_commands: tuple[tuple[str, ...], ...] = ()
    # 当官方 FAIL_TO_PASS 记录损坏时，必须明确记录替代选择器及理由。
    selector_overrides: tuple[str, ...] = ()
    selector_reason: str | None = None
    expected_base_failure: ExpectedBaseFailure | None = None
    supported_platforms: tuple[str, ...] = ()
    source_import_probe: str | None = None

    @model_validator(mode="after")
    def validate_recipe(self) -> EnvironmentRecipe:
        if self.selector_overrides and not self.selector_reason:
            raise ValueError("selector overrides require a documented reason")
        for command in self.build_commands:
            if not command:
                raise ValueError("build commands cannot be empty")
            if command[0] != "{python}":
                raise ValueError("build commands must start with {python}")
        return self

    @property
    def fingerprint(self) -> str:
        """返回决定环境可复用性的稳定配方摘要。"""
        payload = self.model_dump(mode="json")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def supports_python(self, version: str) -> bool:
        """按 major.minor 比较声明的精确兼容版本。空值表示不额外约束。"""
        normalized = ".".join(version.split(".")[:2])
        return not self.python_versions or normalized in self.python_versions

    def supports_current_platform(self) -> bool:
        """判断本机平台是否在配方允许范围内。"""
        platform = "windows" if sys.platform == "win32" else sys.platform
        return not self.supported_platforms or platform in self.supported_platforms


def load_environment_recipes(directory: Path) -> dict[str, EnvironmentRecipe]:
    """读取配方目录；同一任务重复定义会被拒绝，防止口径不确定。"""
    root = directory.expanduser().resolve()
    if not root.is_dir():
        return {}
    recipes: dict[str, EnvironmentRecipe] = {}
    for path in sorted(root.glob("*.json")):
        try:
            recipe = EnvironmentRecipe.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BenchmarkError("invalid environment recipe", context={"path": str(path)}) from exc
        if recipe.task_id in recipes:
            raise BenchmarkError(
                "duplicate environment recipe", context={"task_id": recipe.task_id}
            )
        recipes[recipe.task_id] = recipe
    return recipes
