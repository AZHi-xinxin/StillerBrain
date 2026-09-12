"""Profile-specific presentation; synthetic stores only, no server/network import."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp_server.learning_service import LearningMemoryAccessService
from mcp_server.service import SelfModelAccessService
from mcp_server.tests.test_onboarding_facade import model_content
from mcp_server.tool_guidance_service import ToolGuidanceAccessService
from runtime import ModuleOneOnboardingStore, SelfModelStore
from runtime.learning_memory import LearningMemoryStore
from runtime.ordinary_access import current_ordinary_access
from runtime.tool_guidance import ToolGuidanceStore


class Component:
    """Content-free stand-in recording which manual/ref the facade requested."""

    def __init__(self):
        self.manual_calls = []
        self.contract_calls = []

    def status(self):
        return {
            "status": "available", "row_version": 4,
            "counts": {"pending_changes": 0}, "configured_scope_count": 1,
            "scopes": {"learning_memory": {"row_version": 3}},
            "global_mode": "active", "automatic_exposure": "hard_off",
        }

    def manual(self, **kwargs):
        self.manual_calls.append(kwargs)
        return {"purpose": "Synthetic manual.", "status": self.status()}

    def current_action_contract(self, **kwargs):
        self.contract_calls.append(kwargs)
        return {"write_context_available": False, "allowed_calls": []}


class SimpleAccessStatusLabelTests(unittest.TestCase):
    ORDINARY = {
        "emotional_memory": "emotional", "learning_memory": "learning",
        "tool_guidance": "tool_guidance", "planning_memory": "planning",
        "self_governance_profile": "governance", "injection_control": "injection_control",
        "shared_person_authoring": "authoring_rewrite",
    }

    def setUp(self):
        self.enterContext(patch("socket.socket", side_effect=AssertionError("offline test")))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("offline test")))
        temporary = tempfile.TemporaryDirectory(prefix="simple-status-synthetic-")
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "synthetic.db"
        self.onboarding = ModuleOneOnboardingStore(
            self.database, capability_secret=b"simple-status-synthetic-secret-32-bytes",
        )
        self.components = {name: Component() for name in (*self.ORDINARY, "hallucination_vault")}
        self.arguments = {
            "model_id": "model:synthetic-status", "owner_id": "owner:synthetic-status",
            "onboarding": self.onboarding,
            **{parameter: self.components[name] for name, parameter in self.ORDINARY.items()},
            "hallucination_vault": self.components["hallucination_vault"],
        }
        self.service = self.make_service(True)

    def make_service(self, enabled=None):
        args = dict(self.arguments)
        if enabled is not None:
            args["ordinary_memory_access"] = enabled
        return SelfModelAccessService(SelfModelStore(self.database), **args)

    def wake(self, event):
        common = {"owner_id": self.service.owner_id, "model_id": self.service.model_id}
        wake = self.onboarding.issue_wake(
            **common, host_id="host:synthetic-status", thread_id="thread:synthetic-status",
            source_kind="human_message", source_event_id=event,
        )
        args = {**common, "wake_id": wake["wake_id"], "wake_capability": wake["wake_capability"]}
        prepared = self.onboarding.build_pre_generation_context(
            **args, source_digest="synthetic:" + event, host_contract_digest="synthetic:contract",
        )
        self.assertEqual("injected", self.onboarding.confirm_context_injected(
            **args, context_hash=prepared["context_hash"],
        )["decision"])
        return args

    def candidate_wait(self):
        args = self.wake("candidate-author")
        for action, payload in (
            ("confirm_brain_intro", {"acknowledged": True}),
            ("confirm_module_intro", {"acknowledged": True}),
            ("save_calm_prompt", {"text": "I will review this synthetic candidate carefully."}),
            ("submit_candidate", {
                "content": model_content("simple-status"),
                "diff": [{"op": "replace", "path": "/active_identity_capsule"}],
                "reason": "Synthetic candidate only.", "evidence_refs": ["memory://synthetic/status"],
                "expected_active_revision": None,
            }),
        ):
            self.onboarding.open_brain_context(
                owner_id=self.service.owner_id, model_id=self.service.model_id,
            )
            result = self.onboarding.advance(
                **args, expected_row_version=self.service.module_one_status()["state"]["row_version"],
                action=action, payload=payload,
            )
        self.assertEqual("pending", result["decision"])
        return args

    def test_profile_is_explicit_bool_not_environment_or_model_name(self):
        with patch.dict(os.environ, {"STBRAIN_ACCESS_PROFILE": "simple-memory-v1"}):
            legacy = self.make_service()
            self.assertFalse(legacy.ordinary_memory_access)
            self.assertEqual("locked", legacy.open_brain(view="summary")["learning_memory"]["status"])
        for invalid in ("true", "simple-memory-v1", 1, None, {}):
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaisesRegex(ValueError, "ordinary_memory_access"):
                    SelfModelAccessService(SelfModelStore(self.database),
                                           **self.arguments, ordinary_memory_access=invalid)

    def test_simple_summary_shows_seven_read_only_without_unlock_or_write_grant(self):
        before = self.service.module_one_status()
        result = self.service.open_brain(view="summary")
        for module in self.ORDINARY:
            self.assertEqual("read_only", result[module]["status"], module)
        self.assertEqual("locked", result["hallucination_vault"]["status"])
        self.assertFalse(result["current_status"]["module_one_unlocked"])
        self.assertFalse(result["write_context_available"])
        self.assertNotIn("write_context_ref", result)
        self.assertFalse(result["review_material_presented"])
        self.assertIn("目前只读", result["write_usage"])
        self.assertFalse(result["access_profile"]["ordinary_memory_writable"])
        self.assertTrue(result["access_profile"]["ordinary_memory_readable"])
        self.assertTrue(result["access_profile"]["module_one_required_for_ordinary_writes"])
        self.assertIn("stbrain_help", result["manual_access"]["instruction"])
        self.assertIn("相应连接绑定", result["manual_access"]["instruction"])
        self.assertEqual(before, self.service.module_one_status())
        self.assertTrue(all(not c.manual_calls for c in self.components.values()))
        self.assertIsNone(current_ordinary_access(owner_id=self.service.owner_id, model_id=self.service.model_id))

    def test_legacy_summary_and_manual_retain_original_gate(self):
        for enabled in (None, False):
            legacy = self.make_service(enabled)
            result = legacy.open_brain(view="summary")
            for module in self.components:
                self.assertEqual("locked", result[module]["status"])
            self.assertNotIn("access_profile", result)
            selected = legacy.open_brain(view="manual", module="learning_memory")
            self.assertFalse(selected["manual_available"])
            self.assertEqual("module_one_required", selected["manual_reason_code"])

    def test_health_labels_do_not_unlock_vault_or_self_model(self):
        result = self.service.health()
        for name in ("module_two", "module_three", "module_four", "module_five",
                     "self_governance", "injection_control", "shared_person_authoring"):
            self.assertEqual("read_only", result[name]["status"])
        self.assertEqual("factory", result["module_one"]["stage"])
        self.assertFalse(result["module_one"]["unlocked"])
        self.assertEqual("locked", result["hallucination_vault"]["status"])
        legacy = self.make_service(False).health()
        self.assertEqual("locked", legacy["module_two"]["status"])
        self.assertNotIn("access_profile", legacy)

    def test_each_ordinary_manual_is_readable_and_only_selected_manual_is_called(self):
        for module in self.ORDINARY:
            with self.subTest(module=module):
                for component in self.components.values():
                    component.manual_calls.clear()
                before = self.service.module_one_status()
                result = self.service.open_brain(view="manual", module=module)
                self.assertTrue(result["manual_available"])
                self.assertEqual("instructions_only", result["manual_scope"])
                self.assertEqual("read_only", result[module]["status"])
                self.assertEqual("read_only", result[module]["access_status"])
                self.assertFalse(result["review_material_presented"])
                self.assertFalse(result["write_context_available"])
                self.assertEqual(1, len(self.components[module].manual_calls))
                self.assertTrue(all(not c.manual_calls for n, c in self.components.items() if n != module))
                self.assertEqual(before, self.service.module_one_status())

    def test_module_one_and_vault_selected_manual_keep_real_boundaries(self):
        result = self.service.open_brain(view="manual", module="hallucination_vault")
        self.assertFalse(result["manual_available"])
        self.assertEqual("module_one_required", result["manual_reason_code"])
        self.assertEqual([], self.components["hallucination_vault"].manual_calls)
        before = self.service.module_one_status()
        result = self.service.open_brain(view="manual", module="self_revision")
        self.assertEqual(before, result["current_status"])
        self.assertFalse(result["current_status"]["module_one_unlocked"])
        self.assertNotIn("manual_scope", result)

    def test_uncompleted_module_one_ref_is_not_reused_for_ordinary_review(self):
        self.wake("ordinary-instructions")
        result = self.service.open_brain(view="manual", module="learning_memory")
        self.assertTrue(result["write_context_available"])
        self.assertEqual([{"write_context_ref": None}], self.components["learning_memory"].manual_calls)
        self.assertFalse(result["review_material_presented"])
        self.assertIsNone(current_ordinary_access(owner_id=self.service.owner_id, model_id=self.service.model_id))

    def test_simple_full_registry_and_manuals_are_consistent(self):
        result = self.service.open_brain(view="full")
        for module in self.ORDINARY:
            self.assertIn(module, result)
            self.assertEqual("read_only", result[module]["status"])
        registry = {item["module"]: item["status"] for item in result["module_registry"]}
        for name in ("emotional_memory_module_two", "learning_memory_module_three",
                     "tool_guidance_module_four", "planning_memory_module_five",
                     "self_governance_profile", "injection_control"):
            self.assertEqual("read_only", registry[name])
        self.assertEqual("locked", registry["hallucination_reality_review_vault"])
        self.assertNotIn("hallucination_vault", result)
        self.assertFalse(result["current_status"]["module_one_unlocked"])

    def test_candidate_wait_and_real_wake_requirement_are_not_relabelled_as_live(self):
        self.candidate_wait()
        before = self.service.module_one_status()
        self.assertEqual("candidate_wait", before["state"]["stage"])
        result = self.service.open_brain(view="summary")
        self.assertEqual("candidate_wait", result["current_status"]["state"]["stage"])
        self.assertFalse(result["current_status"]["module_one_unlocked"])
        self.assertFalse(result["review_material_presented"])
        self.assertTrue(before["wake_boundary_required"])
        self.assertEqual(before, self.service.module_one_status())
        self.assertEqual("read_only", result["learning_memory"]["status"])

    def test_display_helper_keeps_legacy_live_and_unknown_scope_behavior(self):
        status = copy.deepcopy(self.service.module_one_status())
        self.assertFalse(self.service._module_access_display("hallucination_vault", status))
        self.assertFalse(self.service._module_access_display("unknown", status))
        status["module_one_unlocked"] = True
        status["state"]["stage"] = "live"
        active_projection = self.service._compact_open_result({}, status)
        for module in self.ORDINARY:
            self.assertEqual("available", active_projection[module]["status"])
        self.assertTrue(active_projection["access_profile"]["ordinary_memory_writable"])
        self.assertIn("已激活", active_projection["write_usage"])
        self.assertEqual("available", self.service._module_status_display("emotional_memory", status, "active"))
        legacy = self.make_service(False)
        self.assertEqual("active", legacy._module_status_display("emotional_memory", status, "active"))
        self.assertTrue(legacy._module_access_display("learning_memory", status))
        self.assertEqual("synthetic-ref", legacy._manual_context_ref(
            "learning_memory", {"write_context_ref": "synthetic-ref"}, status,
        ))

    def test_real_learning_and_tool_manuals_are_readable_without_false_gate(self):
        common = {"onboarding": self.onboarding, "owner_id": self.service.owner_id,
                  "model_id": self.service.model_id}
        self.service.learning = LearningMemoryAccessService(LearningMemoryStore(self.database), **common)
        self.service.tool_guidance = ToolGuidanceAccessService(ToolGuidanceStore(self.database), **common)
        self.wake("real-component-instructions")
        before = self.service.module_one_status()
        for module in ("learning_memory", "tool_guidance"):
            with self.subTest(module=module):
                result = self.service.open_brain(view="manual", module=module)
                self.assertTrue(result["manual_available"])
                self.assertEqual("instructions_only", result["manual_scope"])
                self.assertEqual([], result[module]["reason_codes"])
                self.assertFalse(result["review_material_presented"])
        self.assertEqual(before, self.service.module_one_status())

    def test_missing_module_is_not_advertised_as_configured_or_readable(self):
        self.service.learning = None
        summary = self.service.open_brain(view="summary")
        self.assertNotIn("learning_memory", summary)
        self.assertNotIn("learning_memory", summary["manual_access"]["modules"])
        manual = self.service.open_brain(view="manual", module="learning_memory")
        self.assertFalse(manual["manual_available"])
        self.assertEqual("module_not_configured", manual["manual_reason_code"])


if __name__ == "__main__":
    unittest.main()
