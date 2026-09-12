"""Public tool schemas must survive clients that copy only root field metadata."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from mcp_server import public_contract


PLANNING_TOOLS = (
    "remember_planning_memory",
    "recall_planning_memory",
    "record_planning_event",
    "revise_planning_memory",
    "review_planning_change",
)
CATALOG_TOOLS = PLANNING_TOOLS + (
    "remember_tool_guidance",
    "revise_tool_guidance",
    "preview_person_reference_rewrite",
    "confirm_person_reference_rewrite",
)
PLAN_REF = "plan://plan_" + "a" * 32 + "@1"
DEPENDENCY_REF = "plan://plan_" + "b" * 32 + "@2"


def client_root_projection(schema: dict[str, object]) -> dict[str, object]:
    """Reproduce the three-field schema seen on the affected client wire."""

    return {
        key: copy.deepcopy(schema[key])
        for key in ("type", "properties", "required")
        if key in schema
    }


def create_arguments() -> dict[str, object]:
    return {
        "write_context_ref": "artifact_isolated_test",
        "expected_planning_version": 0,
        "kind": "task",
        "track": "internal",
        "title": "Review a completed exercise",
        "original_text": "I choose to review the result of this exercise.",
        "summary": "Review the exercise result.",
        "reminder": "Review when relevant.",
        "importance": 70,
        "presence_mode": "relevant",
        "scene_tags": ["复盘", "exercise.review"],
        "keywords": ["exercise", "复盘"],
        "timezone": "Asia/Shanghai",
        "ai_adoption_statement": "I choose to adopt this plan.",
        "reason": "I want to review the result.",
        "calm_check": {
            "authorship_confirmed": True,
            "current_state_checked": True,
            "dependencies_checked": True,
            "consequences_reviewed": True,
            "rollback_understood": True,
            "notes": "I have checked the candidate and its consequences.",
        },
        "ai_confirmation": True,
        "idempotency_key": "isolated-planning-request-1",
    }


def revise_arguments() -> dict[str, object]:
    create = create_arguments()
    return {
        "write_context_ref": create["write_context_ref"],
        "expected_planning_version": 1,
        "plan_id": "plan_" + "c" * 32,
        "expected_plan_version": 1,
        "intent": "revise",
        "reason": create["reason"],
        "calm_check": create["calm_check"],
        "ai_confirmation": True,
        "idempotency_key": "isolated-planning-revision-1",
        "changes": {
            "scene_tags": ["复盘", "exercise.review"],
            "keywords": ["exercise"],
            "parent_ref": PLAN_REF,
            "dependency_refs": [DEPENDENCY_REF],
        },
    }


class PublicToolCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        project_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="stbrain-public-schema-") as temporary:
            root = Path(temporary)
            environment = {
                key: value for key, value in os.environ.items()
                if not key.startswith("STBRAIN_")
            }
            environment.update(
                {
                    "STBRAIN_MCP_TOKEN": "schema-test-placeholder-at-least-32-bytes",
                    "STBRAIN_REQUIRE_EXECUTION_BINDING": "0",
                    "STBRAIN_WAKE_SECRET": "schema-test-placeholder-secret-at-least-32-bytes",
                    "STBRAIN_OWNER_ID": "owner:isolated-schema-test",
                    "STBRAIN_MODEL_ID": "model:isolated-schema-test",
                    "STBRAIN_DB_PATH": str(root / "main.sqlite3"),
                    "STBRAIN_LEARNING_IDEA_DB_PATH": str(root / "ideas.sqlite3"),
                    "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(root / "vault.sqlite3"),
                }
            )
            script = (
                "import json; "
                "from mcp_server.server import mcp, planning_service; "
                f"names={CATALOG_TOOLS!r}; "
                "schemas={name:mcp._tool_manager.get_tool(name).parameters for name in names}; "
                "print(json.dumps({'schemas':schemas,'manual':planning_service.manual()}))"
            )
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        cls.schemas = payload["schemas"]
        cls.manual = payload["manual"]

    def assert_valid_in_both_views(self, name: str, arguments: dict[str, object]) -> None:
        for schema in (self.schemas[name], client_root_projection(self.schemas[name])):
            Draft202012Validator(schema).validate(arguments)

    def assert_invalid_in_both_views(self, name: str, arguments: dict[str, object]) -> None:
        for schema in (self.schemas[name], client_root_projection(self.schemas[name])):
            self.assertFalse(Draft202012Validator(schema).is_valid(arguments))

    def test_all_planning_schemas_are_self_contained(self) -> None:
        for name in PLANNING_TOOLS:
            with self.subTest(tool=name):
                schema = self.schemas[name]
                Draft202012Validator.check_schema(schema)
                Draft202012Validator.check_schema(client_root_projection(schema))
                self.assertNotIn('"$ref"', json.dumps(schema))
                self.assertNotIn("$defs", schema)
                self.assertFalse(schema["additionalProperties"])

    def test_create_survives_three_field_client_with_all_reference_fields(self) -> None:
        arguments = create_arguments()
        self.assert_valid_in_both_views("remember_planning_memory", arguments)
        arguments.update(parent_ref=PLAN_REF, dependency_refs=[DEPENDENCY_REF])
        self.assert_valid_in_both_views("remember_planning_memory", arguments)
        arguments.update(parent_ref=None, dependency_refs=[], scene_tags=[], keywords=[])
        self.assert_valid_in_both_views("remember_planning_memory", arguments)

    def test_create_keeps_list_and_reference_constraints(self) -> None:
        cases = [
            {"scene_tags": "exercise"},
            {"scene_tags": [""]},
            {"scene_tags": ["same", "same"]},
            {"scene_tags": [str(index) for index in range(17)]},
            {"scene_tags": ["x" * 161]},
            {"keywords": [""]},
            {"keywords": ["same", "same"]},
            {"keywords": [str(index) for index in range(17)]},
            {"keywords": ["x" * 161]},
            {"parent_ref": "plan://invalid@1"},
            {"parent_ref": PLAN_REF[:-1] + "0"},
            {"parent_ref": "plan_" + "a" * 32},
            {"dependency_refs": None},
            {"dependency_refs": ["plan://invalid@1"]},
            {"dependency_refs": [DEPENDENCY_REF, DEPENDENCY_REF]},
            {"dependency_refs": ["plan://plan_" + f"{index:032x}" + "@1" for index in range(9)]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                arguments = create_arguments()
                arguments.update(changes)
                self.assert_invalid_in_both_views("remember_planning_memory", arguments)

    def test_revise_survives_three_field_client_with_nested_references(self) -> None:
        arguments = revise_arguments()
        self.assert_valid_in_both_views("revise_planning_memory", arguments)
        for changes in ({"parent_ref": None}, {"dependency_refs": []}, {"title": "New title"}):
            arguments["changes"] = changes
            self.assert_valid_in_both_views("revise_planning_memory", arguments)
        arguments["intent"] = "abandon"
        arguments.pop("changes")
        self.assert_valid_in_both_views("revise_planning_memory", arguments)

    def test_revise_keeps_nested_reference_and_unknown_field_constraints(self) -> None:
        cases = [
            {},
            {"unknown_field": "not accepted"},
            {"scene_tags": ["same", "same"]},
            {"keywords": [""]},
            {"parent_ref": "plan://invalid@1"},
            {"dependency_refs": ["plan://invalid@1"]},
            {"dependency_refs": [DEPENDENCY_REF, DEPENDENCY_REF]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                arguments = revise_arguments()
                arguments["changes"] = changes
                self.assert_invalid_in_both_views("revise_planning_memory", arguments)

    def test_planning_existing_confirmation_and_idempotency_rules_are_visible(self) -> None:
        for name, arguments in (
            ("remember_planning_memory", create_arguments()),
            ("revise_planning_memory", revise_arguments()),
        ):
            properties = self.schemas[name]["properties"]
            self.assertEqual(200, properties["idempotency_key"]["maxLength"])
            if name == "remember_planning_memory":
                self.assertIs(True, properties["ai_confirmation"]["const"])
                self.assert_invalid_in_both_views(name, {**arguments, "ai_confirmation": None})
            else:
                self.assertEqual(
                    [{"type": "boolean", "const": True}, {"type": "null"}],
                    properties["ai_confirmation"]["anyOf"],
                )
                self.assert_valid_in_both_views(name, {**arguments, "ai_confirmation": None, "calm_check": None})
                omitted = {key: value for key, value in arguments.items()
                           if key not in {"ai_confirmation", "calm_check"}}
                self.assert_valid_in_both_views(name, omitted)
            for changes in ({"ai_confirmation": False}, {"idempotency_key": "x" * 201}):
                invalid = {**arguments, **changes}
                self.assert_invalid_in_both_views(name, invalid)
        rules = self.manual["creation_field_rules"]
        self.assertIn("internal", rules["reminder"])
        self.assertIn("task", rules["hierarchy"])
        self.assertIn("milestone", rules["hierarchy"])
        self.assertIn("My", rules["authorship"])
        self.assertIn("200", rules["idempotency_key"])
        self.assertIn("本次决定采用的最终计划", rules["confirmation"])
        self.assertIn("ai_confirmation=true", rules["confirmation"])
        self.assertIn("exact hash", rules["confirmation"])
        self.assertNotIn("较晚真实唤醒", rules["confirmation"])

    def test_schema_inlining_keeps_reference_sibling_constraints(self) -> None:
        original = {"$ref": "#/$defs/short_list", "maxItems": 1}
        flattened = public_contract._inline_planning_input_schema(original)
        validator = Draft202012Validator(flattened)
        self.assertTrue(validator.is_valid(["one"]))
        self.assertFalse(validator.is_valid(["one", "two"]))
        self.assertFalse(validator.is_valid([""]))
        self.assertEqual({"$ref": "#/$defs/short_list", "maxItems": 1}, original)

    def test_schema_inlining_rejects_unresolved_or_recursive_references(self) -> None:
        for reference in ("https://invalid.example/schema", "#/$defs/missing"):
            with self.subTest(reference=reference), self.assertRaises(RuntimeError):
                public_contract._inline_planning_input_schema({"$ref": reference})
        with patch.dict(public_contract._PLANNING_DEFS, {"cycle": {"$ref": "#/$defs/cycle"}}):
            with self.assertRaises(RuntimeError):
                public_contract._inline_planning_input_schema({"$ref": "#/$defs/cycle"})

    def test_tool_guidance_natural_tags_match_reviewed_static_constraints(self) -> None:
        module_schema = json.loads(
            (Path(__file__).parents[2] / "schemas" / "tool-guidance.schema.json").read_text(
                encoding="utf-8"
            )
        )
        tags = self.schemas["remember_tool_guidance"]["properties"]["scenario_tags"]
        array = next(option for option in tags["anyOf"] if option.get("type") == "array")
        self.assertEqual(module_schema["$defs"]["tagList"]["items"], array["items"])
        self.assertEqual(16, array["maxItems"])
        self.assertEqual(0, array["minItems"])
        self.assertTrue(array["uniqueItems"])
        self.assertIsNone(tags["default"])
        self.assertIn("中文", tags["description"])
        validator = Draft202012Validator(tags)
        self.assertTrue(validator.is_valid(None))
        self.assertTrue(validator.is_valid([]))
        self.assertTrue(validator.is_valid(["home.arrival", "room-light_1"]))
        self.assertTrue(validator.is_valid(["回家了", "准备睡觉", "home arrival", "-start"]))
        invalid_tags = (
            [""], ["   "], ["回家\n开灯"], ["home\x00arrival"],
            ["x" * 129], ["same", "same"],
            [f"scene.{index}" for index in range(17)],
        )
        for invalid in invalid_tags:
            with self.subTest(tags=invalid):
                self.assertFalse(validator.is_valid(invalid))
        for field in ("scenario_examples", "keywords"):
            human_text_schema = self.schemas["remember_tool_guidance"]["properties"][field]
            self.assertTrue(Draft202012Validator(human_text_schema).is_valid(["回家时打开灯光"]))
        revised = self.schemas["revise_tool_guidance"]["properties"]["scenario_tags"]
        self.assertIsNone(revised["default"])
        self.assertTrue(Draft202012Validator(revised).is_valid(None))
        self.assertTrue(Draft202012Validator(revised).is_valid(["home.arrival"]))
        self.assertTrue(Draft202012Validator(revised).is_valid(["回家"]))

    def test_tool_guidance_risk_descriptions_do_not_change_existing_choices(self) -> None:
        for name in ("remember_tool_guidance", "revise_tool_guidance"):
            properties = self.schemas[name]["properties"]
            self.assertIn("high", properties["risk_level"]["description"])
            self.assertIn("不是一律要求 critical", properties["risk_level"]["description"])
            self.assertIn("explicit_each_time", properties["confirmation_policy"]["description"])
            for risk in ("low", "medium", "high", "critical"):
                self.assertTrue(Draft202012Validator(properties["risk_level"]).is_valid(risk))
            for policy in ("none", "contextual", "explicit_each_time"):
                self.assertTrue(Draft202012Validator(properties["confirmation_policy"]).is_valid(policy))
            if name == "revise_tool_guidance":
                self.assertTrue(Draft202012Validator(properties["risk_level"]).is_valid(None))
                self.assertTrue(Draft202012Validator(properties["confirmation_policy"]).is_valid(None))

    def test_authoring_hash_describes_snapshot_not_live_draft_proof(self) -> None:
        schema = self.schemas["confirm_person_reference_rewrite"]
        source_hash = schema["properties"]["expected_source_draft_hash"]
        self.assertEqual("^[0-9a-f]{64}$", source_hash["pattern"])
        self.assertIn("不能证明", source_hash["description"])
        self.assertIn("重新 preview", source_hash["description"])
        self.assertNotIn("current_source_draft", schema["properties"])
        preview = self.schemas["preview_person_reference_rewrite"]
        self.assertIn("重新 preview", preview["properties"]["draft_version"]["description"])


if __name__ == "__main__":
    unittest.main()
