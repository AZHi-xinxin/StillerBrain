"""Isolated version-help regression; all participants and databases are synthetic."""
from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
import unittest

from mcp_server.authoring_service import AuthoringRewriteAccessService
from mcp_server.public_contract import (
    PUBLIC_REWRITE_CONFIRM_INPUT_SCHEMA, PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA,
)
from mcp_server.tests import test_person_rewrite_simple_entry as fixtures
from runtime.authoring import AUTHORING_SCHEMA_VERSIONS


class PersonRewriteSchemaVersionHelpTests(unittest.TestCase):
    setUp = fixtures.PersonRewriteSimpleEntryTests.setUp
    operation = fixtures.PersonRewriteSimpleEntryTests.operation
    rows = fixtures.PersonRewriteSimpleEntryTests.rows

    def snapshot(self):
        return {"status": self.authoring.status(),
                "previews": self.rows("authoring_rewrite_previews"),
                "receipts": self.rows("authoring_rewrite_receipts"),
                "emotions": self.rows("emotion_memories"),
                "learning": self.rows("learning_items")}

    def preview(self, fields):
        with self.operation("shared_person_authoring") as access:
            return self.authoring.preview(write_context_ref=access["write_context_ref"],
                expected_authoring_version=self.authoring.status()["row_version"], **fields)

    def confirm(self, fields):
        with self.operation("shared_person_authoring") as access:
            return self.authoring.confirm(write_context_ref=access["write_context_ref"],
                expected_authoring_version=self.authoring.status()["row_version"], **fields)

    def assert_help(self, result, module, private_version):
        self.assertEqual("reject", result["decision"])
        self.assertEqual(["module_schema_version_stale"], result["reason_codes"])
        self.assertFalse(result["state_changed"])
        help_value = result["schema_version_help"]
        self.assertEqual("module_schema_version", help_value["field"])
        self.assertEqual(module, help_value["target_module"])
        self.assertEqual(AUTHORING_SCHEMA_VERSIONS[module], help_value["expected_module_schema_version"])
        self.assertEqual("stbrain_open", help_value["manual_tool"])
        self.assertEqual({"view": "manual", "module": "shared_person_authoring"}, help_value["manual_arguments"])
        self.assertEqual(f"shared_person_authoring.modules.{module}.module_schema_version", help_value["manual_path"])
        self.assertIn("其他参数仍须按原流程检查", help_value["message"])
        self.assertIn("已有预览若按旧模块版本生成，应重新 preview", help_value["message"])
        self.assertNotIn(private_version, json.dumps(result, ensure_ascii=False))

    def test_preview_wrong_version_returns_current_value_without_state_changes(self):
        for module in AUTHORING_SCHEMA_VERSIONS:
            fields = fixtures._preview_fields("emotional_memory")
            fields["module"] = module
            fields["module_schema_version"] = "SYNTHETIC PRIVATE INPUT VERSION"
            before = self.snapshot()
            result = self.preview(fields)
            self.assert_help(result, module, fields["module_schema_version"])
            self.assertEqual(before, self.snapshot())
        # A version failure must not claim these later-checked emotion fields
        # would have been suitable for the tool/learning modules.

    def test_confirm_wrong_version_has_same_help_and_preserves_available_preview(self):
        for name in fixtures.MODULES:
            preview = self.preview(fixtures._preview_fields(name))
            fields = fixtures._confirmation_fields(name, preview)
            fields["module_schema_version"] = "SYNTHETIC PRIVATE CONFIRM VERSION"
            before = self.snapshot()
            result = self.confirm(fields)
            self.assert_help(result, fields["module"], fields["module_schema_version"])
            self.assertEqual(before, self.snapshot())
            fields["module_schema_version"] = AUTHORING_SCHEMA_VERSIONS[fields["module"]]
            confirmed = self.confirm(fields)
            self.assertEqual("confirmed", confirmed["decision"])

    def test_correct_version_empty_participants_uses_author_declaration_without_memory_write(self):
        fields = fixtures._preview_fields("emotional_memory")
        fields["authenticated_participant_entity_ids"] = []
        before = self.snapshot()
        result = self.preview(fields)
        self.assertEqual("preview_only", result["decision"])
        self.assertTrue(result["state_changed"])
        self.assertTrue(result["receipt_required"])
        self.assertNotIn("participant_not_authenticated", [item["reason_code"] for item in result["skipped"]])
        self.assertEqual("author_declared_literal_preview", result["source"])
        self.assertEqual(before["emotions"], self.rows("emotion_memories"))
        self.assertEqual(before["receipts"], self.rows("authoring_rewrite_receipts"))

    def test_correct_version_with_explicit_synthetic_participant_has_changes(self):
        fields = fixtures._preview_fields("emotional_memory")
        self.assertEqual(["synthetic-person"], fields["authenticated_participant_entity_ids"])
        result = self.preview(fields)
        self.assertEqual("preview_only", result["decision"])
        self.assertEqual("changes_available", result["rewrite_preview_status"])
        self.assertTrue(result["state_changed"])
        self.assertNotEqual(fields["draft_fields"], result["suggested_fields"])
        self.assertEqual([], self.rows("emotion_memories"))
        self.assertEqual([], self.rows("authoring_rewrite_receipts"))

    def test_other_rejection_shapes_unchanged_and_private_module_not_echoed(self):
        ordinary = AuthoringRewriteAccessService._reject("brain_open_required", status={"row_version": 0})
        with_module = AuthoringRewriteAccessService._reject("brain_open_required", status={"row_version": 0},
            target_module="emotional_memory_module_two")
        self.assertEqual(ordinary, with_module)
        private_module = "SYNTHETIC PRIVATE MODULE"
        result = AuthoringRewriteAccessService._reject("module_schema_version_stale",
            status={}, target_module=private_module)
        self.assertNotIn("schema_version_help", result)
        self.assertNotIn(private_module, json.dumps(result))

    def test_schema_version_is_optional_but_supplied_value_keeps_string_contract(self):
        for schema in (PUBLIC_REWRITE_PREVIEW_INPUT_SCHEMA, PUBLIC_REWRITE_CONFIRM_INPUT_SCHEMA):
            self.assertNotIn("module_schema_version", schema["required"])
            prop = copy.deepcopy(schema["properties"]["module_schema_version"])
            description = prop.pop("description")
            self.assertEqual({"type": "string", "minLength": 1}, prop)
            self.assertIn("contract_version", description)
            self.assertIn("stbrain_open(view='manual',module='shared_person_authoring')", description)
            self.assertIn("shared_person_authoring.modules[目标模块].module_schema_version", description)
            for module, version in AUTHORING_SCHEMA_VERSIONS.items():
                self.assertIn(f"{module}={version}", description)

    def test_manual_mapping_and_exact_facade_path_are_current(self):
        service = self.fixture.service
        service.authoring_rewrite = self.authoring
        result = service.open_brain(view="manual", module="shared_person_authoring")
        self.assertTrue(result["manual_available"])
        manual = result["shared_person_authoring"]
        self.assertEqual(AUTHORING_SCHEMA_VERSIONS, manual["version_help"]["current_module_schema_versions"])
        for module, version in AUTHORING_SCHEMA_VERSIONS.items():
            help_value = AuthoringRewriteAccessService._reject("module_schema_version_stale", status={},
                target_module=module)["schema_version_help"]
            value = result
            for part in help_value["manual_path"].split("."):
                value = value[part]
            self.assertEqual(version, value)

    def test_registered_entry_and_both_docstrings_name_real_manual_navigation(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8"))
        functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        entry = functions["stbrain_open"]
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "tool" for node in entry.decorator_list))
        for name in ("preview_person_reference_rewrite", "confirm_person_reference_rewrite"):
            doc = ast.get_docstring(functions[name])
            self.assertIn("stbrain_open(view='manual',module='shared_person_authoring')", doc)
            self.assertIn("contract_version", doc)


if __name__ == "__main__":
    unittest.main()
