"""
Minimal Work IQ MCP client (Streamable HTTP + JSON-RPC).

Given a user's delegated Work IQ access token, it performs the MCP handshake
(initialize -> initialized), lists tools, and calls a Copilot/Ask-style tool
with a question. Responses may be JSON or SSE (text/event-stream).
"""

import json
import uuid
from typing import Any, Optional

import httpx

MCP_URL = "https://workiq.svc.cloud.microsoft/mcp"
PROTOCOL_VERSION = "2024-11-05"

# Preferred tool name fragments, most specific first.
_PREFERRED_TOOLS = ("ask", "chat", "copilot", "search", "query")
# Property names a question is likely to map to.
_QUESTION_KEYS = ("query", "question", "prompt", "message", "text", "input")


def _parse_mcp_response(resp: httpx.Response) -> dict:
    """Parse a JSON or SSE MCP response into the JSON-RPC message dict."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        message: Optional[dict] = None
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                message = json.loads(line[len("data:"):].strip())
        if message is None:
            raise RuntimeError(f"No SSE data in MCP response: {resp.text[:300]}")
        return message
    return resp.json()


async def _rpc(
    client: httpx.AsyncClient,
    token: str,
    session_id: Optional[str],
    method: str,
    params: Optional[dict] = None,
    is_notification: bool = False,
) -> httpx.Response:
    """Send a single JSON-RPC request (or notification) to the MCP server."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if not is_notification:
        body["id"] = str(uuid.uuid4())
    if params is not None:
        body["params"] = params

    resp = await client.post(MCP_URL, headers=headers, json=body, timeout=90)
    resp.raise_for_status()
    return resp


def _pick_tool(tools: list[dict]) -> Optional[dict]:
    """Choose a Copilot/Ask-style tool, else the first available tool."""
    for fragment in _PREFERRED_TOOLS:
        for tool in tools:
            if fragment in tool.get("name", "").lower():
                return tool
    return tools[0] if tools else None


def _build_arguments(tool: dict, question: str) -> dict:
    """Map the question into the tool's expected string input field."""
    properties = (tool.get("inputSchema") or {}).get("properties") or {}
    for key in _QUESTION_KEYS:
        if key in properties:
            return {key: question}
    for key, spec in properties.items():
        if isinstance(spec, dict) and spec.get("type") == "string":
            return {key: question}
    return {}


def _extract_text(result: Optional[dict]) -> str:
    """Extract readable text from an MCP tools/call result."""
    content = (result or {}).get("content") or []
    parts = [item.get("text", "") for item in content if item.get("type") == "text"]
    text = "\n".join(p for p in parts if p).strip()
    return text or json.dumps(result)[:1000]


async def ask_workiq(token: str, question: str) -> str:
    """Run the full MCP flow and return Work IQ's answer text."""
    async with httpx.AsyncClient() as client:
        # 1) initialize (also returns the session id we must reuse)
        init = await _rpc(
            client,
            token,
            None,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "qubix-webapp", "version": "1.0"},
            },
        )
        session_id = init.headers.get("Mcp-Session-Id")
        _parse_mcp_response(init)  # validates the handshake

        # 2) initialized notification
        await _rpc(client, token, session_id, "notifications/initialized", is_notification=True)

        # 3) tools/list
        tools_resp = await _rpc(client, token, session_id, "tools/list")
        tools = _parse_mcp_response(tools_resp).get("result", {}).get("tools", [])
        tool = _pick_tool(tools)
        if not tool:
            return "Work IQ returned no tools."

        # 4) tools/call
        call_resp = await _rpc(
            client,
            token,
            session_id,
            "tools/call",
            {"name": tool["name"], "arguments": _build_arguments(tool, question)},
        )
        message = _parse_mcp_response(call_resp)
        if "error" in message:
            return f"Work IQ tool error: {message['error'].get('message')}"
        return _extract_text(message.get("result"))
