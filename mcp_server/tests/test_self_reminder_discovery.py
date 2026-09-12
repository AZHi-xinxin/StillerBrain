"""DIY reminder discovery and existing scene behavior, synthetic/local only."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp_server.usage_guide import simple_usage_guide, usage_guide, module_usage_guide
from runtime.onboarding import OPTIONAL_BRAIN_NOTICE
from runtime.self_governance import GOVERNANCE_SCOPES, SelfGovernanceStore


class SelfReminderDiscoveryTests(unittest.TestCase):
    def test_overviews_expose_read_and_write_door_without_authored_examples(self):
        for builder in (simple_usage_guide, usage_guide):
            with self.subTest(profile=builder.__name__):
                with patch("sqlite3.connect", side_effect=AssertionError("static_help_touched_store")):
                    result = builder()
                self.assertFalse(result["state_changed"])
                door = result["self_reminders"]
                self.assertIn("轻提醒", door["title"])
                self.assertIn("安全阀", door["title"])
                self.assertIn("默认空白", door["purpose"])
                self.assertEqual("self_governance_profile", door["help"]["arguments"]["module"])
                self.assertEqual("query_self_governance_profile", door["read_entry"]["tool"])
                for action in ("set", "clear", "rollback"):
                    self.assertIn(action, door["write"])
                for mode in ("manual_only", "scene_relevant", "global"):
                    self.assertIn(mode, door["appearance"])
                self.assertIn("并非每轮常驻", door["appearance"])
                self.assertNotIn("text", door)
                self.assertNotIn("example", door)

    def test_module_help_explains_scopes_planning_and_module_one_boundary(self):
        for simple in (True, False):
            with self.subTest(simple=simple):
                result = module_usage_guide("self_governance_profile", simple=simple)
                self.assertEqual(set(GOVERNANCE_SCOPES), set(result["scopes"]))
                text = json.dumps(result, ensure_ascii=False)
                for phrase in ("中文标签", "规划场景", "global", "trigger_mode", "模块一", "冷静词", "独立流程"):
                    self.assertIn(phrase, text)
                self.assertNotIn("example", result)
                self.assertFalse(result["state_changed"])

    def test_connection_and_optional_first_notice_name_the_discoverable_door(self):
        self.assertIn("轻提醒与安全阀", OPTIONAL_BRAIN_NOTICE)
        self.assertIn("stbrain_help(module='self_governance_profile')", OPTIONAL_BRAIN_NOTICE)
        tree = ast.parse((Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8"))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "mcp" for target in node.targets))
        instructions = next(keyword.value for keyword in assignment.value.keywords if keyword.arg == "instructions")
        for branch in (instructions.body, instructions.orelse):
            text = "".join(node.value for node in ast.walk(branch)
                           if isinstance(node, ast.Constant) and isinstance(node.value, str))
            self.assertIn("轻提醒与自定义安全阀", text)
            self.assertIn("stbrain_help(module='self_governance_profile')", text)
            self.assertIn("正文默认空白", text)

    def test_global_reminder_is_scene_selected_and_preserves_the_authors_exact_text(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-self-reminder-door-") as folder:
            store = SelfGovernanceStore(Path(folder) / "brain.sqlite3")
            identity = {"owner_id": "synthetic-owner", "model_id": "synthetic-model"}
            text = "合成测试作者自行选择的计划提醒。"
            result = store.commit_revision(
                **identity, scope="global", operation="set",
                content={"schema_version": "0.1.0", "text": text,
                         "trigger_mode": "scene_relevant", "scene_tags": ["计划"]},
                wake_id="synthetic-wake-1", wake_seq=1,
                expected_row_version=0, expected_active_revision=None,
            )
            self.assertTrue(result["active_changed"])
            self.assertFalse(result["external_permission_changed"])
            unrelated = store.build_injection(**identity, query="聊一聊今天的小说。")
            self.assertEqual({}, unrelated["injection"])
            matching = store.build_injection(**identity, query="我想看看这个计划。")
            self.assertEqual(["global"], matching["selected_scopes"])
            item = matching["injection"]["scopes"][0]
            self.assertEqual(text, item["text"])
            self.assertEqual("ai_self", item["authorship"])
            self.assertEqual("none", item["external_permission_authority"])
            manual = store.manual(**identity)
            self.assertIn("并非每轮常驻", manual["scope_usage"]["global"])
            self.assertIsNone(manual["blank_structure"]["text"])


if __name__ == "__main__":
    unittest.main()
