"""Small discovery projections without altering registered contracts or advice."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from mcp import types
from mcp.server.fastmcp import FastMCP

from mcp_server.compact_catalog_schema import (
    COMPACT_ORDINARY_TOOL_NAMES, project_compact_tool,
)
from mcp_server.ordinary_revision_schema import install_ordinary_revision_schema
from mcp_server.person_reference_surface import install_person_reference_advisory_surface
from mcp_server.usage_guide import stored_memory_wording_advisory


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def revision_tool():
    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["target_ref", "changes"],
        "properties": {
            "target_ref": {"type": "string", "minLength": 1, "maxLength": 100},
            "changes": {"type": "object"},
            "reason": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
            "execution_ref": {
                "type": "string", "pattern": "^stexec_[A-Za-z0-9_-]{43}$",
                "description": "Reserved host binding; do not invent it.",
                "x-stbrain-execution-tool": "revise_memory", "x-stbrain-execution-contract": "synthetic/1",
            },
        },
    }
    install_ordinary_revision_schema(schema)
    return types.Tool(name="revise_memory", description="Long original description.", inputSchema=schema,
                      annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False),
                      _meta={"synthetic": {"description": "Metadata is not schema prose."}})


class CompactCatalogSchemaTests(unittest.TestCase):
    def test_revision_author_map_is_small_but_keeps_outer_contract(self):
        original = revision_tool()
        before = original.model_dump(by_alias=True)
        compact = project_compact_tool(original)
        self.assertLess(encoded_size(compact.inputSchema), encoded_size(original.inputSchema) * .12)
        self.assertNotIn("allOf", compact.inputSchema)
        self.assertTrue(compact.inputSchema["properties"]["changes"]["additionalProperties"])
        self.assertEqual(1, compact.inputSchema["properties"]["changes"]["minProperties"])
        self.assertEqual(original.inputSchema["required"], compact.inputSchema["required"])
        self.assertEqual(set(original.inputSchema["properties"]), set(compact.inputSchema["properties"]))
        self.assertFalse(compact.inputSchema["additionalProperties"])
        self.assertEqual(original.inputSchema["properties"]["target_ref"]["pattern"],
                         compact.inputSchema["properties"]["target_ref"]["pattern"])
        self.assertEqual(before, original.model_dump(by_alias=True))
        self.assertIsNot(original, compact)

    def test_execution_and_tool_metadata_are_unchanged_detached_copies(self):
        original = revision_tool()
        compact = project_compact_tool(original)
        self.assertEqual(original.inputSchema["properties"]["execution_ref"],
                         compact.inputSchema["properties"]["execution_ref"])
        self.assertEqual(original.annotations, compact.annotations)
        self.assertEqual(original.meta, compact.meta)
        compact.inputSchema["properties"]["execution_ref"]["description"] = "Mutated copy"
        compact.meta["synthetic"]["description"] = "Mutated metadata"
        self.assertNotEqual(original.meta, compact.meta)
        self.assertNotEqual(original.inputSchema, compact.inputSchema)

    def test_unrelated_top_level_conditions_are_not_removed(self):
        tool = revision_tool()
        independent = {"if": {"required": ["reason"]}, "then": {"properties": {"reason": {"maxLength": 2000}}}}
        tool.inputSchema["allOf"].append(independent)
        compact = project_compact_tool(tool)
        self.assertEqual([independent], compact.inputSchema["allOf"])

    def test_property_names_and_literal_payloads_are_not_mistaken_for_annotations(self):
        literal = {"description": "Literal author content.", "title": "Literal title."}
        tool = types.Tool(name="remember_memory", description="Original", inputSchema={
            "title": "Generated model title", "type": "object", "required": ["title", "description"],
            "properties": {
                "title": {"type": "string", "title": "Field title", "minLength": 1, "maxLength": 100},
                "description": {"type": "object", "default": literal, "const": literal,
                                "properties": {"title": {"type": "string"}, "description": {"type": "string"}}},
                "kind": {"enum": ["fact", "task"], "default": "fact"},
                "count": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "x-contract": literal,
        })
        compact = project_compact_tool(tool).inputSchema
        self.assertNotIn("title", compact)
        self.assertEqual({"title", "description", "kind", "count"}, set(compact["properties"]))
        self.assertEqual({"type": "string", "minLength": 1, "maxLength": 100}, compact["properties"]["title"])
        self.assertEqual(literal, compact["properties"]["description"]["default"])
        self.assertEqual(literal, compact["properties"]["description"]["const"])
        self.assertEqual(literal, compact["x-contract"])
        self.assertEqual(["fact", "task"], compact["properties"]["kind"]["enum"])
        self.assertEqual({"type": "integer", "minimum": 1, "maximum": 5}, compact["properties"]["count"])

    def test_only_five_known_entrypoints_are_projected(self):
        self.assertEqual({"remember_memory", "remember_tool_guidance", "revise_memory", "advance_plan", "stbrain_open"},
                         set(COMPACT_ORDINARY_TOOL_NAMES))
        other = types.Tool(name="submit_self_model_candidate", description="Preserve strict original",
                           inputSchema={"type": "object", "properties": {}})
        self.assertIs(other, project_compact_tool(other))

    def test_property_only_clients_retain_required_and_constraint_fields(self):
        tool = revision_tool()
        compact = project_compact_tool(tool)
        client_schema = {key: deepcopy(compact.inputSchema[key]) for key in ("type", "properties", "required")}
        self.assertEqual(["target_ref", "changes"], client_schema["required"])
        self.assertIn("pattern", client_schema["properties"]["target_ref"])
        self.assertTrue(client_schema["properties"]["changes"]["additionalProperties"])
        self.assertNotIn("$ref", json.dumps(client_schema))

    def test_unknown_authoring_surface_is_not_silently_truncated(self):
        description = "存入前·可选提醒\nNew surface framing\n\nAuthor multiline reminder."
        tool = types.Tool(name="remember_memory", description=description,
                          inputSchema={"type": "object", "properties": {}})
        self.assertEqual(description, project_compact_tool(tool).description)


class CompactCatalogAdvisoryTests(unittest.IsolatedAsyncioTestCase):
    async def listed(self, advisory):
        mcp = FastMCP("synthetic-compact-advisory")

        async def save(content: str):
            return {"content": content}

        for name in ("remember_memory", "remember_tool_guidance"):
            mcp.add_tool(save, name=name, description="Original long body " * 100)
        before = {tool.name: deepcopy(tool.parameters) for tool in mcp._tool_manager.list_tools()}
        install_person_reference_advisory_surface(mcp, SimpleNamespace(advisory_status=lambda: advisory))
        original = await mcp.list_tools()
        compact = [project_compact_tool(tool) for tool in original]
        self.assertEqual(before, {tool.name: tool.parameters for tool in mcp._tool_manager.list_tools()})
        return original, compact

    async def test_full_multiline_custom_advice_and_wording_are_retained(self):
        sentinel = "这里是工具目录的偏好快照；本轮管理结果优先，客户端刷新 MCP 工具目录后可看到新偏好。"
        custom = "我和昕昕记录自己的故事。\n\n" + sentinel + "\n\n这仍然是我自己写的提醒。"
        original, compact = await self.listed({"enabled": True, "message": custom})
        for full, short in zip(original, compact):
            self.assertIn(custom, short.description)
            self.assertIn(stored_memory_wording_advisory()["message"], short.description)
            self.assertIn("无需为提醒增加预览或确认调用", short.description)
            self.assertIn("本轮管理结果优先", short.description)
            self.assertIn("manage_person_reference_advisory", short.description)
            self.assertIn("可修改、关闭或恢复", short.description)
            self.assertLess(len(short.description), len(full.description))
            self.assertNotIn("Original long body", short.description)
            self.assertIn("Original long body", full.description)

    async def test_disabled_advice_stays_disabled_without_resurrecting_text(self):
        _, compact = await self.listed({"enabled": False, "message": "DISABLED_PRIVATE_BODY"})
        for tool in compact:
            self.assertIn("人称提醒已关闭", tool.description)
            self.assertNotIn("DISABLED_PRIVATE_BODY", tool.description)
            self.assertIn(stored_memory_wording_advisory()["message"], tool.description)

    async def test_unavailable_advice_does_not_block_discovery_or_invent_default(self):
        _, compact = await self.listed(None)
        for tool in compact:
            self.assertIn("人称提醒当前不可读取", tool.description)
            self.assertIn(stored_memory_wording_advisory()["message"], tool.description)


if __name__ == "__main__":
    unittest.main()
