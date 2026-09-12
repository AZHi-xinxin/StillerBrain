"""Static public-description checks; no server import, DB, network or model use."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest
from unittest.mock import patch

from runtime.ordinary_access import ORDINARY_READ_TOOLS, ORDINARY_TOOLS


class ToolDescriptionConsistencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8"))
        cls.functions = {}
        for node in ast.walk(cls.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(isinstance(decorator, ast.Call)
                   and isinstance(decorator.func, ast.Attribute)
                   and isinstance(decorator.func.value, ast.Name)
                   and decorator.func.value.id == "mcp" and decorator.func.attr == "tool"
                   for decorator in node.decorator_list):
                cls.functions[node.name] = node
        cls.docs = {name: ast.get_docstring(node) or "" for name, node in cls.functions.items()}

    def test_tool_inventory_stays_45_and_all_descriptions_exist(self):
        self.assertEqual(45, len(self.docs))
        self.assertTrue(set(ORDINARY_TOOLS) <= set(self.docs))
        self.assertTrue(all(self.docs.values()))

    def test_every_ordinary_write_documents_activation_and_internal_binding(self):
        names = (set(ORDINARY_TOOLS) - set(ORDINARY_READ_TOOLS)) | {
            "remember_memory", "revise_memory", "advance_plan",
        }
        for name in sorted(names):
            with self.subTest(tool=name):
                doc = " ".join(self.docs[name].split())
                for phrase in ("simple-memory-v1", "read-only", "first", "activation",
                               "direct MCP", "gateway", "internal context", "mechanical module",
                               "author confirmation"):
                    self.assertIn(phrase, doc)
                if name not in {"remember_memory", "revise_memory", "advance_plan"}:
                    self.assertIn("Legacy mode retains its authorized context", doc)

    def test_every_ordinary_read_remains_available_before_first_activation(self):
        for name in sorted(ORDINARY_READ_TOOLS):
            with self.subTest(tool=name):
                self.assertIn("permits this read before module-one activation", self.docs[name])

    def test_learning_edits_and_integration_are_not_described_as_new_candidates(self):
        revise = " ".join(self.docs["revise_learning_memory"].split())
        integration = " ".join(self.docs["integrate_learning_memories"].split())
        self.assertIn("do not create a new review candidate", revise)
        self.assertIn("integration saves directly", integration)
        self.assertNotIn("create a later-wake major", revise)
        review = " ".join(self.docs["review_learning_change"].split())
        self.assertIn("previously stored", review)
        self.assertIn("complete matching review material", review)
        self.assertIn("no extra later-wake wait", review)
        self.assertIn("not candidate presentation proof", review)

    def test_planning_docs_match_direct_commit_and_retained_detailed_fields(self):
        create = " ".join(self.docs["remember_planning_memory"].split())
        self.assertIn("creates the active plan directly", create)
        self.assertIn("calm_check is legacy audit input", create)
        self.assertIn("ai_adoption_statement, calm_check, ai_confirmation=true and idempotency_key", create)
        self.assertNotIn("cannot become active this wake", create)
        self.assertNotIn("only after its full candidate", create)
        self.assertIn("Directly append", self.docs["revise_planning_memory"])
        self.assertIn("no extra later-wake wait", self.docs["review_planning_change"])

    def test_emotional_original_edits_route_to_actual_supported_schema(self):
        for name in ("remember_emotional_memory", "revise_emotional_memory"):
            doc = " ".join(self.docs[name].split())
            self.assertIn("revise_memory", doc)
            self.assertIn("changes.original_text", doc)
            self.assertIn("earlier", doc)
            self.assertNotIn("First call stbrain_open", doc)
            self.assertNotIn("original_text is immutable after this call", doc)
        args = self.functions["revise_emotional_memory"].args
        self.assertNotIn("original_text", {arg.arg for arg in args.args + args.kwonlyargs})

    def test_dedicated_emotional_manual_routes_body_fields_to_unified_revision(self):
        from mcp_server.emotional_service import EmotionalMemoryAccessService
        from mcp_server.usage_guide import module_usage_guide

        # Read the real manual without constructing a store or touching a brain.
        service = object.__new__(EmotionalMemoryAccessService)
        with patch.object(service, "status", return_value={"synthetic": True}):
            manual = service.manual()
        dedicated = manual["tools"]["revise_emotional_memory"]
        unified = manual["tools"]["revise_memory"]
        for field in ("original_text", "memory_type", "source_timestamp"):
            with self.subTest(field=field):
                self.assertIn("changes." + field, dedicated)
                self.assertIn(field, unified)
        self.assertIn("revise_memory", dedicated)
        self.assertIn("工具顶层", dedicated)
        self.assertIn("保留旧版本", dedicated)
        self.assertNotIn("追加正文", dedicated)
        for simple in (True, False):
            workflow = " ".join(module_usage_guide("emotional_memory", simple=simple)["workflow"])
            self.assertIn("revise_memory", workflow)
            self.assertIn("changes.original_text", workflow)
            self.assertIn("保留旧版本", workflow)

    def test_governance_and_vault_exceptions_are_not_given_ordinary_blanket_authority(self):
        governance = " ".join(self.docs["manage_self_governance_profile"].split())
        injection = " ".join(self.docs["manage_injection_control"].split())
        for doc in (governance, injection):
            self.assertIn("propose_*/activate/withdraw", doc)
            self.assertIn("legacy candidate", doc)
        self.assertIn("hallucination_vault", injection)
        self.assertIn("separate authorization and review flow", injection)
        self.assertIn("never grants an external tool", governance)
        self.assertIn("later-wake", self.docs["review_hallucination_restore"])

    def test_help_is_static_and_not_a_replacement_for_bound_candidate_review(self):
        help_doc = " ".join(self.docs["stbrain_help"].split())
        self.assertIn("module selects detailed", help_doc)
        self.assertIn("creates no write context", help_doc)
        self.assertIn("appropriately bound stbrain_open review flow", help_doc)
        self.assertIn("fully_presented=true", self.docs["stbrain_open"])
        self.assertIn("separate real wakes", self.docs["stbrain_open"])

    def test_pin_confirmation_is_explicit_and_does_not_rewrite_core_source(self):
        doc = " ".join(self.docs["manage_brain_pin"].split())
        self.assertIn("ai_confirmation=true", doc)
        self.assertIn("author can confirm immediately", doc)
        self.assertIn("legacy wake-bound requests retain", doc)
        self.assertIn("does not rewrite the underlying self-definition", doc)

    def test_plan_event_interfaces_and_gateway_auth_boundaries_remain_distinct(self):
        event = " ".join(self.docs["record_planning_event"].split())
        self.assertIn("plan_id/reason", event)
        self.assertIn("target_ref/expected_event_seq/note", event)
        self.assertIn("genuine", event)
        activate = " ".join(self.docs["activate_self_model_candidate"].split())
        self.assertIn("later independently verified real wake", activate)
        self.assertIn("only direct self writes require the deployment password", activate)

    def test_initializer_describes_version_history_and_explicit_legacy_boundaries(self):
        assignments = [node for node in self.tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "mcp" for target in node.targets)]
        self.assertEqual(1, len(assignments))
        call = assignments[0].value
        instructions = next(item.value for item in call.keywords if item.arg == "instructions")
        self.assertIsInstance(instructions, ast.IfExp)
        simple = ast.literal_eval(instructions.body)
        self.assertIn("首次完成设置并激活前", simple)
        self.assertIn("模块行版本", simple)
        self.assertIn("高级治理旧候选和黑匣子", simple)
        legacy = "".join(node.value for node in ast.walk(instructions.orelse)
                         if isinstance(node, ast.Constant) and isinstance(node.value, str))
        self.assertIn("authorized context", legacy)
        self.assertIn("earlier originals in version history", legacy)
        self.assertNotIn("Original memory text stays immutable", legacy)


if __name__ == "__main__":
    unittest.main()
