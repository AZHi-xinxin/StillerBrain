"""Real tools/list root contracts, isolated from user stores and the network.

The server and a client's provider conversion are separate boundaries.  A valid
conditional schema must not be weakened merely because a client rewrites it.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator


def assert_object_contract(schema):
    assert isinstance(schema, dict), "input_schema_not_mapping"
    assert schema.get("type") == "object", "input_schema_root_not_object"
    assert isinstance(schema.get("properties"), dict), "input_schema_properties_missing"
    assert set(schema.get("required", [])) <= set(schema["properties"]), "unknown_required_property"
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    for non_object in (None, [], "synthetic", 1, True):
        assert not validator.is_valid(non_object), "non_object_arguments_accepted"


def conditional_first_branch_loss(schema):
    """Minimal independent model of the old client's lossy root transformation.

    This is not the Kelivo executable or a general replacement for its converter.
    It models only: discard if/then from the first conditional branch, then
    replace the enclosing root's structural fields with that empty projection.
    The public source before the fix is pinned here for reproducibility:
    https://github.com/Chevey339/kelivo/blob/c58cc4a19bd974ccd1be584413c92a02801a15af/lib/features/home/services/tool_handler_service.dart#L133-L205
    The non-Google preservation fix is commit 37e229962aa469bcad3dedf15ef52a7d5b023f33.
    No third-party implementation is imported or executed by these tests.
    """
    result = deepcopy(schema)
    branches = result.pop("allOf", None)
    if branches:
        assert set(branches[0]) == {"if", "then"}, "fixture_is_not_conditional_only"
        for field in ("type", "properties", "items"):
            result.pop(field, None)
    return result


class ToolSchemaRootTests(unittest.TestCase):
    def run_registered_probe(self, *, simple, execution, optional=False):
        allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATH", "PATHEXT", "TEMP", "TMP", "OS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="synthetic-tool-schema-roots-") as folder:
            root = Path(folder).resolve()
            env.update({
                "STBRAIN_MCP_TOKEN": "synthetic-schema-mcp-" + "m" * 40,
                "STBRAIN_WAKE_SECRET": "synthetic-schema-wake-" + "w" * 40,
                "STBRAIN_OWNER_ID": "synthetic-schema-owner",
                "STBRAIN_MODEL_ID": "synthetic-schema-model",
                "STBRAIN_DB_PATH": str(root / "main.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(root / "ideas.db"),
                "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(root / "vault.db"),
                "STBRAIN_REQUIRE_EXECUTION_BINDING": str(int(execution)),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                "PYTHONIOENCODING": "utf-8",
            })
            if simple:
                env["STBRAIN_ACCESS_PROFILE"] = "simple-memory-v1"
            if execution or optional:
                env["STBRAIN_EXECUTION_EPOCH"] = "synthetic-schema-epoch"
            completed = subprocess.run(
                [sys.executable, "-B", "-m", "mcp_server.tests.test_tool_schema_roots", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding="utf-8", timeout=90,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertTrue(result["passed"])
        self.assertEqual(0, result["network_attempts"])
        self.assertFalse(result["real_memory_used"])
        self.assertGreaterEqual(result["full_tool_count"], 50)
        self.assertEqual(9, result["daily_tool_count"])
        self.assertEqual(7, result["wire_views_checked"])

    def test_legacy_without_execution_binding(self):
        self.run_registered_probe(simple=False, execution=False)

    def test_legacy_with_execution_binding(self):
        self.run_registered_probe(simple=False, execution=True)

    def test_simple_without_execution_binding(self):
        self.run_registered_probe(simple=True, execution=False)

    def test_simple_with_execution_binding(self):
        self.run_registered_probe(simple=True, execution=True)

    def test_legacy_with_optional_execution_binding(self):
        self.run_registered_probe(simple=False, execution=False, optional=True)

    def test_simple_with_optional_execution_binding(self):
        self.run_registered_probe(simple=True, execution=False, optional=True)

    def test_root_assertion_rejects_null_missing_and_non_object_types(self):
        for schema in (None, {}, {"type": None}, {"type": "null"}, {"type": ["object", "null"]},
                       {"type": "object", "properties": {}, "required": ["absent"]}):
            with self.subTest(schema=schema), self.assertRaises(AssertionError):
                assert_object_contract(schema)

    def test_conditional_schema_is_valid_before_lossy_client_projection(self):
        schema = {"type": "object", "properties": {"target_ref": {"type": "string"}},
                  "allOf": [{"if": {"required": ["target_ref"]},
                             "then": {"properties": {"target_ref": {"minLength": 1}}}}]}
        assert_object_contract(schema)
        self.assertIsNone(conditional_first_branch_loss(schema).get("type"))
        # Preserving the non-Google JSON Schema keeps both the root and its
        # conditional constraints; it is not equivalent to just adding type back.
        preserved = deepcopy(schema)
        assert_object_contract(preserved)
        self.assertFalse(Draft202012Validator(preserved).is_valid({"target_ref": ""}))
        self.assertEqual("object", schema["type"])


async def probe():
    from mcp import types
    from mcp.server.lowlevel.server import request_ctx
    from mcp.shared.context import RequestContext
    from starlette.requests import Request
    from tests.sqlite_fixture_guard import is_synthetic_sqlite_path

    root = Path(os.environ["STBRAIN_DB_PATH"]).resolve().parent
    allowed_databases = {root / name for name in ("main.db", "ideas.db", "vault.db")}
    original_connect = sqlite3.connect
    attempts = 0

    def connect(path, *args, **kwargs):
        assert is_synthetic_sqlite_path(path, allowed_databases, uri=kwargs.get("uri")), "outside_synthetic_database"
        return original_connect(path, *args, **kwargs)

    def deny(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise AssertionError("network_or_child_process_forbidden")

    @contextmanager
    def http_context(*, query="", headers=()):
        request = Request({"type": "http", "method": "POST", "path": "/mcp",
                           "query_string": query.encode("ascii"),
                           "headers": [(key.encode("ascii"), value.encode("ascii")) for key, value in headers]})
        token = request_ctx.set(RequestContext(request_id="synthetic-schema-request", meta=None,
                                              session=None, lifespan_context=None, request=request))
        try:
            yield
        finally:
            request_ctx.reset(token)

    with ExitStack() as guards:
        guards.enter_context(patch("sqlite3.connect", side_effect=connect))
        for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                       "socket.socket.bind", "socket.socket.sendto", "socket.getaddrinfo", "subprocess.Popen"):
            guards.enter_context(patch(target, side_effect=deny))
        from mcp_server import server
        from mcp_server.tool_catalog_profile import DAILY_TOOL_NAMES
        from runtime.memory_image_uploads import UPLOAD_TOOL

        manager = server.mcp._tool_manager
        registered = {tool.name: deepcopy(tool.parameters) for tool in manager.list_tools()}
        for schema in registered.values():
            assert_object_contract(schema)
        # Compatibility-only entries can be hidden from list_tools, but their
        # retained manager schema must not become a null-root input either.
        compatibility = manager.get_tool("revise_tool_guidance")
        assert compatibility is not None
        assert_object_contract(compatibility.parameters)
        handler = server.mcp._mcp_server.request_handlers[types.ListToolsRequest]

        async def wire(request):
            result = await handler(request)
            payload = json.loads(result.model_dump_json(by_alias=True))
            tools = payload["tools"]
            assert len({tool["name"] for tool in tools}) == len(tools), "duplicate_tool_name"
            schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
            for schema in schemas.values():
                assert_object_contract(schema)
            return schemas

        internal = await wire(None)
        assert internal == registered, "sdk_refresh_schema_changed"
        default = await wire(types.ListToolsRequest(method="tools/list"))
        assert set(default) == set(registered) - {"stbrain_tools", "stbrain_manage"}
        assert default == {name: registered[name] for name in default}
        for options in ({}, {"query": "tool_profile=full"},
                        {"headers": [("x-stbrain-tool-profile", "full")]}):
            with http_context(**options):
                assert await wire(types.ListToolsRequest(method="tools/list")) == default
        for options in ({"query": "tool_profile=daily"},
                        {"headers": [("x-stbrain-tool-profile", "daily")]}):
            with http_context(**options):
                daily = await wire(types.ListToolsRequest(method="tools/list"))
            assert set(daily) == set(DAILY_TOOL_NAMES) | {UPLOAD_TOOL}
            revision = daily["revise_memory"]
            assert "allOf" not in revision, "compact_revision_conditionals_returned"
            assert_object_contract(conditional_first_branch_loss(revision))
            assert {"target_ref", "changes"} <= set(revision["required"])
            assert revision["properties"]["target_ref"]["pattern"] == default["revise_memory"]["properties"]["target_ref"]["pattern"]
        revision = default["revise_memory"]
        assert len(revision["allOf"]) == 4, "revision_module_constraints_missing"
        assert {"target_ref", "changes"} <= set(revision["required"])
        assert conditional_first_branch_loss(revision).get("type") is None
        assert_object_contract(deepcopy(revision))
        assert ("execution_ref" in revision["properties"]) == bool(os.environ.get("STBRAIN_EXECUTION_EPOCH"))
        assert {tool.name: tool.parameters for tool in manager.list_tools()} == registered, "discovery_mutated_manager"
        assert {name: tool.inputSchema for name, tool in server.mcp._mcp_server._tool_cache.items()} == registered, "daily_mutated_sdk_cache"
        assert attempts == 0
        print(json.dumps({"passed": True, "network_attempts": attempts, "real_memory_used": False,
                          "full_tool_count": len(default), "daily_tool_count": len(daily),
                          "wire_views_checked": 7}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        # The event loop is created before denying sockets; Windows uses a
        # local socket pair for loop startup, not for an external connection.
        asyncio.run(probe())
    else:
        unittest.main()
