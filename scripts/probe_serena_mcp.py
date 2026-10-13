"""Temporary protocol probe for a pinned Serena process; no model requests."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(project: Path) -> None:
    executable = Path(sys.executable).parent / "serena.exe"
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    env["SERENA_HOME"] = str(project.parent / "serena-home")
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["UV_CACHE_DIR"] = str(project.parent / "uv-cache")
    env["UV_PYTHON_INSTALL_DIR"] = str(project.parent / "uv-python")
    env["UV_PYTHON"] = sys.executable
    parameters = StdioServerParameters(
        command=str(executable),
        args=[
            "start-mcp-server", "--project", str(project),
            "--transport", "stdio", "--context", "desktop-app",
            "--enable-web-dashboard", "false", "--open-web-dashboard", "false",
            "--enable-gui-log-window", "false", "--log-level", "ERROR",
        ],
        env=env,
    )
    async with asyncio.timeout(90):
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = [tool.name for tool in listed.tools]
                print(json.dumps({"tools": names}, ensure_ascii=False))
                print(json.dumps({
                    tool.name: tool.inputSchema for tool in listed.tools
                    if tool.name in {
                        "get_symbols_overview", "find_symbol", "find_referencing_symbols"
                    }
                }, ensure_ascii=False))
                if "get_symbols_overview" in names:
                    result = await session.call_tool(
                        "get_symbols_overview", {"relative_path": "widget.py"}
                    )
                    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
