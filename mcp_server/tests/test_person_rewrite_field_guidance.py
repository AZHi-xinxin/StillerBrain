"""Public authoring field examples; synthetic data, no service/database import."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from mcp_server.authoring_service import AuthoringRewriteAccessService
from mcp_server.public_contract import (
    PUBLIC_REWRITE_CONFIRM_INPUT_SCHEMA, PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA,
)
from runtime.authoring import AUTHORING_REWRITE_MODULES, _validated_draft_fields, AuthoringError


class PersonRewriteFieldGuidanceTests(unittest.TestCase):
    def schemas(self):
        return [PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA["properties"]["draft_fields"],
                PUBLIC_REWRITE_CONFIRM_INPUT_SCHEMA["properties"]["final_fields"]]

    def test_both_maps_explain_slash_names_without_relaxing_the_contract(self):
        expected = {"/original_text", "/summary", "/title", "/current_understanding",
                    "/preceding_context_summary", "/display_label", "/completion_rule",
                    "/purpose", "/call_notes", "/documentation_note", "/salience_reason",
                    "/handoff_condition"}
        for schema in self.schemas():
            self.assertEqual(expected, set(schema["properties"]))
            self.assertIs(False, schema["additionalProperties"])
            self.assertEqual(1, schema["minProperties"])
            self.assertIn("带 /", schema["description"])
            self.assertIn("/current_understanding", schema["description"])
            Draft202012Validator.check_schema(schema)
            for example in schema["examples"]:
                Draft202012Validator(schema).validate(example)

    def test_bare_keys_unknown_keys_empty_maps_and_nonstrings_still_reject(self):
        for schema in self.schemas():
            validator = Draft202012Validator(schema)
            for value in ({"original_text": "synthetic"}, {"summary": "synthetic"},
                          {"private unknown": "synthetic"}, {}, {"/summary": 123}):
                with self.subTest(value=value):
                    self.assertFalse(validator.is_valid(value))
            self.assertTrue(validator.is_valid({"/summary": "synthetic"}))

    def test_manual_maps_each_ordinary_field_and_preserves_module_allowlists(self):
        synthetic = SimpleNamespace(advisory_status=lambda: {}, status=lambda: {})
        manual = AuthoringRewriteAccessService.manual(synthetic)
        fields = manual["field_format"]
        self.assertEqual("/original_text", fields["ordinary_writer_mapping"]["emotional_memory"]["content"])
        self.assertEqual("/current_understanding", fields["ordinary_writer_mapping"]["learning_memory"]["content"])
        self.assertIn("suggested_fields", fields["confirm"])
        for module, allowed in AUTHORING_REWRITE_MODULES.items():
            self.assertEqual(sorted(allowed), manual["modules"][module]["eligible_scalar_field_paths"])

    def test_runtime_still_requires_eligible_paths_and_returns_values_unchanged(self):
        allowed = AUTHORING_REWRITE_MODULES["emotional_memory_module_two"]
        value = {"/original_text": "synthetic original", "/summary": "synthetic summary"}
        self.assertEqual(value, _validated_draft_fields(value, allowed))
        with self.assertRaises(AuthoringError):
            _validated_draft_fields({"original_text": "synthetic"}, allowed)
        with self.assertRaises(AuthoringError):
            _validated_draft_fields({"/purpose": "synthetic"}, allowed)


if __name__ == "__main__":
    unittest.main()
