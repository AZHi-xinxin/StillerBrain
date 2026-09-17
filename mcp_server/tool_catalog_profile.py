"""Legacy full catalog and compact, capability-complete daily discovery."""
from __future__ import annotations

from typing import Any

from mcp import types
from mcp.shared.exceptions import McpError
from .compact_catalog_schema import project_compact_tool
from runtime.compact_tool_routes import DISCOVERY_TOOL, MANAGE_TOOL


TOOL_PROFILE_HEADER = "x-stbrain-tool-profile"
TOOL_PROFILE_QUERY = "tool_profile"
DAILY_TOOL_NAMES = (
    "stbrain_open",
    "remember_memory",
    "remember_tool_guidance",
    "revise_memory",
    "advance_plan",
    DISCOVERY_TOOL,
    MANAGE_TOOL,
)
_DAILY_TOOL_NAMES = frozenset(DAILY_TOOL_NAMES)
DAILY_CATALOG_NAVIGATION = (
    "当前为精简日常档：通用存入/修改/查询和计划推进直接调用；"
    "查询用 stbrain_open(view='recall',query=...)，可选 module。"
    "其余功能用 stbrain_tools 按分类找、stbrain_manage 按操作名执行。"
    "模块一、人称、DIY 提醒和浮现开关均可由你在原权限内自主操作，无需请人切档。"
    "已知参数可直接调用，不强制先读说明。"
)
_INVALID_PROFILE_MESSAGE = (
    "tool_profile_invalid: X-STBrain-Tool-Profile 或 URL 参数 tool_profile "
    "仅接受 daily/full；每个选择器最多出现一次，同时提供时必须一致。"
)


def _request_tool_profile(mcp: Any) -> str:
    """Read the SDK's current HTTP request without retaining connection state."""
    try:
        request = mcp.get_context().request_context.request
    except ValueError:
        # FastMCP has no request context during ordinary in-process discovery.
        return "full"
    if request is None:
        return "full"
    header_values = request.headers.getlist(TOOL_PROFILE_HEADER)
    query_values = request.query_params.getlist(TOOL_PROFILE_QUERY)
    if (
        len(header_values) > 1
        or len(query_values) > 1
        or any(value not in {"daily", "full"} for value in header_values + query_values)
        or (header_values and query_values and header_values[0] != query_values[0])
    ):
        # Do not reflect request values, URLs, headers or credentials in errors.
        raise McpError(types.ErrorData(code=types.INVALID_PARAMS, message=_INVALID_PROFILE_MESSAGE))
    return (header_values or query_values or ["full"])[0]


def install_tool_catalog_profile(mcp: Any) -> None:
    """Project explicit tools/list responses after the SDK caches full schemas.

    MCP 1.29 also calls the ListToolsRequest handler with None when a tools/call
    misses its schema cache. That internal refresh must always see the existing
    complete catalog, regardless of a caller's discovery selector. Filtering
    only the protocol response preserves input/output validation and keeps the
    tool manager, every call wrapper, and cached compatibility calls intact.
    """
    server = mcp._mcp_server
    if getattr(server, "_tool_catalog_profile_installed", False):
        raise RuntimeError("tool_catalog_profile_already_installed")
    if any(mcp._tool_manager.get_tool(name) is None for name in DAILY_TOOL_NAMES):
        raise RuntimeError("daily_tool_catalog_incomplete")
    original_handler = server.request_handlers[types.ListToolsRequest]

    async def list_tools(request: types.ListToolsRequest | None) -> types.ServerResult:
        if request is None:
            return await original_handler(request)
        profile = _request_tool_profile(mcp)
        result = await original_handler(request)
        if profile == "full":
            # Keep the legacy full44 contract byte-for-byte; the SDK internal
            # cache (request=None) still includes both compact native facades.
            return result.model_copy(update={"root": result.root.model_copy(update={
                "tools": [tool for tool in result.root.tools
                          if tool.name not in {DISCOVERY_TOOL, MANAGE_TOOL}],
            })})
        available = {tool.name: tool for tool in result.root.tools
                     if tool.name in _DAILY_TOOL_NAMES}
        # Discovery order is presentation only, not an authorization rule.
        # Put the read/open entry first so a crowded MCP client shows ST's
        # safest entry before any write operation.
        selected = [project_compact_tool(available[name])
                    for name in DAILY_TOOL_NAMES]
        selected = [tool.model_copy(update={
            "description": DAILY_CATALOG_NAVIGATION + "\n\n" + (tool.description or ""),
        }) if tool.name == "stbrain_open" else tool for tool in selected]
        return result.model_copy(update={
            "root": result.root.model_copy(update={"tools": selected}),
        })

    server.request_handlers[types.ListToolsRequest] = list_tools
    server._tool_catalog_profile_installed = True
