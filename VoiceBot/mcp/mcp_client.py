# voicebot/orchestrator/mcp_client.py

import logging
import httpx
from typing import Any

logger = logging.getLogger(__name__)


class MCPClient:
    """
    Lightweight HTTP client for the MCP tool server.
    One instance shared across the orchestrator — reuses connection pool.
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }
        self._client = httpx.AsyncClient(timeout=timeout)

    async def list_tools(self) -> list[dict]:
        """Fetch available tools from MCP server — call once at init."""
        try:
            r = await self._client.post(
                f"{self.base_url}/mcp",
                headers=self.headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            r.raise_for_status()
            return r.json().get("result", {}).get("tools", [])
        except Exception as e:
            logger.error("[MCP] list_tools failed: %s", e)
            return []

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a single MCP tool and return its text result."""
        try:
            r = await self._client.post(
                f"{self.base_url}/mcp",
                headers=self.headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
            )
            r.raise_for_status()
            content = r.json().get("result", {}).get("content", [])
            return " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        except Exception as e:
            logger.error("[MCP] call_tool %s failed: %s", tool_name, e)
            return f"Tool {tool_name} failed: {e}"

    async def close(self):
        await self._client.aclose()