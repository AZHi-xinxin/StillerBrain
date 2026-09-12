"""All component gates share the same content-only credential contract."""
import ast
import importlib
from pathlib import Path
import unittest

from runtime.credential_guard import contains_credential_or_secret
from runtime import injection_control, self_revision, tool_guidance


class CredentialGuardWiringTests(unittest.TestCase):
    COMPONENTS = (
        "authoring", "emotional_memory", "hallucination_vault",
        "learning_idea_box", "learning_memory", "planning_memory", "self_governance",
    )

    def gates(self):
        for name in self.COMPONENTS:
            yield name, importlib.import_module("runtime." + name)._contains_secret
        yield "self_revision_compatibility", self_revision.contains_credential_or_secret
        yield "tool_guidance", lambda v: tool_guidance._forbidden_content(v) == "credential_or_secret_detected"

    def test_all_component_gates_reject_same_synthetic_cases(self):
        for name, gate in self.gates():
            for value in ("站点密码轮换：DEMO_OLD → DEMO_NEW", "token：DEMO_VALUE",
                          "密钥变更：DEMO_VALUE", {"record": {"password": "DEMO_VALUE"}}):
                with self.subTest(component=name, shape=type(value).__name__):
                    self.assertTrue(gate(value))

    def test_all_component_gates_preserve_harmless_content(self):
        values = (
            "本轮用量 token: 1200", "密码学是一门学科",
            "API Key: 存放于环境变量 DEMO_API_KEY",
            {"topic": "token", "punctuation_example": ": means a colon"},
        )
        for name, gate in self.gates():
            for value in values:
                with self.subTest(component=name, shape=type(value).__name__):
                    self.assertFalse(gate(value))

    def test_injection_control_uses_shared_gate_without_rewriting(self):
        original = "本轮用量 token: 1200"
        self.assertEqual(injection_control._required_text("reason", original, 500), original)
        with self.assertRaisesRegex(injection_control.InjectionControlError, "credential_or_secret_detected"):
            injection_control._required_text("reason", "token：DEMO_VALUE", 500)

    def test_no_component_retains_private_pattern_copies(self):
        root = Path(self_revision.__file__).parent
        for name in (*self.COMPONENTS, "injection_control", "self_revision", "tool_guidance"):
            tree = ast.parse((root / (name + ".py")).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    self.assertNotEqual(node.id, "_SECRET_PATTERNS", name)


if __name__ == "__main__":
    unittest.main()
