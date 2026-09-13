"""将完整工具结果投影为更紧凑的模型可见消息。

完整 ``ToolResult`` 永远由轨迹事件保存；本模块只决定下一次模型请求看到的
文本，避免搜索命中、源码和 pytest 输出在多轮中反复占用输入 Token。
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from tracefix.tools.base import ToolResult

_MARKER = "\n...[TraceFix 已裁剪模型可见工具结果]...\n"


class ToolPresentationConfig(BaseModel):
    """模型可见工具结果的确定性精简配置；关闭时保留原始 JSON。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    enabled: bool = True
    max_search_matches: int = Field(default=6, ge=1, le=50)
    max_read_chars: int = Field(default=4_000, ge=256)
    max_test_output_chars: int = Field(default=4_000, ge=256)
    max_diff_chars: int = Field(default=5_000, ge=256)
    max_generic_chars: int = Field(default=2_000, ge=256)


class ToolPresentationMetrics(BaseModel):
    """累计记录原始工具结果与模型视图之间的字符节省估计。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    result_count: int = Field(default=0, ge=0)
    compacted_result_count: int = Field(default=0, ge=0)
    original_chars: int = Field(default=0, ge=0)
    presented_chars: int = Field(default=0, ge=0)

    @property
    def estimated_tokens_saved(self) -> int:
        """以约四字符一 Token 的稳定启发式报告节省量，不替代供应商 usage。"""
        return max(0, (self.original_chars - self.presented_chars) // 4)

    def add(self, original: str, presented: str) -> None:
        """合并一条工具结果的原始与投影长度。"""
        self.result_count += 1
        self.original_chars += len(original)
        self.presented_chars += len(presented)
        if original != presented:
            self.compacted_result_count += 1


class ToolResultPresenter:
    """按工具语义保留调试事实，并对大字段执行首尾裁剪。"""

    def __init__(self, config: ToolPresentationConfig | None = None) -> None:
        self.config = config or ToolPresentationConfig()

    def present(self, result: ToolResult) -> str:
        """返回给模型的 JSON；输入 ``ToolResult`` 本身不会被修改。"""
        original = result.model_dump_json()
        if not self.config.enabled:
            return original
        payload: dict[str, JsonValue] = {
            "call_id": result.call_id,
            "tool_name": result.tool_name,
            "success": result.success,
        }
        if result.error:
            payload["error"] = _shorten(result.error, self.config.max_generic_chars)
        if result.metadata:
            # 元数据主要保留缓存、策略和错误分类，不复制潜在的大型供应商/命令载荷。
            metadata_json = json.dumps(result.metadata, ensure_ascii=False, separators=(",", ":"))
            payload["metadata"] = (
                result.metadata
                if len(metadata_json) <= self.config.max_generic_chars
                else {"truncated_metadata": _shorten(metadata_json, self.config.max_generic_chars)}
            )
        payload["output"] = self._present_output(result.tool_name, result.output)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _present_output(self, tool_name: str, output: JsonValue) -> JsonValue:
        if not isinstance(output, dict):
            return self._generic(output)
        value: dict[str, JsonValue] = dict(output)
        if tool_name == "search_code":
            matches = value.get("matches")
            if isinstance(matches, list) and len(matches) > self.config.max_search_matches:
                value["matches"] = matches[: self.config.max_search_matches]
                value["matches_omitted"] = len(matches) - self.config.max_search_matches
                # 明确告诉模型仍有命中，促使它缩小 path/glob/query，而不是误以为
                # 已经穷尽结果后继续使用宽泛搜索。
                value["next_action"] = "请用具体 path、glob 或更精确关键词继续定位。"
            return value
        if tool_name == "read_file":
            return self._shorten_fields(value, ("content",), self.config.max_read_chars)
        if tool_name == "run_tests":
            return self._shorten_fields(
                value, ("stdout", "stderr"), self.config.max_test_output_chars
            )
        if tool_name == "get_git_diff":
            return self._shorten_fields(value, ("diff",), self.config.max_diff_chars)
        return self._generic(value)

    def _generic(self, value: JsonValue) -> JsonValue:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(rendered) <= self.config.max_generic_chars:
            return value
        return {"truncated_output": _shorten(rendered, self.config.max_generic_chars)}

    @staticmethod
    def _shorten_fields(
        value: dict[str, JsonValue], fields: tuple[str, ...], limit: int
    ) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = dict(value)
        for field in fields:
            current = result.get(field)
            if isinstance(current, str):
                result[field] = _shorten(current, limit)
        return result


def _shorten(value: str, limit: int) -> str:
    """保留报错开头与结尾，结尾通常含 traceback/pytest 最终摘要。"""
    if len(value) <= limit:
        return value
    tail = min(max(256, limit // 4), limit // 2)
    head = limit - tail
    return value[:head] + _MARKER + value[-tail:]
