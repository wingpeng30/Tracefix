"""Shared pure argument models; names preserve existing provider schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _SearchCodeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    path: str = "."
    glob: str = "**/*"
    case_sensitive: bool = False
    max_results: int = Field(default=20, ge=1, le=200)



class _ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> _ReadFileArgs:
        """拒绝反向行号范围。"""
        if self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line cannot be earlier than start_line")
        return self



class _ApplyPatchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patch: str = Field(min_length=1)



class _RunTestsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = "pytest -q"
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)



class _GetGitDiffArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_lines: int = Field(default=3, ge=0, le=20)


class RuleSearchArguments(_SearchCodeArgs):
    query: str = Field(min_length=1, max_length=1000)
    path: str = Field(default=".", max_length=1024)
    glob: str = Field(default="**/*", max_length=512)
    cursor: str | None = Field(default=None, pattern=r"^rules_cursor_[a-f0-9]{32}$")
