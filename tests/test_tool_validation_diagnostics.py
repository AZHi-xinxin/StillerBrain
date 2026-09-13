"""Synthetic, value-free diagnostics; no network, database or model calls."""

from __future__ import annotations

import copy
import json
import unittest

from rikkahub_gateway.tool_execution import (
    HostExecutionBoundary,
    NativeToolCall,
    ToolExecutionBoundaryError,
    advertised_schemas,
    canonical_hash,
    normalize_validation_diagnostic,
)


class ToolValidationDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        self.boundary = HostExecutionBoundary(b"synthetic-diagnostic-secret-only!" * 2)

    def bind(self, schema, arguments, *, name="revise_memory", advertised_name=None):
        advertised_name = advertised_name or name
        payload = {"tools": [{"type": "function", "function": {
            "name": advertised_name, "parameters": schema,
        }}]}
        schemas = advertised_schemas(payload)
        catalog = {
            "contract": "advertised-tools/1", "catalog_complete": True,
            "catalog_hash": canonical_hash({"synthetic": advertised_name}),
            "entries": [{"canonical_name": advertised_name, "schema_hash": canonical_hash(schema)}],
        }
        raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        return self.boundary.bind_call(
            wake_id="synthetic-wake", catalog=catalog, schemas=schemas,
            call=NativeToolCall("synthetic-call", name, raw),
        )

    def rejected(self, schema, arguments):
        try:
            self.bind(schema, arguments)
        except ToolExecutionBoundaryError as error:
            self.assertEqual("tool_arguments_schema_invalid", str(error))
            # The source ValidationError carries arbitrary instance/schema data;
            # even the implicit context must not survive on the outward error.
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
            self.assertIsNotNone(error.validation_diagnostic)
            return error
        self.fail("invalid synthetic arguments unexpectedly bound")

    def test_valid_call_is_unchanged(self):
        schema = {"type": "object", "properties": {"summary": {"type": "string"}},
                  "required": ["summary"], "additionalProperties": False}
        before = copy.deepcopy(schema)
        binding = self.bind(schema, '{ "summary": "synthetic value" }')
        self.assertEqual(canonical_hash({"summary": "synthetic value"}), binding.arguments_hash)
        self.assertEqual(before, schema)

    def test_required_reports_safe_missing_labels_only(self):
        schema = {"type": "object", "required": ["target_ref", "changes"],
                  "properties": {"target_ref": {"type": "string"}, "changes": {"type": "object"}}}
        error = self.rejected(schema, {"private_request_key": "private request value"})
        diagnostic = error.validation_diagnostic
        self.assertEqual("required", diagnostic["validator"])
        self.assertEqual([], diagnostic["field_path"])
        self.assertEqual(["target_ref", "changes"], diagnostic["required_fields"])
        self.assertEqual("revise_memory", diagnostic["tool_name"])
        self.assertNotIn("private", json.dumps(diagnostic))

    def test_additional_properties_never_reports_unpublished_keys(self):
        schema = {"type": "object", "properties": {"summary": {"type": "string"}},
                  "additionalProperties": False}
        arguments = {f"UNPUBLISHED_SECRET_KEY_{index}": f"PRIVATE_VALUE_{index}" for index in range(1000)}
        arguments["summary"] = "private summary"
        diagnostic = self.rejected(schema, arguments).validation_diagnostic
        self.assertEqual("additionalProperties", diagnostic["validator"])
        encoded = json.dumps(diagnostic)
        for forbidden in ("UNPUBLISHED", "PRIVATE", "private summary"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(1000, diagnostic["unexpected_count"])
        self.assertEqual(1000, diagnostic["unknown_count"])
        self.assertEqual(["<field>"], diagnostic["unexpected_fields"])
        self.assertLess(len(encoded), 512)

    def test_enum_omits_actual_value_and_schema_candidates(self):
        schema = {"type": "object", "properties": {"origin": {"enum": ["PRIVATE_CANDIDATE"]}}}
        diagnostic = self.rejected(schema, {"origin": "PRIVATE_ACTUAL_VALUE"}).validation_diagnostic
        self.assertEqual("enum", diagnostic["validator"])
        self.assertEqual(["origin"], diagnostic["field_path"])
        self.assertNotIn("PRIVATE", json.dumps(diagnostic))

    def test_type_array_path_uses_schema_fields_and_index_placeholder(self):
        schema = {"type": "object", "properties": {"steps": {"type": "array", "items": {
            "type": "object", "properties": {"summary": {"type": "string"}},
        }}}}
        diagnostic = self.rejected(schema, {"steps": [{"summary": 918273}]}).validation_diagnostic
        self.assertEqual("type", diagnostic["validator"])
        self.assertEqual(["steps", "[]", "summary"], diagnostic["field_path"])
        self.assertNotIn("918273", json.dumps(diagnostic))

    def test_oneof_and_anyof_branch_summaries_are_bounded(self):
        fields = ["summary", "origin", "confidence", "title", "kind", "target_ref", "changes"]
        for keyword in ("oneOf", "anyOf"):
            with self.subTest(keyword=keyword):
                schema = {"type": "object", keyword: [
                    {"required": [field], "properties": {field: {"enum": ["PRIVATE_CANDIDATE"]}}}
                    for field in fields
                ]}
                diagnostic = self.rejected(schema, {"PRIVATE_INSTANCE_KEY": "PRIVATE_INSTANCE_VALUE"}).validation_diagnostic
                self.assertEqual(keyword, diagnostic["validator"])
                self.assertEqual(4, len(diagnostic["branch_errors"]))
                self.assertTrue(all(branch["validator"] == "required" for branch in diagnostic["branch_errors"]))
                self.assertNotIn("PRIVATE", json.dumps(diagnostic))

    def test_oneof_multiple_matches_still_rejects_without_dumping_schemas(self):
        schema = {"type": "object", "oneOf": [
            {"description": "PRIVATE_BRANCH_1"}, {"description": "PRIVATE_BRANCH_2"},
        ]}
        diagnostic = self.rejected(schema, {"summary": "PRIVATE_VALUE"}).validation_diagnostic
        self.assertEqual("oneOf", diagnostic["validator"])
        self.assertNotIn("branch_errors", diagnostic)
        self.assertNotIn("PRIVATE", json.dumps(diagnostic))

    def test_pattern_property_regex_and_actual_key_are_redacted(self):
        schema = {"type": "object", "properties": {"changes": {
            "type": "object", "patternProperties": {"^PRIVATE_MATCH_": {"type": "integer"}},
        }}}
        diagnostic = self.rejected(schema, {"changes": {"PRIVATE_MATCH_REAL_KEY": "PRIVATE_VALUE"}}).validation_diagnostic
        self.assertEqual(["changes", "<field>"], diagnostic["field_path"])
        self.assertNotIn("PRIVATE", json.dumps(diagnostic))

    def test_even_ascii_schema_property_names_need_fixed_public_whitelist(self):
        for private_key in ("SafeLookingPrivateSecret", "sk_abcdefghijklmnopqrstuvwxyz", "password", "秘密字段/~/正文"):
            with self.subTest(private_key=private_key):
                schema = {"type": "object", "properties": {private_key: {"type": "integer"}}}
                diagnostic = self.rejected(schema, {private_key: "PRIVATE_VALUE"}).validation_diagnostic
                self.assertEqual(["<field>"], diagnostic["field_path"])
                self.assertNotIn(private_key, json.dumps(diagnostic, ensure_ascii=False))

    def test_unknown_required_names_are_placeholders_and_bounded(self):
        schema = {"type": "object", "required": [f"PRIVATE_SCHEMA_NAME_{index}" for index in range(200)]}
        diagnostic = self.rejected(schema, {}).validation_diagnostic
        self.assertEqual(["<field>"], diagnostic["required_fields"])
        self.assertNotIn("PRIVATE", json.dumps(diagnostic))

    def test_propertynames_does_not_use_instance_path_or_message(self):
        schema = {"type": "object", "propertyNames": {"pattern": "^PUBLIC_ONLY$"}}
        diagnostic = self.rejected(schema, {"PRIVATE_KEY": "PRIVATE_VALUE"}).validation_diagnostic
        self.assertEqual("pattern", diagnostic["validator"])
        self.assertEqual(["<field>"], diagnostic["field_path"])
        self.assertNotIn("PRIVATE", json.dumps(diagnostic))
        self.assertNotIn("PUBLIC_ONLY", json.dumps(diagnostic))

    def test_local_reference_labels_and_schema_description_do_not_escape(self):
        schema = {"type": "object", "$defs": {"PRIVATE_DEFINITION": {"type": "integer"}},
                  "properties": {"confidence": {"$ref": "#/$defs/PRIVATE_DEFINITION",
                                                "description": "PRIVATE_SCHEMA_DESCRIPTION"}}}
        diagnostic = self.rejected(schema, {"confidence": "PRIVATE_VALUE"}).validation_diagnostic
        self.assertEqual("type", diagnostic["validator"])
        self.assertEqual(["confidence"], diagnostic["field_path"])
        self.assertNotIn("PRIVATE", json.dumps(diagnostic))

    def test_parse_and_unknown_tool_rejections_keep_original_codes_without_diagnostics(self):
        for arguments, name, advertised, expected in (
            ('{"summary":', "revise_memory", "revise_memory", "tool_arguments_invalid_json"),
            ({}, "unknown_tool", "revise_memory", "tool_not_advertised"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaises(ToolExecutionBoundaryError) as caught:
                    self.bind({"type": "object"}, arguments, name=name, advertised_name=advertised)
                self.assertEqual(expected, str(caught.exception))
                self.assertIsNone(caught.exception.validation_diagnostic)

    def test_exception_metadata_is_independent_and_resanitized(self):
        error = self.rejected({"type": "object", "required": ["summary"]}, {})
        projection = error.validation_diagnostic
        projection["field_path"].append("PRIVATE_ADDED_AFTERWARD")
        projection["schema"] = {"PRIVATE_SCHEMA": "PRIVATE_VALUE"}
        self.assertNotIn("PRIVATE", json.dumps(error.validation_diagnostic))
        fabricated = ToolExecutionBoundaryError("tool_arguments_schema_invalid", validation_diagnostic={
            "tool_name": "revise_memory", "validator": "PRIVATE_VALIDATOR", "field_path": ["PRIVATE_KEY"],
            "required_fields": ["PRIVATE_REQUIRED"], "message": "PRIVATE_MESSAGE", "instance": "PRIVATE_INSTANCE",
        })
        self.assertEqual("other", fabricated.validation_diagnostic["validator"])
        self.assertNotIn("PRIVATE", json.dumps(fabricated.validation_diagnostic))
        self.assertIsNone(normalize_validation_diagnostic(
            fabricated.validation_diagnostic, allowed_tool_names={"different_advertised_tool"},
        ))
        self.assertIsNotNone(normalize_validation_diagnostic(
            fabricated.validation_diagnostic, allowed_tool_names={"revise_memory"},
        ))
        spoofed = {**fabricated.validation_diagnostic, "tool_name": "SafeLookingPrivateSecret"}
        self.assertIsNone(normalize_validation_diagnostic(spoofed, allowed_tool_names={"revise_memory"}))

    def test_protected_value_collision_can_drop_whole_projection(self):
        error = self.rejected({"type": "object", "required": ["summary"]}, {})

        def external_projection(protected_values):
            # Consumer contract: compare every protected value to the complete
            # small JSON projection and omit it as one item on collision.
            diagnostic = error.validation_diagnostic
            encoded = json.dumps(diagnostic, ensure_ascii=False)
            return None if any(value and value in encoded for value in protected_values) else diagnostic

        self.assertIsNotNone(external_projection(("synthetic-unrelated-protected-value",)))
        self.assertIsNone(external_projection(("summary",)))
        self.assertIsNone(external_projection(("revise_memory",)))
        self.assertEqual("tool_arguments_schema_invalid", str(error))

    def test_deep_paths_have_a_fixed_output_ceiling(self):
        schema = {"type": "integer"}
        instance = "PRIVATE_VALUE"
        for _ in range(18):
            schema = {"type": "object", "properties": {"changes": schema}}
            instance = {"changes": instance}
        diagnostic = self.rejected(schema, instance).validation_diagnostic
        self.assertEqual(11, len(diagnostic["field_path"]))
        self.assertEqual("<truncated>", diagnostic["field_path"][-1])
        self.assertNotIn("PRIVATE", json.dumps(diagnostic))


if __name__ == "__main__":
    unittest.main()
