"""Value-free extra-field census: synthetic schemas/arguments only."""
from __future__ import annotations
import asyncio
import copy
import json
import unittest

from jsonschema import Draft202012Validator, ValidationError
from mcp.server.fastmcp import FastMCP
from mcp_server.public_contract import PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA
from rikkahub_gateway.server import _safe_validation_diagnostic, _tool_validation_message
from rikkahub_gateway.tool_execution import (
    ToolExecutionBoundaryError, _additional_properties_detail, normalize_validation_diagnostic,
)
from tests import test_tool_validation_diagnostics as fixtures

NAME = "mcp__StillerBrain__preview_person_reference_rewrite"
SECRET_KEY = "Private arbitrary person's name as field"
SECRET_VALUE = "Private draft body must never appear"


def schema(fields=None):
    draft = copy.deepcopy(PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA["properties"]["draft_fields"])
    if fields is not None:
        draft["properties"] = {field: {"type": "string"} for field in fields}
    return {"type": "object", "properties": {"draft_fields": draft}}


class AdditionalPropertiesDiagnosticTests(unittest.TestCase):
    setUp = fixtures.ToolValidationDiagnosticTests.setUp
    bind = fixtures.ToolValidationDiagnosticTests.bind

    def rejected(self, fields, selected=None):
        with self.assertRaises(ToolExecutionBoundaryError) as caught:
            self.bind(selected or schema(), {"draft_fields": fields}, name=NAME)
        return caught.exception.validation_diagnostic

    def test_bare_known_field_identifies_name_and_actual_allowed_paths(self):
        result = self.rejected({"summary": SECRET_VALUE})
        self.assertEqual(["summary"], result["unexpected_fields"])
        self.assertEqual((1, 0, 12), (result["unexpected_count"], result["unknown_count"], result["allowed_field_count"]))
        self.assertIn("/summary", result["allowed_fields"])
        self.assertFalse(result["allowed_fields_truncated"])
        self.assertNotIn(SECRET_VALUE, json.dumps(result) + _tool_validation_message(result))

    def test_correct_slash_missing_in_client_schema_is_distinguishable(self):
        result = self.rejected({"/summary": SECRET_VALUE}, schema(["/title"]))
        self.assertEqual(["/summary"], result["unexpected_fields"])
        self.assertEqual(["/title"], result["allowed_fields"])
        self.assertEqual(1, result["allowed_field_count"])

    def test_different_public_field_is_named_but_secret_keys_and_values_are_hidden(self):
        result = self.rejected({"current_understanding": SECRET_VALUE, SECRET_KEY: SECRET_VALUE,
                                "Another arbitrary secret key": {"value": SECRET_VALUE}})
        self.assertEqual(["current_understanding", "<field>"], result["unexpected_fields"])
        self.assertEqual((3, 2), (result["unexpected_count"], result["unknown_count"]))
        encoded = json.dumps(result) + _tool_validation_message(result)
        for item in (SECRET_KEY, SECRET_VALUE, "Another arbitrary secret key"):
            self.assertNotIn(item, encoded)

    def test_unknown_allowed_property_is_counted_but_not_revealed(self):
        result = self.rejected({"summary": SECRET_VALUE}, schema(["/summary", SECRET_KEY]))
        self.assertEqual(2, result["allowed_field_count"])
        self.assertEqual(["/summary"], result["allowed_fields"])
        self.assertTrue(result["allowed_fields_truncated"])
        self.assertNotIn(SECRET_KEY, json.dumps(result))

    def test_known_lists_are_bounded_with_explicit_truncation_and_exact_counts(self):
        public = ["original_text", "summary", "title", "current_understanding", "purpose",
                  "call_notes", "intent", "target_version", "confidence", "reminder", "module",
                  "query", "limit", "reason", "target_ref", "changes", "keywords", "origin"]
        result = self.rejected({key: SECRET_VALUE for key in public}, schema([]))
        self.assertEqual(18, result["unexpected_count"])
        self.assertEqual(6, len(result["unexpected_fields"]))
        self.assertTrue(result["unexpected_fields_truncated"])
        other = self.rejected({SECRET_KEY: SECRET_VALUE}, schema(public))
        self.assertEqual(18, other["allowed_field_count"])
        self.assertEqual(16, len(other["allowed_fields"]))
        self.assertTrue(other["allowed_fields_truncated"])

    def test_large_key_map_omits_optional_census_without_inaccurate_counts(self):
        result = self.rejected({"synthetic-private-key-" + str(i): "synthetic" for i in range(1025)}, schema([]))
        self.assertNotIn("unexpected_count", result)
        self.assertEqual("additionalProperties", result["validator"])
        self.assertEqual(["draft_fields", "<field>"], result["field_path"])

    def test_complex_schema_gets_no_new_metadata_and_does_not_reexecute_regex(self):
        for extra in ({"patternProperties": {}}, {"allOf": []}, {"$ref": "#/$defs/unused"}):
            error = ValidationError("not logged", validator="additionalProperties", instance={SECRET_KEY: SECRET_VALUE},
                schema={"properties": {}, "additionalProperties": False, **extra})
            self.assertEqual({}, _additional_properties_detail(error))

    def test_mapping_values_are_never_accessed_by_census(self):
        class KeysOnly(dict):
            def __getitem__(self, key):
                raise AssertionError("census must not inspect a value")
        error = ValidationError("not logged", validator="additionalProperties", instance=KeysOnly(summary=SECRET_VALUE),
            schema={"properties": {}, "additionalProperties": False})
        self.assertEqual(["summary"], _additional_properties_detail(error)["unexpected_fields"])

    def test_only_exact_false_simple_mapping_schema_gets_metadata(self):
        for bad_schema, instance in (({"properties": {}, "additionalProperties": True}, {}),
                                     ({"additionalProperties": False}, {}),
                                     ({"properties": [], "additionalProperties": False}, {}),
                                     ({"properties": {}, "additionalProperties": False}, [])):
            error = ValidationError("not logged", validator="additionalProperties", instance=instance, schema=bad_schema)
            self.assertEqual({}, _additional_properties_detail(error))

    def test_normalization_rejects_forged_private_labels_counts_and_oversize_lists(self):
        original = self.rejected({"summary": SECRET_VALUE})
        for changes in ({"unexpected_fields": [SECRET_KEY]}, {"allowed_fields": [SECRET_KEY]},
                        {"unexpected_count": True}, {"unknown_count": -1}, {"unexpected_count": 1025},
                        {"unknown_count": 2}, {"allowed_fields": ["summary"] * 17},
                        {"allowed_fields_truncated": "false"}, {"unexpected_fields_truncated": True}):
            forged = {**original, **changes}
            result = normalize_validation_diagnostic(forged, allowed_tool_names=[NAME])
            self.assertNotIn("unexpected_fields", result)
            self.assertNotIn(SECRET_KEY, json.dumps(result))

    def test_current_catalog_membership_and_protected_values_still_drop_diagnostic(self):
        original = self.rejected({"summary": SECRET_VALUE})
        self.assertIsNone(_safe_validation_diagnostic(original, allowed_tool_names=["other_tool"]))
        self.assertIsNone(_safe_validation_diagnostic(original, allowed_tool_names=[NAME], protected_values=("/purpose",)))
        message = _tool_validation_message(original, protected_values=("当前字段表共允许",))
        self.assertNotIn("当前字段表共允许", message)

    def test_real_sdk_published_draft_keys_accept_correct_three_learning_paths(self):
        mcp = FastMCP("synthetic-schema-only")
        @mcp.tool()
        async def preview_person_reference_rewrite(draft_fields: dict):
            return {}
        mcp._tool_manager.get_tool("preview_person_reference_rewrite").parameters = copy.deepcopy(PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA)
        tools = asyncio.run(mcp.list_tools())
        field_schema = tools[0].inputSchema["properties"]["draft_fields"]
        fields = {"/summary": SECRET_VALUE, "/title": "Synthetic", "/current_understanding": "Synthetic"}
        Draft202012Validator(field_schema).validate(fields)
        selected = {"type": "object", "properties": {"draft_fields": field_schema}}
        binding = self.bind(selected, {"draft_fields": fields}, name=NAME)
        self.assertEqual(NAME, binding.tool_name)


if __name__ == "__main__":
    unittest.main()
