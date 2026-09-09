from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema


EXPECTED_PUBLIC_TOOLS = {
    "stbrain_help",
    "remember_memory",
    "revise_memory",
    "advance_plan",
    "stbrain_health",
    "stbrain_open",
    "stbrain_open_direct",
    "submit_self_model_candidate",
    "activate_self_model_candidate",
    "query_self_model",
    "preview_person_reference_rewrite",
    "confirm_person_reference_rewrite",
    "remember_emotional_memory",
    "recall_emotional_memory",
    "revise_emotional_memory",
    "integrate_emotional_memories",
    "manage_brain_pin",
    "veto_ephemeral_memory",
    "remember_learning_memory",
    "remember_learning_contrast_pair",
    "recall_learning_memory",
    "revise_learning_memory",
    "integrate_learning_memories",
    "review_learning_change",
    "preview_learning_recall",
    "remember_tool_guidance",
    "recall_tool_guidance",
    "revise_tool_guidance",
    "review_tool_guidance_candidate",
    "record_tool_experience",
    "manage_self_governance_profile",
    "query_self_governance_profile",
    "manage_injection_control",
    "query_injection_control",
    "remember_planning_memory",
    "recall_planning_memory",
    "record_planning_event",
    "revise_planning_memory",
    "review_planning_change",
    "hold_hallucination_record",
    "open_hallucination_vault",
    "transfer_hallucination_record",
    "review_hallucination_restore",
}


class ToolCatalogTests(unittest.TestCase):
    @staticmethod
    def _public_tool_functions() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
        server_path = Path(__file__).resolve().parents[1] / "server.py"
        tree = ast.parse(server_path.read_text(encoding="utf-8"))
        exposed: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                function = decorator.func
                if isinstance(function, ast.Attribute) and function.attr == "tool":
                    exposed[node.name] = node
        return exposed

    def test_server_exposes_exact_public_facades(self) -> None:
        self.assertEqual(EXPECTED_PUBLIC_TOOLS, set(self._public_tool_functions()))

    def test_public_write_schemas_use_only_nonsecret_open_reference(self) -> None:
        functions = self._public_tool_functions()
        direct_arguments = {
            item.arg for item in functions["stbrain_open_direct"].args.args
        }
        self.assertEqual({"grant_ref"}, direct_arguments)
        remember_emotional_doc = ast.get_docstring(
            functions["remember_emotional_memory"]
        )
        self.assertIsNotNone(remember_emotional_doc)
        self.assertIn("Do not send recall_mode", remember_emotional_doc)
        for name in {"submit_self_model_candidate", "activate_self_model_candidate"}:
            arguments = {item.arg for item in functions[name].args.args}
            self.assertIn("write_context_ref", arguments)
            self.assertIn("expected_row_version", arguments)
            self.assertNotIn("wake_id", arguments)
            self.assertNotIn("wake_capability", arguments)
            self.assertNotIn("challenge_response", arguments)

        for name in {
            "remember_emotional_memory",
            "revise_emotional_memory",
            "integrate_emotional_memories",
            "manage_brain_pin",
            "veto_ephemeral_memory",
        }:
            arguments = {item.arg for item in functions[name].args.args}
            self.assertIn("write_context_ref", arguments)
            self.assertIn("expected_emotion_version", arguments)
            self.assertNotIn("wake_id", arguments)
            self.assertNotIn("wake_capability", arguments)
            self.assertNotIn("challenge_response", arguments)

        for name in {
            "remember_learning_memory",
            "remember_learning_contrast_pair",
            "revise_learning_memory",
            "integrate_learning_memories",
            "review_learning_change",
        }:
            arguments = {item.arg for item in functions[name].args.args}
            self.assertIn("write_context_ref", arguments)
            self.assertIn("expected_learning_version", arguments)

        for name in {
            "remember_tool_guidance",
            "revise_tool_guidance",
            "review_tool_guidance_candidate",
            "record_tool_experience",
        }:
            arguments = {item.arg for item in functions[name].args.args}
            self.assertIn("write_context_ref", arguments)
            self.assertIn("expected_tool_row_version", arguments)

        for name in {
            "remember_planning_memory",
            "record_planning_event",
            "revise_planning_memory",
            "review_planning_change",
        }:
            arguments = {item.arg for item in functions[name].args.args}
            self.assertIn("write_context_ref", arguments)
            self.assertIn("expected_planning_version", arguments)

        for name in {
            "hold_hallucination_record",
            "review_hallucination_restore",
        }:
            arguments = {item.arg for item in functions[name].args.args}
            self.assertIn("write_context_ref", arguments)
            self.assertIn("expected_vault_version", arguments)

        governance_arguments = {
            item.arg
            for item in functions["manage_self_governance_profile"].args.args
        }
        self.assertIn("write_context_ref", governance_arguments)
        self.assertIn("expected_profile_version", governance_arguments)
        self.assertNotIn("wake_id", governance_arguments)
        self.assertNotIn("wake_capability", governance_arguments)

        for name in {
            "preview_person_reference_rewrite",
            "confirm_person_reference_rewrite",
        }:
            arguments = {item.arg for item in functions[name].args.args}
            self.assertIn("write_context_ref", arguments)
            self.assertIn("expected_authoring_version", arguments)
            self.assertNotIn("wake_id", arguments)
            self.assertNotIn("wake_capability", arguments)

    def test_fastmcp_publishes_the_strict_v8_schemas(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.update(
                {
                    "STBRAIN_MCP_TOKEN": "catalog-test-token-that-is-at-least-32-bytes",
                    "STBRAIN_REQUIRE_EXECUTION_BINDING": "0",
                    "STBRAIN_WAKE_SECRET": "catalog-test-wake-secret-that-is-at-least-32-bytes",
                    "STBRAIN_MODEL_ID": "model:catalog-test",
                    "STBRAIN_OWNER_ID": "owner:catalog-test",
                    "STBRAIN_DB_PATH": str(Path(temporary) / "catalog.db"),
                }
            )
            script = (
                "import json; "
                "from mcp_server.server import mcp; "
                f"names={sorted(EXPECTED_PUBLIC_TOOLS)!r}; "
                "schemas={name:mcp._tool_manager.get_tool(name).parameters for name in names}; "
                "print(json.dumps(schemas, sort_keys=True))"
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        schemas = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(EXPECTED_PUBLIC_TOOLS, set(schemas))
        for schema in schemas.values():
            self.assertFalse(schema["additionalProperties"])

        compact_open = schemas["stbrain_open"]
        self.assertEqual({"view", "module", "page", "expected_material_hash"}, set(compact_open["properties"]))
        self.assertEqual("summary", compact_open["properties"]["view"]["default"])
        self.assertEqual(["summary", "manual", "review"], compact_open["properties"]["view"]["enum"])
        self.assertEqual(0, compact_open["properties"]["page"]["default"])
        self.assertEqual(0, compact_open["properties"]["page"]["minimum"])
        self.assertEqual("self_revision", compact_open["properties"]["module"]["default"])
        self.assertIn("planning_memory", compact_open["properties"]["module"]["enum"])
        self.assertFalse(compact_open.get("required"))

        direct = schemas["stbrain_open_direct"]
        self.assertEqual(["grant_ref"], direct["required"])
        self.assertEqual({"grant_ref"}, set(direct["properties"]))
        self.assertEqual(256, direct["properties"]["grant_ref"]["maxLength"])

        schema = schemas["submit_self_model_candidate"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(11, len(schema["allOf"][0]["oneOf"]))
        for intent in ("submit", "revise"):
            candidate = schema["$defs"][f"payload_{intent}"]
            self.assertFalse(candidate["additionalProperties"])
            self.assertEqual({"content", "reason"}, set(candidate["properties"]))
        serialized = json.dumps(schema, sort_keys=True)
        self.assertNotIn("wake_capability", serialized)
        self.assertNotIn("challenge_response", serialized)
        self.assertNotIn("_server_derive_candidate_metadata", serialized)

        activation = schemas["activate_self_model_candidate"]
        self.assertEqual(
            {
                "candidate_id",
                "write_context_ref",
                "expected_row_version",
                "expected_active_revision",
                "ai_confirmation",
            },
            set(activation["required"]),
        )
        self.assertIs(True, activation["properties"]["ai_confirmation"]["const"])
        self.assertEqual(
            [{"type": "string", "minLength": 1}, {"type": "null"}],
            activation["properties"]["expected_active_revision"]["anyOf"],
        )

        revise_calm = schemas["revise_learning_memory"]["properties"]["calm_check"]
        integrate_calm = schemas["integrate_learning_memories"]["properties"]["calm_check"]
        review_calm = schemas["review_learning_change"]["properties"]["calm_check"]
        self.assertEqual(revise_calm, integrate_calm)
        self.assertEqual(integrate_calm, review_calm)
        self.assertFalse(integrate_calm["additionalProperties"])
        self.assertEqual(
            {
                "evidence_sufficient",
                "counterevidence_checked",
                "scope_changed",
                "affected_links_checked",
                "single_turn_pressure_absent",
                "rollback_understood",
                "notes",
                "evidence_refs",
            },
            set(integrate_calm["required"]),
        )
        self.assertEqual(set(integrate_calm["required"]), set(integrate_calm["properties"]))
        for name in {
            "evidence_sufficient",
            "counterevidence_checked",
            "affected_links_checked",
            "single_turn_pressure_absent",
            "rollback_understood",
        }:
            self.assertEqual("boolean", integrate_calm["properties"][name]["type"])
            self.assertIs(True, integrate_calm["properties"][name]["const"])
        self.assertEqual("boolean", integrate_calm["properties"]["scope_changed"]["type"])
        self.assertNotIn("const", integrate_calm["properties"]["scope_changed"])
        self.assertEqual("string", integrate_calm["properties"]["notes"]["type"])
        self.assertEqual(1000, integrate_calm["properties"]["notes"]["maxLength"])
        evidence_refs = integrate_calm["properties"]["evidence_refs"]
        self.assertEqual("array", evidence_refs["type"])
        self.assertEqual(1, evidence_refs["minItems"])
        self.assertEqual(16, evidence_refs["maxItems"])
        self.assertEqual("string", evidence_refs["items"]["type"])
        self.assertEqual(300, evidence_refs["items"]["maxLength"])
        calm_example = {
            "evidence_sufficient": True,
            "counterevidence_checked": True,
            "scope_changed": False,
            "affected_links_checked": True,
            "single_turn_pressure_absent": True,
            "rollback_understood": True,
            "notes": "已逐项检查证据、反证、关系与回滚边界。",
            "evidence_refs": ["learning://example@1"],
        }
        jsonschema.Draft202012Validator.check_schema(integrate_calm)
        jsonschema.validate(calm_example, integrate_calm)
        jsonschema.validate(
            {**calm_example, "scope_changed": True, "notes": "范围已由旧版扩大。"},
            integrate_calm,
        )
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                {**calm_example, "scope_changed": True},
                integrate_calm,
            )

        query = schemas["query_self_model"]
        self.assertEqual("integer", query["properties"]["limit"]["type"])
        self.assertEqual(1, query["properties"]["limit"]["minimum"])
        self.assertEqual(100, query["properties"]["limit"]["maximum"])
        self.assertEqual(20, query["properties"]["limit"]["default"])
        self.assertEqual("boolean", query["properties"]["include_content"]["type"])
        self.assertEqual(
            "boolean",
            query["properties"]["include_anchor_references"]["type"],
        )

        remember = schemas["remember_emotional_memory"]
        self.assertEqual(
            {
                "write_context_ref",
                "expected_emotion_version",
                "memory_type",
                "original_text",
                "summary",
                "primary_emotion",
                "reason",
            },
            set(remember["required"]),
        )
        self.assertNotIn("payload", remember["properties"])
        self.assertNotIn("content", remember["properties"])
        self.assertEqual(
            {"shared_event", "feeling", "relationship", "meaningful_dialogue", "emotional_reflection"},
            set(remember["properties"]["memory_type"]["enum"]),
        )
        self.assertEqual(1, remember["properties"]["secondary_emotions"]["anyOf"][0]["maxItems"])
        self.assertNotIn("pattern", remember["properties"]["original_text"])
        self.assertIn("第一、第二或第三人称", remember["properties"]["original_text"]["description"])
        self.assertIn("referent_bindings", remember["properties"])
        self.assertIn("不会自动注入", remember["properties"]["reason"]["description"])
        self.assertNotIn("pattern", remember["properties"]["reason"])
        self.assertIn("firsthand", remember["properties"]["origin"]["description"])
        self.assertIn("OB/旧档案", remember["properties"]["origin"]["description"])

        revise = schemas["revise_emotional_memory"]
        self.assertNotIn("original_text", revise["properties"])
        self.assertNotIn("payload", revise["properties"])
        self.assertEqual(1, revise["properties"]["expected_memory_version"]["minimum"])
        self.assertEqual(1, revise["properties"]["secondary_emotions"]["anyOf"][0]["maxItems"])

        integrate = schemas["integrate_emotional_memories"]
        self.assertIn("source_memory_ids", integrate["required"])
        self.assertNotIn("payload", integrate["properties"])
        self.assertEqual(1, integrate["properties"]["secondary_emotions"]["anyOf"][0]["maxItems"])

        recall_emotional = schemas["recall_emotional_memory"]
        self.assertEqual(1, recall_emotional["properties"]["limit"]["minimum"])
        self.assertEqual(50, recall_emotional["properties"]["limit"]["maximum"])
        self.assertEqual(10, recall_emotional["properties"]["limit"]["default"])
        self.assertFalse(recall_emotional["properties"]["explicit_request"]["default"])
        self.assertFalse(recall_emotional["properties"]["safety_emergency"]["default"])

        pin = schemas["manage_brain_pin"]
        self.assertEqual(
            {"request", "confirm", "lower", "remove"},
            set(pin["properties"]["action"]["enum"]),
        )
        self.assertEqual("boolean", pin["properties"]["ai_confirmation"]["type"])

        learning = schemas["remember_learning_memory"]
        self.assertIn("scene_tags", learning["properties"])
        self.assertIn("referent_bindings", learning["properties"])
        self.assertNotIn("payload", learning["properties"])
        self.assertNotIn("epistemic_status", learning["properties"])
        self.assertIn("source_basis", learning["properties"])
        self.assertIn("claim_review_status", learning["properties"])
        self.assertIn(
            "只描述来源",
            learning["properties"]["source_basis"]["description"],
        )
        ordinary_link_items = learning["properties"]["links"]["anyOf"][0]["items"]
        self.assertNotIn(
            "contrast", ordinary_link_items["properties"]["relation_type"]["enum"]
        )
        learning_revision = schemas["revise_learning_memory"]
        link_items = learning_revision["properties"]["links"]["anyOf"][0]["items"]
        contrast_link = link_items["oneOf"][0]
        self.assertEqual("contrast", contrast_link["properties"]["relation_type"]["const"])
        self.assertFalse(contrast_link["additionalProperties"])
        self.assertEqual(
            {
                "subject_key",
                "scope_signature",
                "time_condition",
                "predicate_signature",
                "mutual_exclusivity_basis",
            },
            set(contrast_link["properties"]["basis"]["required"]),
        )
        self.assertNotIn("edge_type", contrast_link["properties"])

        learning_recall = schemas["recall_learning_memory"]
        self.assertEqual(
            {"search", "inventory"},
            set(learning_recall["properties"]["view"]["enum"]),
        )
        self.assertEqual("search", learning_recall["properties"]["view"]["default"])
        self.assertIn(
            "都有什么",
            learning_recall["properties"]["view"]["description"],
        )
        self.assertIn(
            "语义搜索",
            learning_recall["properties"]["query"]["description"],
        )
        self.assertIn(
            "全部活动卡",
            learning_recall["properties"]["query"]["description"],
        )
        self.assertEqual(0, learning_recall["properties"]["offset"]["minimum"])
        self.assertEqual(100000, learning_recall["properties"]["offset"]["maximum"])
        self.assertIn(
            "versioned item_ref",
            learning_recall["properties"]["target_ref"]["anyOf"][0]["description"],
        )

        pair = schemas["remember_learning_contrast_pair"]
        self.assertFalse(pair["additionalProperties"])
        self.assertEqual(2, pair["properties"]["scene_tags"]["minItems"])
        self.assertEqual(1, pair["properties"]["application_contexts"]["minItems"])
        for side in ("first_claim", "second_claim"):
            claim = pair["properties"][side]
            self.assertFalse(claim["additionalProperties"])
            self.assertEqual(60, claim["properties"]["confidence"]["maximum"])
            self.assertEqual(1, claim["properties"]["uncertainties"]["minItems"])
            self.assertNotIn("epistemic_status", claim["properties"])
            self.assertNotIn("claim_review_status", claim["properties"])
            self.assertNotIn("lifecycle", claim["properties"])
            self.assertNotIn("links", claim["properties"])

        tool = schemas["remember_tool_guidance"]
        self.assertIn("scenario_tags", tool["required"])
        self.assertIn("completion_rule", tool["required"])
        self.assertNotIn("arguments", tool["properties"])
        self.assertNotIn("raw_result", tool["properties"])
        self.assertIn("referent_bindings", tool["properties"])
        for name in {
            "remember_emotional_memory",
            "remember_learning_memory",
            "remember_tool_guidance",
        }:
            self.assertIn("rewrite_receipt", schemas[name]["properties"])

        rewrite_preview = schemas["preview_person_reference_rewrite"]
        self.assertFalse(rewrite_preview["additionalProperties"])
        self.assertFalse(
            rewrite_preview["properties"]["draft_fields"]["additionalProperties"]
        )
        self.assertFalse(
            rewrite_preview["properties"]["rewrite_targets"]["items"]["additionalProperties"]
        )
        rewrite_confirm = schemas["confirm_person_reference_rewrite"]
        self.assertIs(True, rewrite_confirm["properties"]["ai_confirmation"]["const"])
        self.assertFalse(
            rewrite_confirm["properties"]["final_fields"]["additionalProperties"]
        )

        governance = schemas["manage_self_governance_profile"]
        self.assertEqual(
            {
                "action",
                "scope",
                "write_context_ref",
                "expected_profile_version",
            },
            set(governance["required"]),
        )
        self.assertEqual(
            {
                "propose_set",
                "propose_clear",
                "propose_rollback",
                "withdraw",
                "activate",
            },
            set(governance["properties"]["action"]["enum"]),
        )
        self.assertNotIn("content", governance["properties"])
        self.assertNotIn("payload", governance["properties"])
        self.assertIn("text", governance["properties"])
        self.assertIn("trigger_mode", governance["properties"])
        self.assertIn("scene_tags", governance["properties"])
        expected_active_revision_schema = governance["properties"][
            "expected_active_revision"
        ]
        expected_active_revision_text = json.dumps(
            expected_active_revision_schema, ensure_ascii=False
        ).casefold()
        self.assertIn("json null", expected_active_revision_text)
        self.assertIn("false", expected_active_revision_text)
        reason_text = json.dumps(
            governance["properties"]["reason"], ensure_ascii=False
        ).casefold()
        self.assertIn("activate", reason_text)
        self.assertIn("可选", reason_text)

        governance_query = schemas["query_self_governance_profile"]
        self.assertEqual(
            {"status", "manual", "revisions"},
            set(governance_query["properties"]["view"]["enum"]),
        )

        planning_calm = schemas["remember_planning_memory"]["properties"]["calm_check"]
        for name in ("remember_planning_memory", "revise_planning_memory"):
            self.assertNotIn("$defs", schemas[name])
            self.assertNotIn('"$ref"', json.dumps(schemas[name]))
        self.assertFalse(planning_calm["additionalProperties"])
        self.assertEqual(
            {
                "authorship_confirmed", "current_state_checked",
                "dependencies_checked", "consequences_reviewed",
                "rollback_understood", "notes",
            },
            set(planning_calm["required"]),
        )
        self.assertEqual(
            planning_calm,
            schemas["review_planning_change"]["properties"]["calm_check"],
        )
        self.assertFalse(
            schemas["revise_planning_memory"]["properties"]["changes"]["anyOf"][0][
                "additionalProperties"
            ]
        )
        evidence_schema = schemas["record_planning_event"]["properties"]["evidence"]
        self.assertEqual(16, evidence_schema["maxItems"])
        self.assertFalse(evidence_schema["items"]["additionalProperties"])

        vault_hold = schemas["hold_hallucination_record"]
        self.assertFalse(vault_hold["additionalProperties"])
        self.assertEqual(20000, vault_hold["properties"]["isolated_content"]["anyOf"][0]["maxLength"])
        self.assertEqual(2, len(vault_hold["allOf"]))
        vault_transfer = schemas["transfer_hallucination_record"]
        self.assertEqual(3, len(vault_transfer["allOf"]))
        self.assertEqual(
            {"preview", "commit", "propose_restore"},
            set(vault_transfer["properties"]["intent"]["enum"]),
        )

    def test_fastmcp_call_tool_rejects_extra_arguments_and_non_json_true(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.update(
                {
                    "STBRAIN_MCP_TOKEN": "call-test-token-that-is-at-least-32-bytes",
                    "STBRAIN_REQUIRE_EXECUTION_BINDING": "0",
                    "STBRAIN_WAKE_SECRET": "call-test-wake-secret-that-is-at-least-32-bytes",
                    "STBRAIN_MODEL_ID": "model:call-test",
                    "STBRAIN_OWNER_ID": "owner:call-test",
                    "STBRAIN_DB_PATH": str(Path(temporary) / "call.db"),
                }
            )
            script = r'''
import asyncio
import json

from mcp_server.server import mcp


async def rejected(name, arguments):
    try:
        await mcp.call_tool(name, arguments)
    except Exception as exc:
        return {"rejected": True, "error_type": type(exc).__name__, "message": str(exc)}
    return {"rejected": False, "error_type": None, "message": ""}


async def main():
    sentinel = "sensitive-sentinel-must-not-echo"
    valid = {
        "stbrain_health": {},
        "stbrain_open": {},
        "submit_self_model_candidate": {
            "intent": "acknowledge",
            "write_context_ref": "ref",
            "expected_row_version": 0,
        },
        "activate_self_model_candidate": {
            "candidate_id": "cand",
            "write_context_ref": "ref",
            "expected_row_version": 0,
            "expected_active_revision": None,
            "ai_confirmation": True,
        },
        "query_self_model": {},
        "preview_person_reference_rewrite": {
            "write_context_ref": "ref", "expected_authoring_version": 0,
            "module": "emotional_memory_module_two", "draft_version": 0,
            "draft_fields": {"/original_text": "她回来了。", "/summary": "她回来了。"},
            "referent_bindings": [{"field_path": "/original_text", "surface_form": "她", "occurrence_index": 0, "entity_ref": "person:x", "resolution_status": "resolved", "confidence": 100}],
            "rewrite_targets": [{"field_path": "/original_text", "surface_form": "她", "occurrence_index": 0, "entity_ref": "person:x", "target_surface_form": "小乙", "mention_kind": "pronoun", "target_alias_ref": "alias://x@1", "target_alias_version": 1, "unique_in_scope": True}],
            "conversation_mode": "one_to_one", "authenticated_participant_entity_ids": ["person:x"],
            "alias_collision_scope": "conversation:test", "alias_collision_scope_version": 1,
            "protected_spans": [], "module_schema_version": "emotional-memory/0.1.1",
        },
        "confirm_person_reference_rewrite": {
            "write_context_ref": "ref", "expected_authoring_version": 0,
            "module": "emotional_memory_module_two", "preview_id": "rwprev_00000000000000000000000000000000",
            "expected_source_draft_hash": "0" * 64, "expected_suggestion_hash": "1" * 64,
            "expected_validation_context_hash": "2" * 64,
            "final_fields": {"/original_text": "小乙回来了。", "/summary": "她回来了。"},
            "final_fields_hash": "3" * 64, "conversation_mode": "one_to_one",
            "authenticated_participant_entity_ids": ["person:x"],
            "alias_collision_scope": "conversation:test", "alias_collision_scope_version": 1,
            "protected_spans": [], "module_schema_version": "emotional-memory/0.1.1",
            "ai_confirmation": True,
        },
        "remember_emotional_memory": {
            "write_context_ref": "ref",
            "expected_emotion_version": 0,
            "memory_type": "shared_event",
            "original_text": "我记得这次测试。",
            "summary": "我记得测试。",
            "primary_emotion": "calm",
            "reason": "我认为值得保存。",
        },
        "recall_emotional_memory": {"query": "测试"},
        "revise_emotional_memory": {
            "write_context_ref": "ref",
            "expected_emotion_version": 0,
            "memory_id": "emmem",
            "expected_memory_version": 1,
            "reason": "我希望修订。",
            "summary": "我记得这次测试。",
        },
        "integrate_emotional_memories": {
            "write_context_ref": "ref",
            "expected_emotion_version": 0,
            "source_memory_ids": ["one", "two"],
            "original_text": "我把两段经历整理在一起。",
            "summary": "我整合了两段经历。",
            "primary_emotion": "calm",
            "reason": "我认为它们属于同一条时间线。",
        },
        "manage_brain_pin": {
            "write_context_ref": "ref",
            "expected_emotion_version": 0,
            "action": "request",
            "reason": "我希望申请这条锚点。",
            "pin_kind": "identity_anchor",
            "display_text": "我重视诚实。",
            "source_ref": "memory://anchor",
        },
        "veto_ephemeral_memory": {
            "write_context_ref": "ref",
            "expected_emotion_version": 0,
            "reason": "我不希望保留这段短期内容。",
            "thread_id": "thread",
        },
        "remember_learning_memory": {
            "write_context_ref": "ref", "expected_learning_version": 0,
            "kind": "fact", "title": "测试知识", "summary": "测试概要",
            "current_understanding": "测试理解", "source_basis": "reported",
            "confidence": 60, "correctness_assessment": "我检查了边界。", "reason": "测试。",
        },
        "remember_learning_contrast_pair": {
            "write_context_ref": "ref", "expected_learning_version": 0,
            "kind": "concept",
            "first_claim": {
                "title": "说法甲", "summary": "甲概要", "current_understanding": "甲理解",
                "source_basis": "reported", "confidence": 30, "uncertainties": ["未核验"],
            },
            "second_claim": {
                "title": "说法乙", "summary": "乙概要", "current_understanding": "乙理解",
                "source_basis": "reported", "confidence": 30, "uncertainties": ["未核验"],
            },
            "contrast_basis": {
                "subject_key": "同一主体", "scope_signature": "同一范围",
                "time_condition": "timeless", "predicate_signature": "同一谓词",
                "mutual_exclusivity_basis": "两个结论不能同时成立",
            },
            "application_contexts": ["比较两种说法"],
            "scene_tags": ["相反知识", "同一主体"],
            "correctness_assessment": "我保留两种尚未核验的说法。", "reason": "测试。",
        },
        "recall_learning_memory": {"query": "测试"},
        "revise_learning_memory": {
            "write_context_ref": "ref", "expected_learning_version": 0,
            "target_ref": "learning://one@1", "expected_target_version": 1,
            "action": "change", "change_class": "typo",
            "classification_basis": ["只是错字"], "correctness_assessment": "我检查了修改。",
            "diff": "修正错字", "reason": "测试。",
        },
        "integrate_learning_memories": {
            "write_context_ref": "ref", "expected_learning_version": 0,
            "source_learning_ids": ["one", "two"], "synthesis_kind": "summary",
            "classification_basis": ["同类知识"], "correctness_assessment": "我检查了来源。",
            "diff": "形成综合", "calm_check": {}, "reason": "测试。",
            "kind": "fact", "title": "综合", "summary": "综合概要",
            "current_understanding": "综合理解", "source_basis": "reported", "confidence": 60,
        },
        "review_learning_change": {
            "write_context_ref": "ref", "expected_learning_version": 0,
            "candidate_id": "candidate", "expected_candidate_version": 1,
            "expected_candidate_hash": "hash", "expected_base_version": 1,
            "action": "accept", "correctness_assessment": "我检查了候选。",
            "calm_check": {}, "reason": "测试。", "ai_confirmation": True,
        },
        "preview_learning_recall": {"situation": "测试场景"},
        "remember_tool_guidance": {
            "write_context_ref": "ref", "expected_tool_row_version": 0,
            "tool_name": "example_tool", "operation_key": "query", "display_label": "示例工具",
            "capability_class": "information_query", "risk_level": "low", "confirmation_policy": "none",
            "completion_rule": "收到明确成功结果", "critical_preconditions": [], "purpose": "查询示例",
            "use_when": ["需要示例时"], "avoid_when": ["无需查询时"], "scenario_tags": ["example"],
            "scenario_examples": ["查询示例"], "call_notes": "按公开 Schema 调用",
            "keywords": ["示例"], "aliases": [], "reason": "测试。",
        },
        "recall_tool_guidance": {"query": "示例"},
        "revise_tool_guidance": {
            "write_context_ref": "ref", "expected_tool_row_version": 0,
            "card_id": "card", "expected_card_version": 1, "intent": "revise",
            "edit_class": "metadata", "reason": "测试。", "display_label": "示例工具",
        },
        "review_tool_guidance_candidate": {
            "write_context_ref": "ref", "expected_tool_row_version": 0,
            "candidate_id": "candidate", "candidate_hash": "hash", "decision": "accept",
            "correctness_decision": "correct", "correctness_assessment": "我已完整检查候选内容与边界。",
            "reason": "测试。", "ai_confirmation": True, "expected_base_version": 1,
        },
        "record_tool_experience": {
            "write_context_ref": "ref", "expected_tool_row_version": 0,
            "card_id": "card", "outcome": "success", "reason_code": "ok",
            "attempt_summary": "测试调用成功。",
        },
        "manage_self_governance_profile": {
            "action": "propose_set", "scope": "tool_use",
            "write_context_ref": "ref", "expected_profile_version": 0,
            "reason": "我选择建立这段边界。", "text": "我会按当前场景复核工具调用。",
            "trigger_mode": "scene_relevant", "scene_tags": ["工具调用"],
            "expected_active_revision": None,
        },
        "query_self_governance_profile": {},
        "manage_injection_control": {
            "action": "emergency_off", "scope": "global",
            "write_context_ref": "ref", "expected_control_version": 0,
            "ai_confirmation": True, "reason": "我选择停止自动注入。",
        },
        "query_injection_control": {},
        "remember_planning_memory": {
            "write_context_ref": "ref", "expected_planning_version": 0,
            "kind": "task", "track": "internal", "title": "完成测试",
            "original_text": "我选择完成这次测试。", "summary": "完成测试",
            "reminder": "继续测试", "importance": 70, "presence_mode": "relevant",
            "scene_tags": ["测试"], "keywords": ["测试"], "timezone": "Asia/Shanghai",
            "ai_adoption_statement": "我愿意采纳并维护这个计划。",
            "reason": "这是我自己决定采用的计划。",
            "calm_check": {
                "authorship_confirmed": True, "current_state_checked": True,
                "dependencies_checked": True, "consequences_reviewed": True,
                "rollback_understood": True, "notes": "我已逐项检查。",
            },
            "ai_confirmation": True, "idempotency_key": "plan-test-1",
        },
        "recall_planning_memory": {"query": "测试"},
        "record_planning_event": {
            "write_context_ref": "ref", "expected_planning_version": 0,
            "plan_id": "plan_00000000000000000000000000000000",
            "expected_plan_version": 1, "event_type": "progress",
            "reason": "我完成了一步。",
            "evidence": [{
                "source_kind": "current_conversation", "source_ref": "wake://test",
                "evidence_summary": "本轮已完成一步", "provenance": "verified",
            }],
            "ai_confirmation": True, "idempotency_key": "plan-event-1",
        },
        "revise_planning_memory": {
            "write_context_ref": "ref", "expected_planning_version": 0,
            "plan_id": "plan_00000000000000000000000000000000",
            "expected_plan_version": 1, "intent": "abandon",
            "reason": "我重新评估后决定放弃。",
            "calm_check": {
                "authorship_confirmed": True, "current_state_checked": True,
                "dependencies_checked": True, "consequences_reviewed": True,
                "rollback_understood": True, "notes": "我已逐项检查。",
            },
            "ai_confirmation": True, "idempotency_key": "plan-revise-1",
        },
        "review_planning_change": {
            "write_context_ref": "ref", "expected_planning_version": 0,
            "candidate_id": "plancand_00000000000000000000000000000000",
            "expected_candidate_version": 1, "expected_candidate_hash": "0" * 64,
            "expected_base_version": 0, "decision": "accept",
            "correctness_assessment": "我已检查全部内容与来源。",
            "calm_check": {
                "authorship_confirmed": True, "current_state_checked": True,
                "dependencies_checked": True, "consequences_reviewed": True,
                "rollback_understood": True, "notes": "我已逐项检查。",
            },
            "reason": "我在新唤醒中独立接受。", "ai_confirmation": True,
        },
        "hold_hallucination_record": {
            "write_context_ref": "ref", "expected_vault_version": 0,
            "intent": "record", "reason": "我选择隔离而不裁定真假。",
            "ai_confirmation": True, "neutral_title": "待现实复核的说法",
            "isolated_content": "原始说法", "current_account": "当前较可靠说明",
            "basis": "我目前掌握的依据", "warning_text": "我先区分记录与事实。",
            "warning_suffix": "我读完后仍会独立判断。",
        },
        "open_hallucination_vault": {},
        "transfer_hallucination_record": {
            "write_context_ref": "ref", "intent": "preview",
            "source_ref": "learning://one@1", "expected_source_row_version": 0,
        },
        "review_hallucination_restore": {
            "write_context_ref": "ref", "expected_vault_version": 1,
            "candidate_id": "hvrestore_00000000000000000000000000000000",
            "expected_candidate_version": 1, "expected_candidate_hash": "0" * 64,
            "expected_base_record_version": 1, "action": "reject",
            "reason": "我决定继续隔离。", "ai_confirmation": True,
        },
    }
    extra_results = {}
    for name, arguments in valid.items():
        attempt = dict(arguments)
        attempt["unexpected_top_level"] = sentinel
        extra_results[name] = await rejected(name, attempt)

    confirmation_results = {}
    for value in (1, "true", "yes", False):
        arguments = dict(valid["activate_self_model_candidate"])
        arguments["ai_confirmation"] = value
        confirmation_results[repr(value)] = await rejected(
            "activate_self_model_candidate", arguments
        )

    missing_base_arguments = dict(valid["activate_self_model_candidate"])
    del missing_base_arguments["expected_active_revision"]
    missing_base_result = await rejected(
        "activate_self_model_candidate", missing_base_arguments
    )

    governance_false_arguments = dict(valid["manage_self_governance_profile"])
    governance_false_arguments["expected_active_revision"] = False
    governance_false_result = await rejected(
        "manage_self_governance_profile", governance_false_arguments
    )

    _, governance_manage = await mcp.call_tool(
        "manage_self_governance_profile",
        valid["manage_self_governance_profile"],
    )
    _, governance_query = await mcp.call_tool(
        "query_self_governance_profile",
        valid["query_self_governance_profile"],
    )

    print(json.dumps({
        "sentinel": sentinel,
        "extra_results": extra_results,
        "confirmation_results": confirmation_results,
        "missing_base_result": missing_base_result,
        "governance_false_result": governance_false_result,
        "governance_manage": governance_manage,
        "governance_query": governance_query,
    }, sort_keys=True))


asyncio.run(main())
'''
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        for rejection in result["extra_results"].values():
            self.assertTrue(rejection["rejected"], rejection)
            self.assertEqual("ToolError", rejection["error_type"])
            self.assertNotIn(result["sentinel"], rejection["message"])
        self.assertEqual({"1", "'true'", "'yes'", "False"}, set(result["confirmation_results"]))
        for rejection in result["confirmation_results"].values():
            self.assertTrue(rejection["rejected"], rejection)
            self.assertEqual("ToolError", rejection["error_type"])
        self.assertTrue(result["missing_base_result"]["rejected"])
        self.assertEqual("ToolError", result["missing_base_result"]["error_type"])
        self.assertTrue(result["governance_false_result"]["rejected"])
        self.assertEqual("ToolError", result["governance_false_result"]["error_type"])
        self.assertEqual("rejected", result["governance_manage"]["decision"])
        self.assertEqual(
            "self_governance_profile", result["governance_query"]["module"]
        )

    def test_fastmcp_runtime_rejects_schema_coercions_without_side_effect_or_echo(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            environment.update(
                {
                    "STBRAIN_MCP_TOKEN": "strict-test-token-that-is-at-least-32-bytes",
                    "STBRAIN_REQUIRE_EXECUTION_BINDING": "0",
                    "STBRAIN_WAKE_SECRET": "strict-test-wake-secret-that-is-at-least-32-bytes",
                    "STBRAIN_MODEL_ID": "model:strict-test",
                    "STBRAIN_OWNER_ID": "owner:strict-test",
                    "STBRAIN_DB_PATH": str(Path(temporary) / "strict.db"),
                }
            )
            script = r'''
import asyncio
import json

from mcp_server.server import mcp, service


async def rejected(name, arguments):
    try:
        await mcp.call_tool(name, arguments)
    except Exception as exc:
        return {"rejected": True, "error_type": type(exc).__name__, "message": str(exc)}
    return {"rejected": False, "error_type": None, "message": ""}


async def main():
    sentinel = "private-input-value-must-not-echo-7e91"
    submit = {
        "intent": "acknowledge",
        "write_context_ref": "ref",
        "expected_row_version": 0,
    }
    activate = {
        "candidate_id": "cand",
        "write_context_ref": "ref",
        "expected_row_version": 0,
        "expected_active_revision": None,
        "ai_confirmation": True,
    }
    cases = {
        "health_extra": ("stbrain_health", {"unexpected": sentinel}),
        "open_extra": ("stbrain_open", {"unexpected": sentinel}),
        "submit_extra": (
            "submit_self_model_candidate",
            {**submit, "unexpected": sentinel},
        ),
        "submit_row_bool": (
            "submit_self_model_candidate",
            {**submit, "expected_row_version": False},
        ),
        "submit_row_string": (
            "submit_self_model_candidate",
            {**submit, "expected_row_version": "0"},
        ),
        "submit_row_float": (
            "submit_self_model_candidate",
            {**submit, "expected_row_version": 0.0},
        ),
        "submit_row_negative": (
            "submit_self_model_candidate",
            {**submit, "expected_row_version": -1},
        ),
        "submit_ref_empty": (
            "submit_self_model_candidate",
            {**submit, "write_context_ref": ""},
        ),
        "submit_ref_non_string": (
            "submit_self_model_candidate",
            {**submit, "write_context_ref": 7},
        ),
        "submit_intent_non_string": (
            "submit_self_model_candidate",
            {**submit, "intent": 7},
        ),
        "submit_payload_non_object": (
            "submit_self_model_candidate",
            {**submit, "payload": []},
        ),
        "activate_row_string": (
            "activate_self_model_candidate",
            {**activate, "expected_row_version": "0"},
        ),
        "activate_row_bool": (
            "activate_self_model_candidate",
            {**activate, "expected_row_version": False},
        ),
        "activate_row_float": (
            "activate_self_model_candidate",
            {**activate, "expected_row_version": 0.0},
        ),
        "activate_row_negative": (
            "activate_self_model_candidate",
            {**activate, "expected_row_version": -1},
        ),
        "activate_candidate_empty": (
            "activate_self_model_candidate",
            {**activate, "candidate_id": ""},
        ),
        "activate_candidate_non_string": (
            "activate_self_model_candidate",
            {**activate, "candidate_id": 7},
        ),
        "activate_ref_empty": (
            "activate_self_model_candidate",
            {**activate, "write_context_ref": ""},
        ),
        "activate_ref_non_string": (
            "activate_self_model_candidate",
            {**activate, "write_context_ref": 7},
        ),
        "activate_base_empty": (
            "activate_self_model_candidate",
            {**activate, "expected_active_revision": ""},
        ),
        "activate_base_non_string": (
            "activate_self_model_candidate",
            {**activate, "expected_active_revision": 7},
        ),
        "activate_confirmation_string": (
            "activate_self_model_candidate",
            {**activate, "ai_confirmation": sentinel},
        ),
        "query_extra": ("query_self_model", {"unexpected": sentinel}),
        "query_view_non_string": ("query_self_model", {"view": 7}),
        "query_scope_non_string": ("query_self_model", {"scope": 7}),
        "query_text_non_string": ("query_self_model", {"query": 7}),
        "query_limit_string": ("query_self_model", {"limit": "20"}),
        "query_limit_float": ("query_self_model", {"limit": 20.0}),
        "query_limit_bool": ("query_self_model", {"limit": True}),
        "query_limit_low": ("query_self_model", {"limit": 0}),
        "query_limit_high": ("query_self_model", {"limit": 101}),
        "query_content_string": ("query_self_model", {"include_content": sentinel}),
        "query_anchors_number": (
            "query_self_model",
            {"include_anchor_references": 1},
        ),
        "query_facet_item_non_string": ("query_self_model", {"facet_names": [1]}),
        "query_facets_non_array": ("query_self_model", {"facet_names": "name"}),
        "remember_version_string": (
            "remember_emotional_memory",
            {
                "write_context_ref": "ref",
                "expected_emotion_version": "0",
                "memory_type": "shared_event",
                "original_text": "我记得这次测试。",
                "summary": "我记得测试。",
                "primary_emotion": "calm",
                "reason": "我认为值得保存。",
            },
        ),
        "remember_importance_bool": (
            "remember_emotional_memory",
            {
                "write_context_ref": "ref",
                "expected_emotion_version": 0,
                "memory_type": "shared_event",
                "original_text": "我记得这次测试。",
                "summary": "我记得测试。",
                "primary_emotion": "calm",
                "reason": "我认为值得保存。",
                "importance": True,
            },
        ),
        "pin_confirmation_string": (
            "manage_brain_pin",
            {
                "write_context_ref": "ref",
                "expected_emotion_version": 0,
                "action": "confirm",
                "reason": "我确认这条锚点。",
                "pin_id": "pin",
                "ai_confirmation": sentinel,
            },
        ),
        "veto_extra": (
            "veto_ephemeral_memory",
            {
                "write_context_ref": "ref",
                "expected_emotion_version": 0,
                "reason": "我否决它。",
                "thread_id": "thread",
                "unexpected": sentinel,
            },
        ),
    }
    before = {
        "state": service.module_one_status()["state"],
        "candidates": len(service.store.list_candidates(service.model_id)),
        "revisions": len(service.store.list_revisions(service.model_id)),
        "events": len(service.store.list_events(service.model_id)),
    }
    results = {
        label: await rejected(name, arguments)
        for label, (name, arguments) in cases.items()
    }
    after = {
        "state": service.module_one_status()["state"],
        "candidates": len(service.store.list_candidates(service.model_id)),
        "revisions": len(service.store.list_revisions(service.model_id)),
        "events": len(service.store.list_events(service.model_id)),
    }
    print(json.dumps({
        "sentinel": sentinel,
        "before": before,
        "after": after,
        "results": results,
    }, sort_keys=True))


asyncio.run(main())
'''
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        result = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(result["before"], result["after"])
        for label, rejection in result["results"].items():
            self.assertTrue(rejection["rejected"], (label, rejection))
            self.assertEqual("ToolError", rejection["error_type"], (label, rejection))
            self.assertNotIn(result["sentinel"], rejection["message"], (label, rejection))


if __name__ == "__main__":
    unittest.main()
