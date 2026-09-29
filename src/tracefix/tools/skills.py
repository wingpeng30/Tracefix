"""Approved, on-demand skill and reference loading for TraceFix runs."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from tracefix.exceptions import ToolExecutionError
from tracefix.messages import ToolCall
from tracefix.tools.base import BaseTool, SkillCatalogEntry, ToolResult, ToolSpec

_SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills"
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SkillLimits(BaseModel):
    """Hard UTF-8 byte and skill-count bounds; these are not token limits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_active_skills: int = Field(default=4, ge=1)
    max_skill_bytes: int = Field(default=16 * 1024, ge=1)
    max_reference_bytes: int = Field(default=8 * 1024, ge=1)
    max_total_bytes: int = Field(default=32 * 1024, ge=1)


class SkillActivationTool(BaseTool):
    """Expose catalog metadata at startup and approved text only on request."""

    def __init__(
        self,
        root: Path = _SKILL_ROOT,
        *,
        limits: SkillLimits | None = None,
    ) -> None:
        self.limits = limits or SkillLimits()
        self.root = root.resolve(strict=True)
        self.catalog = self._discover()
        self._activated: set[str] = set()
        self._loaded: dict[tuple[str, str], tuple[str, str]] = {}
        self._loaded_bytes = 0

    @property
    def skill_catalog(self) -> tuple[SkillCatalogEntry, ...]:
        return tuple(
            SkillCatalogEntry(
                name=name,
                description=str(item["description"]),
                version=str(item["version"]),
                sha256=str(item["sha256"]),
            )
            for name, item in sorted(self.catalog.items())
        )

    @property
    def spec(self) -> ToolSpec:
        names = sorted(self.catalog)
        return ToolSpec(
            name="load_skill",
            description=(
                "Load the complete instructions for an available skill, or one named reference "
                "from a skill already activated. Skill references are read-only text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": names},
                    "reference": {
                        "type": "string",
                        "description": "Optional relative Markdown path listed by the skill.",
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
        )

    def _discover(self) -> dict[str, dict[str, str | Path]]:
        found: dict[str, dict[str, str | Path]] = {}
        for directory in sorted(self.root.iterdir()):
            if directory.is_symlink():
                raise ToolExecutionError("skill directory cannot be a symbolic link")
            skill_file = directory / "SKILL.md"
            if not directory.is_dir() or not skill_file.is_file():
                continue
            if skill_file.is_symlink() or not skill_file.resolve().is_relative_to(self.root):
                raise ToolExecutionError("skill file escapes the approved skill root")
            if skill_file.stat().st_size > self.limits.max_skill_bytes + 8192:
                raise ToolExecutionError(
                    f"skill file exceeds the discovery size limit: {directory.name}"
                )
            text = skill_file.read_text(encoding="utf-8")
            if not text.startswith("---\n"):
                raise ToolExecutionError(f"skill has malformed frontmatter: {directory.name}")
            _, separator, remainder = text[4:].partition("\n---\n")
            if not separator:
                raise ToolExecutionError(f"skill has unterminated frontmatter: {directory.name}")
            # Locate the closing delimiter without accepting arbitrary YAML documents.
            frontmatter = text[4:].split("\n---\n", 1)[0]
            metadata = self._parse_frontmatter(frontmatter, directory.name)
            body = text[4 + len(frontmatter) + len("\n---\n") :].strip()
            if not isinstance(metadata, dict):
                raise ToolExecutionError(f"skill frontmatter must be a mapping: {directory.name}")
            name, description = metadata.get("name"), metadata.get("description")
            if (
                not isinstance(name, str)
                or not _NAME_RE.fullmatch(name)
                or name != directory.name
                or not isinstance(description, str)
                or not description.strip()
                or name in found
                or not body
            ):
                raise ToolExecutionError(
                    f"skill metadata is invalid or duplicated: {directory.name}"
                )
            if len(body.encode("utf-8")) > self.limits.max_skill_bytes:
                raise ToolExecutionError(
                    f"skill instructions exceed the size limit: {directory.name}"
                )
            version = metadata.get("metadata", {}).get("version", "unversioned")
            if not isinstance(version, str):
                version = str(version)
            found[name] = {
                "description": description.strip(),
                "version": version,
                "path": skill_file,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        return found

    @staticmethod
    def _parse_frontmatter(text: str, directory_name: str) -> dict[str, object]:
        """Parse the scalar Agent Skills fields used here without a runtime dependency."""
        values: dict[str, object] = {"metadata": {}}
        metadata: dict[str, str] = {}
        seen: set[str] = set()
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line.startswith("  "):
                key, separator, raw = line.strip().partition(":")
                if (
                    not separator
                    or key != "version"
                    or key in metadata
                    or not raw.strip()
                ):
                    raise ToolExecutionError(
                        f"unsupported skill metadata syntax: {directory_name}"
                    )
                metadata[key] = SkillActivationTool._scalar(raw.strip(), directory_name)
                continue
            if line[:1].isspace():
                raise ToolExecutionError(f"invalid skill frontmatter indentation: {directory_name}")
            key, separator, raw = line.partition(":")
            if not separator or key not in {
                "name", "description", "license", "compatibility", "allowed-tools", "metadata"
            }:
                raise ToolExecutionError(f"unsupported skill frontmatter field: {directory_name}")
            if key in seen:
                raise ToolExecutionError(f"duplicate skill frontmatter field: {directory_name}")
            seen.add(key)
            if key == "metadata":
                if raw.strip():
                    raise ToolExecutionError(
                        f"metadata must be a YAML mapping: {directory_name}"
                    )
                values[key] = metadata
            else:
                values[key] = SkillActivationTool._scalar(raw.strip(), directory_name)
        values["metadata"] = metadata
        return values

    @staticmethod
    def _scalar(raw: str, directory_name: str) -> str:
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ToolExecutionError(
                    f"invalid quoted skill metadata: {directory_name}"
                ) from exc
        elif raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
            value = raw[1:-1].replace("''", "'")
        else:
            if ": " in raw or raw.startswith(("[", "{", "|", ">")):
                raise ToolExecutionError(
                    f"quote skill metadata containing YAML syntax: {directory_name}"
                )
            value = raw.split(" #", 1)[0].strip()
        if not isinstance(value, str) or not value.strip():
            raise ToolExecutionError(f"skill metadata value must be a string: {directory_name}")
        return value

    def execute(self, call: ToolCall) -> ToolResult:
        name = call.arguments.get("name")
        item = self.catalog.get(name) if isinstance(name, str) else None
        if item is None:
            raise ToolExecutionError("unknown skill name")
        reference = call.arguments.get("reference")
        path = Path(item["path"])
        kind = "skill"
        relative_path = "SKILL.md"
        if reference is not None:
            if name not in self._activated:
                raise ToolExecutionError("activate the skill before loading its references")
            if not isinstance(reference, str) or Path(reference).is_absolute():
                raise ToolExecutionError("skill reference must be a relative path")
            raw_candidate = path.parent / reference
            if any(part.is_symlink() for part in (raw_candidate, *raw_candidate.parents)):
                raise ToolExecutionError("skill reference cannot traverse symbolic links")
            try:
                candidate = raw_candidate.resolve(strict=True)
            except OSError as exc:
                raise ToolExecutionError("skill reference does not exist") from exc
            try:
                reference_root = (path.parent / "references").resolve(strict=True)
            except OSError as exc:
                raise ToolExecutionError("skill has no references directory") from exc
            if (
                candidate.is_symlink()
                or not candidate.is_relative_to(reference_root)
                or candidate.suffix.lower() != ".md"
                or not candidate.is_file()
            ):
                raise ToolExecutionError("reference is outside the approved Markdown directory")
            relative_path = candidate.relative_to(path.parent).as_posix()
            if candidate.stat().st_size > self.limits.max_reference_bytes:
                raise ToolExecutionError(
                    f"reference text exceeds the {self.limits.max_reference_bytes}-byte limit"
                )
            path = candidate
            kind = "reference"
        try:
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(self.root):
                raise ToolExecutionError("approved skill path changed or escaped its root")
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ToolExecutionError("approved skill text could not be read") from exc
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if kind == "skill" and digest != item["sha256"]:
            raise ToolExecutionError("skill content changed after catalog discovery")
        identity = (str(item["version"]), digest)
        loaded_key = (name, relative_path)
        if kind == "skill" and name in self._activated:
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=True,
                output={"name": name, "already_loaded": True},
            )
        if loaded_key in self._loaded:
            previous = self._loaded[loaded_key]
            if previous != identity:
                raise ToolExecutionError("skill text changed after it was loaded")
            return ToolResult(
                call_id=call.id,
                tool_name=call.name,
                success=True,
                output={"name": name, "path": relative_path, "already_loaded": True},
            )
        size = len(content.encode("utf-8"))
        size_limit = (
            self.limits.max_skill_bytes if kind == "skill" else self.limits.max_reference_bytes
        )
        if size > size_limit:
            raise ToolExecutionError(f"{kind} text exceeds the {size_limit}-byte limit")
        if self._loaded_bytes + size > self.limits.max_total_bytes:
            raise ToolExecutionError(
                "skill text budget exceeded "
                f"({self._loaded_bytes}/{self.limits.max_total_bytes} bytes used)"
            )
        if kind == "skill" and len(self._activated) >= self.limits.max_active_skills:
            raise ToolExecutionError("maximum number of active skills reached")
        metadata: dict[str, JsonValue] = {
            "kind": kind,
            "name": name,
            "version": item["version"],
            "sha256": digest,
            "path": str(path.relative_to(self.root)),
            "content_bytes": size,
        }
        output: dict[str, JsonValue] = {**metadata, "content": content}
        self._loaded[loaded_key] = identity
        self._loaded_bytes += size
        if kind == "skill":
            self._activated.add(name)
        return ToolResult(
            call_id=call.id,
            tool_name=call.name,
            success=True,
            output=output,
            metadata=metadata,
        )
