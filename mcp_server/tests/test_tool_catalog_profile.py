"""Actual SDK protocol handlers with synthetic tools; no stores or model calls."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import unittest

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from mcp.shared.exceptions import McpError
from starlette.requests import Request

from mcp_server.person_reference_surface import install_person_reference_advisory_surface
from mcp_server.simple_tool_catalog import install_simple_tool_catalog
from mcp_server.compact_catalog_schema import project_compact_tool
from mcp_server.tool_catalog_profile import (
    DAILY_CATALOG_NAVIGATION,
    DAILY_TOOL_NAMES,
    install_tool_catalog_profile,
)


EXPECTED_DAILY_NAMES = {
    "remember_memory", "remember_tool_guidance", "stbrain_open",
    "revise_memory", "advance_plan", "stbrain_tools", "stbrain_manage",
}
FACADES = {"stbrain_tools", "stbrain_manage"}


@contextmanager
def http_context(*, headers=(), query="", request_present=True):
    request = Request({
        "type": "http", "method": "POST", "path": "/mcp",
        "query_string": query.encode("ascii"),
        "headers": [(name.lower().encode("ascii"), value.encode("ascii"))
                    for name, value in headers],
    }) if request_present else None
    token = request_ctx.set(RequestContext(
        request_id="synthetic-request", meta=None, session=None,
        lifespan_context=None, request=request,
    ))
    try:
        yield
    finally:
        request_ctx.reset(token)


class ToolCatalogProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mcp = FastMCP("synthetic-tool-catalog-profile")
        self.calls = []

        async def synthetic_tool(value: str = "synthetic") -> dict[str, str]:
            self.calls.append(value)
            return {"value": value}

        for name in sorted(EXPECTED_DAILY_NAMES | {
            "stbrain_help", "manage_person_reference_advisory", "revise_tool_guidance",
        }):
            self.mcp.add_tool(synthetic_tool, name=name, description="Original description: " + name)
        install_person_reference_advisory_surface(self.mcp, SimpleNamespace(
            advisory_status=lambda: {"enabled": True, "message": "Synthetic author preference."},
        ))
        install_simple_tool_catalog(self.mcp)
        self.manager = self.mcp._tool_manager
        self.original_list = self.manager.list_tools
        self.original_get = self.manager.get_tool
        self.original_call = self.manager.call_tool
        self.original_handler = self.mcp._mcp_server.request_handlers[types.ListToolsRequest]
        self.original_tools = {tool.name: tool for tool in self.original_list()}
        install_tool_catalog_profile(self.mcp)
        self.handler = self.mcp._mcp_server.request_handlers[types.ListToolsRequest]

    async def listed(self):
        return await self.handler(types.ListToolsRequest(method="tools/list"))

    @staticmethod
    def names(result):
        return {tool.name for tool in result.root.tools}

    @staticmethod
    def legacy(result):
        return result.model_copy(update={"root": result.root.model_copy(update={
            "tools": [tool for tool in result.root.tools if tool.name not in FACADES],
        })})

    async def test_default_and_explicit_full_are_exact_original_directory(self):
        baseline = await self.original_handler(types.ListToolsRequest(method="tools/list"))
        expected = self.legacy(baseline).model_dump(by_alias=True)
        self.assertEqual(expected, (await self.listed()).model_dump(by_alias=True))
        for options in (
            {}, {"headers": [("X-STBrain-Tool-Profile", "full")]},
            {"query": "tool_profile=full"},
            {"headers": [("x-stbrain-tool-profile", "full")], "query": "tool_profile=full"},
            {"request_present": False},
        ):
            with self.subTest(options=options), http_context(**options):
                self.assertEqual(expected, (await self.listed()).model_dump(by_alias=True))
        self.assertNotIn("revise_tool_guidance", self.names(baseline))
        self.assertEqual([], self.calls)

    async def test_daily_has_seven_compact_tools_without_mutating_sdk_contracts(self):
        self.assertEqual(EXPECTED_DAILY_NAMES, set(DAILY_TOOL_NAMES))
        baseline = await self.original_handler(types.ListToolsRequest(method="tools/list"))
        original = {tool.name: tool for tool in baseline.root.tools}
        for options in (
            {"headers": [("X-STBrain-Tool-Profile", "daily")]},
            {"query": "tool_profile=daily"},
            {"headers": [("x-stbrain-tool-profile", "daily")], "query": "tool_profile=daily"},
        ):
            with self.subTest(options=options), http_context(**options):
                result = await self.listed()
                self.assertEqual(7, len(result.root.tools))
                self.assertEqual("stbrain_open", result.root.tools[0].name)
                self.assertEqual(EXPECTED_DAILY_NAMES, self.names(result))
                for tool in result.root.tools:
                    expected = project_compact_tool(original[tool.name]).model_dump(by_alias=True)
                    if tool.name == "stbrain_open":
                        expected["description"] = DAILY_CATALOG_NAVIGATION + "\n\n" + expected["description"]
                    self.assertEqual(expected, tool.model_dump(by_alias=True))
                self.assertEqual(set(original), set(self.mcp._mcp_server._tool_cache))
        for phrase in ("精简", "stbrain_tools", "stbrain_manage", "模块一", "DIY", "无需请人切档",
                       "不强制先读说明"):
            self.assertIn(phrase, DAILY_CATALOG_NAVIGATION)
        self.assertEqual(self.legacy(baseline).model_dump(by_alias=True), (await self.listed()).model_dump(by_alias=True))
        self.assertEqual([], self.calls)

    async def test_daily_keeps_existing_author_preference_and_original_tool_objects(self):
        with http_context(query="tool_profile=daily"):
            tools = {tool.name: tool for tool in (await self.listed()).root.tools}
        for name in ("remember_memory", "remember_tool_guidance"):
            self.assertIn("Synthetic author preference.", tools[name].description)
        for tool in self.original_list():
            self.assertEqual(self.original_tools[tool.name].description, tool.description)
            self.assertEqual(self.original_tools[tool.name].parameters, tool.parameters)

    async def test_sequential_and_concurrent_requests_never_inherit_profile(self):
        full_names = set(self.original_tools) - FACADES
        with http_context(query="tool_profile=daily"):
            self.assertEqual(EXPECTED_DAILY_NAMES, self.names(await self.listed()))
        with http_context():
            self.assertEqual(full_names, self.names(await self.listed()))

        first_entered = asyncio.Event()
        second_entered = asyncio.Event()

        async def daily_request():
            with http_context(query="tool_profile=daily"):
                first_entered.set()
                await second_entered.wait()
                return self.names(await self.listed())

        async def full_request():
            await first_entered.wait()
            with http_context():
                second_entered.set()
                await asyncio.sleep(0)
                return self.names(await self.listed())

        daily, full = await asyncio.gather(daily_request(), full_request())
        self.assertEqual(EXPECTED_DAILY_NAMES, daily)
        self.assertEqual(full_names, full)
        self.assertEqual(full_names, self.names(await self.listed()))

    async def test_invalid_conflicting_or_duplicate_selectors_fail_without_echo(self):
        marker = "PrivateInputMustNotBeEchoed"
        cases = (
            {"headers": [("x-stbrain-tool-profile", marker)]},
            {"query": "tool_profile=" + marker}, {"query": "tool_profile="},
            {"query": "tool_profile=DAILY"},
            {"headers": [("x-stbrain-tool-profile", "daily")], "query": "tool_profile=full"},
            {"headers": [("x-stbrain-tool-profile", "daily"), ("x-stbrain-tool-profile", "daily")]},
            {"query": "tool_profile=daily&tool_profile=daily"},
        )
        messages = set()
        for options in cases:
            with self.subTest(options=options), http_context(**options):
                with self.assertRaises(McpError) as raised:
                    await self.listed()
                self.assertEqual(types.INVALID_PARAMS, raised.exception.error.code)
                self.assertNotIn(marker, raised.exception.error.message)
                messages.add(raised.exception.error.message)
        self.assertEqual(1, len(messages))
        self.assertIn("tool_profile_invalid", next(iter(messages)))
        self.assertEqual(set(self.original_tools) - FACADES, self.names(await self.listed()))
        self.assertEqual([], self.calls)

    async def test_original_manager_and_guard_are_untouched_and_hidden_call_still_works(self):
        self.assertIs(self.original_list, self.manager.list_tools)
        self.assertEqual(self.original_get, self.manager.get_tool)
        self.assertIs(self.original_call, self.manager.call_tool)
        hidden = self.manager.get_tool("revise_tool_guidance")
        self.assertIsNotNone(hidden)
        with http_context(query="tool_profile=daily"):
            await self.listed()
            result = await self.manager.call_tool("revise_tool_guidance", {"value": "synthetic-direct"})
        self.assertEqual({"value": "synthetic-direct"}, result)
        self.assertEqual(["synthetic-direct"], self.calls)

    async def test_internal_sdk_refresh_and_protocol_calls_ignore_discovery_selector(self):
        call_handler = self.mcp._mcp_server.request_handlers[types.CallToolRequest]
        for query in ("tool_profile=daily", "tool_profile=PrivateInputMustNotBeEchoed"):
            with self.subTest(query=query), http_context(query=query):
                self.mcp._mcp_server._tool_cache.clear()
                internal = await self.handler(None)
                self.assertEqual(set(self.original_tools), self.names(internal))
                self.mcp._mcp_server._tool_cache.clear()
                result = await call_handler(types.CallToolRequest(
                    method="tools/call", params=types.CallToolRequestParams(
                        name="manage_person_reference_advisory", arguments={"value": "synthetic-hidden-call"},
                    ),
                ))
                self.assertFalse(result.root.isError)
                self.assertEqual({"value": "synthetic-hidden-call"}, result.root.structuredContent)
                self.assertEqual(set(self.original_tools), set(self.mcp._mcp_server._tool_cache))

    async def test_existing_guard_denial_is_identical_for_hidden_tools(self):
        async def guarded_call(name, arguments, context=None, convert_result=False):
            return {"decision": "reject", "reason_code": "synthetic_existing_guard", "state_changed": False}

        self.manager.call_tool = guarded_call
        expected = {"decision": "reject", "reason_code": "synthetic_existing_guard", "state_changed": False}
        for query in ("tool_profile=daily", "tool_profile=full"):
            with http_context(query=query):
                await self.listed()
                self.assertIs(guarded_call, self.manager.call_tool)
                self.assertEqual(expected, await self.manager.call_tool("revise_tool_guidance", {}))
        self.assertEqual([], self.calls)

    def test_double_install_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "tool_catalog_profile_already_installed"):
            install_tool_catalog_profile(self.mcp)


if __name__ == "__main__":
    unittest.main()
