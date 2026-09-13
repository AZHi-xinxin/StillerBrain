"""Current tool-card guidance, with fake state and no database/model access."""
from __future__ import annotations

import ast
import asyncio
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator
from mcp.server.fastmcp import FastMCP

from mcp_server.daily_revision_service import DailyRevisionAccessService, ORDINARY_REVISION_FIELDS
from mcp_server.ordinary_revision_schema import install_ordinary_revision_schema
from mcp_server.service import SelfModelAccessService
from mcp_server.simple_tool_catalog import install_simple_tool_catalog
from mcp_server.tool_guidance_service import ToolGuidanceAccessService
from mcp_server.usage_guide import module_usage_guide


class ToolRevisionGuidanceAlignmentTests(unittest.TestCase):
    def setUp(self):
        # Keep socket.socket a real class: Windows Proactor uses isinstance
        # while servicing its local self-pipe. Deny outbound connects instead.
        self.loop = asyncio.new_event_loop()
        self.loop_errors = []
        self.loop.set_exception_handler(lambda _loop, context: self.loop_errors.append(context.get("message")))

        def close_loop():
            self.loop.close()
            self.assertEqual([], self.loop_errors, "event-loop errors must fail the test")

        self.addCleanup(close_loop)
        for target in ("sqlite3.connect", "socket.create_connection", "socket.socket.connect",
                       "socket.socket.connect_ex", "subprocess.Popen"):
            self.enterContext(patch(target, side_effect=AssertionError("offline guidance test")))
        self.owner, self.model = "synthetic-guidance-owner", "synthetic-guidance-model"
        # Avoid the real service constructor, which initializes its store.
        self.tools = object.__new__(ToolGuidanceAccessService)
        self.tools.owner_id, self.tools.model_id = self.owner, self.model
        self.tools.status = Mock(return_value={"row_version": 3})
        self.revisions = DailyRevisionAccessService(
            object(), None, None, None, self.owner, self.model, tool_guidance_service=self.tools)
        self.ref = "tool-card://toolcard_" + "a" * 32 + "@2"

    def test_manual_recommends_current_unified_name_with_explicit_controls(self):
        manual = self.tools.manual()
        self.assertIn("revise_memory", manual["tools"])
        self.assertNotIn("revise_tool_guidance", manual["tools"])
        self.assertNotIn("revise_tool_guidance", json.dumps(manual["principles"]))
        self.assertIn("changes.intent", manual["tools"]["revise_memory"])
        self.assertIn("changes.target_version", manual["tools"]["revise_memory"])
        self.assertIn("changes={'intent':'retire'}", manual["revision_examples"]["retire"])
        self.assertIn("changes={'intent':'restore','target_version':", manual["revision_examples"]["restore"])
        self.assertIn("不用于工具卡", manual["revision_examples"]["lifecycle"])
        self.assertIn("本轮实际工具目录", manual["legacy_compatibility"])
        self.assertIn("changes.expires_at=null", manual["authoring_constraints"]["expires_at"])
        self.assertFalse(manual["execution_performed"])

    def test_actual_selected_manual_projection_keeps_the_same_current_guidance(self):
        facade = object.__new__(SelfModelAccessService)
        for name in ("emotional", "learning", "planning", "governance", "injection_control",
                     "hallucination_vault", "authoring_rewrite"):
            setattr(facade, name, None)
        facade.tool_guidance = self.tools
        facade.ordinary_memory_access = True
        facade._manual_context_ref = Mock(return_value=None)
        facade._module_access_display = Mock(return_value=True)
        facade._module_status_display = Mock(return_value="available")
        result = facade._selected_open_manual({}, {}, {"module_one_unlocked": True}, "tool_guidance")
        self.assertTrue(result["manual_available"])
        self.assertIn("revise_memory", result["tool_guidance"]["tools"])
        self.assertNotIn("revise_tool_guidance", result["tool_guidance"]["tools"])
        self.assertEqual(3, result["tool_guidance"]["tool_row_version"])

    def test_unknown_author_field_rejection_stays_rejected_and_points_to_current_tool(self):
        for changes in ({"lifecycle": "retired"}, {"private_unknown_field": "SYNTHETIC_SECRET_VALUE"}):
            result = self.revisions.revise(self.ref, changes)
            self.assertEqual(["ordinary_revision_requires_advanced"], result["reason_codes"])
            self.assertFalse(result["state_changed"])
            self.assertEqual("revise_memory", result["advanced_tool"])
            self.assertEqual(sorted(ORDINARY_REVISION_FIELDS["tool_guidance"]), result["allowed_fields"])
            self.assertNotIn("lifecycle", result["allowed_fields"])
            self.assertIn("changes.intent", result["next_step"])
            self.assertIn("changes.target_version", result["next_step"])
            encoded = json.dumps(result)
            for absent in ("revise_tool_guidance", "private_unknown_field", "SYNTHETIC_SECRET_VALUE"):
                self.assertNotIn(absent, encoded)
        self.tools.status.assert_not_called()

    def test_backend_rejection_projection_also_uses_current_name(self):
        result = self.revisions._runtime_reject(
            "tool_guidance", {"reason_code": "ordinary_revision_requires_advanced"})
        self.assertEqual("revise_memory", result["advanced_tool"])
        self.assertNotIn("revise_tool_guidance", json.dumps(result))
        self.assertFalse(result["execution_performed"])
        self.assertFalse(result["state_changed"])

    def test_ordinary_authorization_is_not_bypassed_by_new_guidance(self):
        result = self.revisions.revise(self.ref, {"intent": "retire"})
        self.assertEqual(["execution_binding_required"], result["reason_codes"])
        self.assertFalse(result["revised"])
        self.assertFalse(result["state_changed"])
        self.tools.status.assert_not_called()

    def test_static_simple_help_and_legacy_compatibility_are_distinct(self):
        simple = module_usage_guide("tool_guidance", simple=True)
        legacy = module_usage_guide("tool_guidance", simple=False)
        self.assertIn("revise_memory", simple["write_tools"])
        self.assertNotIn("revise_tool_guidance", simple["write_tools"])
        self.assertIn("revise_tool_guidance", legacy["write_tools"])
        for result in (simple, legacy):
            workflow = " ".join(result["workflow"])
            self.assertIn("changes.intent", workflow)
            self.assertIn("changes.target_version", workflow)
            self.assertIn("changes={'intent':'retire'}", workflow)
            self.assertIn("changes={'intent':'restore','target_version':", workflow)
            self.assertIn("不使用 changes.lifecycle", workflow)
            self.assertNotIn("lifecycle", result["revise_author_fields"])
            self.assertIn("本轮实际工具目录", result["compatibility"])

    def test_real_sdk_list_keeps_tool_controls_and_existing_schema_shape(self):
        mcp = FastMCP("synthetic-guidance-schema")

        @mcp.tool()
        async def revise_memory(target_ref: str, changes: dict) -> dict:
            return {}

        @mcp.tool()
        async def revise_tool_guidance(card_id: str) -> dict:
            return {}

        parameters = mcp._tool_manager.get_tool("revise_memory").parameters
        parameters["additionalProperties"] = False
        install_ordinary_revision_schema(parameters)
        before = copy.deepcopy(parameters)
        install_simple_tool_catalog(mcp)
        listed = self.loop.run_until_complete(mcp.list_tools())
        self.assertEqual(["revise_memory"], [tool.name for tool in listed])
        actual = listed[0].inputSchema
        self.assertEqual(before, actual)
        self.assertNotIn("$ref", json.dumps(actual))
        properties = actual["properties"]["changes"]["properties"]
        self.assertIn("intent", properties)
        self.assertIn("target_version", properties)
        self.assertIn("anyOf", properties["intent"])
        self.assertIn("anyOf", properties["target_version"])
        self.assertEqual(["target_ref", "changes"], actual["required"])
        validator = Draft202012Validator(actual)
        for changes in ({"intent": "retire"}, {"intent": "restore", "target_version": 1}):
            self.assertTrue(validator.is_valid({"target_ref": self.ref, "changes": changes}))
        self.assertFalse(validator.is_valid({"target_ref": self.ref, "changes": {"lifecycle": "retired"}}))
        self.assertFalse(validator.is_valid({"target_ref": self.ref, "changes": {"purpose": "Synthetic"},
                                            "intent": "restore", "target_version": 1}))

    def test_registered_function_documentation_uses_nested_controls(self):
        source = Path(__file__).resolve().parents[1] / "server.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(node for node in tree.body
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == "revise_memory")
        doc = ast.get_docstring(function)
        self.assertIn("changes.intent", doc)
        self.assertIn("changes.target_version", doc)
        self.assertIn("changes={'intent':'retire'}", doc)
        self.assertIn("changes={'intent':'restore','target_version':", doc)
        self.assertIn("不使用 changes.lifecycle", doc)
        self.assertIn("网关不会执行目录外的旧名称", doc)


if __name__ == "__main__":
    unittest.main()
