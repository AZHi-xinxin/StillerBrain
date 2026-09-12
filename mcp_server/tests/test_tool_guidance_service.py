from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from mcp_server.tool_guidance_service import ToolGuidanceAccessService
from runtime.tool_guidance import ToolGuidanceStore
from tests.test_tool_guidance import action_card, catalog


class FakeOnboarding:
    def __init__(self) -> None:
        self.allowed = False
        self.contexts: dict[str, dict[str, Any]] = {}

    def add_wake(self, ref: str, wake_id: str, wake_seq: int) -> None:
        self.contexts[ref] = {
            "write_context_available": True,
            "wake_id": wake_id,
            "wake_seq": wake_seq,
        }

    def authorize_other_module_write(self, **_: Any) -> dict[str, Any]:
        return {"decision": "allowed" if self.allowed else "reject"}

    def current_open_write_context(
        self, *, write_context_ref: str, **_: Any
    ) -> dict[str, Any]:
        return self.contexts.get(
            write_context_ref, {"write_context_available": False}
        )

    def contains_protected_persistence_value(self, **_: Any) -> bool:
        return False


class ToolGuidanceAccessServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.store = ToolGuidanceStore(
            self.database, detail_lookup_schema_hash="b" * 64
        )
        self.onboarding = FakeOnboarding()
        self.live_catalog = catalog()
        self.service = ToolGuidanceAccessService(
            self.store,
            onboarding=self.onboarding,
            owner_id="owner:service",
            model_id="model:service",
            catalog_provider=lambda _binding: self.live_catalog,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def remember(self, *, ref: str = "ref:one", expected: int = 0, **overrides: object) -> dict:
        fields = action_card(**overrides)
        return self.service.remember(
            write_context_ref=ref,
            expected_tool_row_version=expected,
            **fields,
        )

    def test_module_one_and_open_wake_gate_writes_without_mutation(self) -> None:
        self.onboarding.add_wake("ref:one", "wake:one", 1)
        before = self.service.status()
        locked = self.remember()
        self.assertEqual("reject", locked["decision"])
        self.assertEqual(["module_one_required"], locked["reason_codes"])
        self.assertEqual(before, self.service.status())

        self.onboarding.allowed = True
        unopened = self.remember(ref="ref:missing")
        self.assertEqual(["brain_open_required"], unopened["reason_codes"])
        self.assertEqual(before, self.service.status())

    def test_valid_wake_ref_and_cas_create_then_precise_recall(self) -> None:
        self.onboarding.allowed = True
        self.onboarding.add_wake("ref:one", "wake:one", 1)
        stored = self.remember()
        self.assertEqual("stored", stored["decision"])
        self.assertEqual(1, stored["tool_row_version"])
        self.assertFalse(stored["execution_performed"])
        card_id = stored["card"]["card_id"]

        stale_cas = self.service.record_experience(
            write_context_ref="ref:one",
            expected_tool_row_version=0,
            card_id=card_id,
            outcome="unknown",
            reason_code="no_current_result",
            attempt_summary="No conclusive current native result was available.",
            lesson="Do not claim completion.",
            confidence=60,
        )
        self.assertEqual("reject", stale_cas["decision"])
        self.assertEqual(["tool_row_version_conflict"], stale_cas["reason_codes"])

        precise = self.service.recall(card_id=card_id, view="card")
        self.assertEqual("precise_result", precise["decision"])
        self.assertEqual(card_id, precise["results"][0]["card_id"])
        self.assertEqual("matched", precise["results"][0]["schema_status"])
        self.assertFalse(precise["execution_performed"])

        other = ToolGuidanceAccessService(
            self.store,
            onboarding=self.onboarding,
            owner_id="owner:other",
            model_id="model:service",
            catalog_provider=self.live_catalog,
        )
        self.assertEqual([], other.recall(card_id=card_id, view="card")["results"])

    def test_legacy_candidate_is_presented_by_manual_then_reviewed_in_later_wake(self) -> None:
        self.onboarding.allowed = True
        self.onboarding.add_wake("ref:one", "wake:one", 10)
        stored = self.remember()
        card_id = stored["card"]["card_id"]
        # Seed a legacy proposal directly; ordinary service.revise now appends.
        pending = self.store.propose_revision(
            owner_id="owner:service", model_id="model:service", wake_id="wake:one", wake_seq=10,
            catalog=self.live_catalog,
            expected_row_version=1,
            card_id=card_id,
            expected_card_version=1,
            intent="revise",
            edit_class="major",
            reason="I learned that the operation applies only to a narrower, supported device set.",
            correctness_assessment=(
                "The current live catalog matches the saved schema, and narrowing the supported "
                "device set is more accurate while provider availability remains uncertain."
            ),
            calm_check_stability="I compared this issue against the active card and catalog rather than reacting to one failed call.",
            calm_check_necessity="The active purpose is materially broad, so only querying details would not correct future recall.",
            calm_check_consequences="The narrower purpose reduces false matches without weakening confirmation or authorization checks.",
            calm_check_alternatives="I considered downweighting the card, but that would retain inaccurate operational meaning.",
            purpose="Change a supported, specifically requested home-device state after current checks.",
        )
        self.assertEqual("candidate_pending", pending["decision"])
        self.assertEqual(2, pending["tool_row_version"])

        same_wake_review = self.service.review(
            write_context_ref="ref:one",
            expected_tool_row_version=2,
            candidate_id=pending["candidate_id"],
            candidate_hash=pending["candidate_hash"],
            decision="accept",
            correctness_decision="correct",
            correctness_assessment="I reviewed the complete candidate and still find the narrower statement more accurate.",
            reason="This intentionally tests the later-wake boundary.",
            ai_confirmation=True,
            expected_base_version=1,
        )
        self.assertEqual(["later_real_wake_required"], same_wake_review["reason_codes"])

        self.onboarding.add_wake("ref:two", "wake:two", 11)
        opened = self.service.manual(write_context_ref="ref:two")
        self.assertEqual(1, len(opened["pending_candidates"]))
        shown = opened["pending_candidates"][0]
        self.assertEqual(pending["candidate_hash"], shown["candidate_hash"])
        self.assertIn("calm_check", shown)
        self.assertIn("diff", shown)

        accepted = self.service.review(
            write_context_ref="ref:two",
            expected_tool_row_version=2,
            candidate_id=pending["candidate_id"],
            candidate_hash=pending["candidate_hash"],
            decision="accept",
            correctness_decision="correct",
            correctness_assessment=(
                "In this later real wake I compared the full candidate, exact diff, active base, "
                "and current matching schema, and I still judge it more correct."
            ),
            reason="I independently accept the candidate after the required later-wake review.",
            ai_confirmation=True,
            expected_base_version=1,
        )
        self.assertEqual("accepted", accepted["decision"])
        self.assertEqual(2, accepted["card"]["version"])
        self.assertFalse(accepted["execution_performed"])

    def test_ordinary_context_light_card_direct_revision_and_directory(self) -> None:
        self.onboarding.allowed = True
        self.onboarding.add_wake("ordinary-ref", "ordinary-wake", 1)
        self.onboarding.contexts["ordinary-ref"]["context_mode"] = "ordinary_authenticated"
        self.live_catalog = None
        saved = self.service.remember(write_context_ref="ordinary-ref", expected_tool_row_version=0,
            tool_name="家庭服务", purpose="到家时查看设备情况。", reminder="到家了，可以看看设备状态。", keywords=["到家"],
            reason="Synthetic ordinary authenticated authoring")
        self.assertEqual("stored", saved["decision"])
        changed = self.service.revise(write_context_ref="ordinary-ref", expected_tool_row_version=1,
            card_id=saved["card"]["card_id"], expected_card_version=1,
            reason="Synthetic direct revision", reminder="回家后可以看看设备状态。")
        self.assertEqual("direct_revision", changed["submission_mode"])
        self.assertTrue(self.service.recall_for_injection(query="到家")["envelopes"])
        directory = self.service.recall(view="directory")
        self.assertEqual(saved["card"]["card_id"], directory["results"][0]["card_id"])
        self.assertEqual(0, self.service.status()["counts"]["pending_candidates"])

    def test_service_withdraw_old_pending_needs_no_catalog_or_review_essay(self) -> None:
        self.onboarding.allowed = True
        self.onboarding.add_wake("ref:one", "wake:one", 1)
        card = self.remember()["card"]
        pending = self.store.propose_revision(owner_id="owner:service", model_id="model:service",
            wake_id="old-wake", wake_seq=100, expected_row_version=1,
            card_id=card["card_id"], expected_card_version=1, intent="revise", edit_class="major",
            purpose="Synthetic narrower home-device advice.", reason="Synthetic legacy proposal",
            correctness_assessment="I compared the complete synthetic proposal with its source.",
            calm_check_stability="The meaning remains stable across ordinary observations.",
            calm_check_necessity="The narrower purpose makes this synthetic card more accurate.",
            calm_check_consequences="Actual authorization and execution boundaries remain unchanged.",
            calm_check_alternatives="Keeping the previous version remains a possible choice.", catalog=None)
        self.live_catalog = None
        result = self.service.review(write_context_ref="ref:one", expected_tool_row_version=2,
            candidate_id=pending["candidate_id"], candidate_hash=pending["candidate_hash"],
            expected_base_version=1, decision="withdraw", reason="退出旧候选。")
        self.assertEqual("withdraw", result["decision"])
        self.assertFalse(result["active_version_changed"])
        self.assertFalse(result["execution_performed"])

    def test_conflict_error_explains_directory_lookup(self) -> None:
        self.onboarding.allowed = True
        self.onboarding.add_wake("ref:one", "wake:one", 1)
        card = self.remember()["card"]
        result = self.service.revise(write_context_ref="ref:one", expected_tool_row_version=0,
            card_id=card["card_id"], expected_card_version=1, reason="Synthetic stale write",
            reminder="新的作者提醒。")
        self.assertEqual(["tool_row_version_conflict"], result["reason_codes"])
        self.assertEqual("directory", result["lookup_guidance"]["arguments"]["view"])
        self.assertFalse(result["state_changed"])

    def test_risk_rejections_explain_high_floor_without_changing_cards(self) -> None:
        self.onboarding.allowed = True
        self.onboarding.add_wake("ref:one", "wake:one", 1)
        before = self.service.status()
        for risk, confirmation in (
            ("low", "explicit_each_time"),
            ("medium", "explicit_each_time"),
            ("high", "contextual"),
            ("critical", "contextual"),
        ):
            with self.subTest(risk=risk, confirmation=confirmation):
                result = self.remember(risk_level=risk, confirmation_policy=confirmation)
                self.assertEqual(["risk_below_runtime_floor"], result["reason_codes"])
                self.assertEqual("high", result["repair_guidance"]["real_world_action_minimum_risk_level"])
                self.assertFalse(result["repair_guidance"]["critical_required"])
                self.assertEqual("none", result["repair_guidance"]["permission_authority"])
                self.assertFalse(result["state_changed"])
                self.assertFalse(result["execution_performed"])
                self.assertEqual(before, self.service.status())

        stored = self.remember(risk_level="high", confirmation_policy="explicit_each_time")
        self.assertEqual("stored", stored["decision"])
        self.assertEqual("high", stored["card"]["content"]["risk_level"])

    def test_control_character_tag_rejection_keeps_natural_language_tags_visible(self) -> None:
        self.onboarding.allowed = True
        self.onboarding.add_wake("ref:one", "wake:one", 1)
        before = self.service.status()
        rejected = self.remember(scenario_tags=["到家\n开灯"])
        self.assertEqual(["invalid_scenario_tags_item"], rejected["reason_codes"])
        self.assertEqual("scenario_tags", rejected["repair_guidance"]["field"])
        self.assertIn("中文", rejected["repair_guidance"]["message"])
        self.assertEqual(before, self.service.status())
        manual = self.service.manual()
        self.assertIn("high", manual["authoring_constraints"]["real_world_action"])
        self.assertIn("中文", manual["authoring_constraints"]["scenario_tags"])

        stored = self.remember(
            scenario_tags=["到家", "home.arrival"],
            scenario_examples=["昕昕到家后明确要求打开灯。"],
            keywords=["到家", "开灯"],
        )
        self.assertEqual("stored", stored["decision"])
        self.assertEqual(["到家", "home.arrival"], stored["card"]["content"]["scenario_tags"])

    def test_injection_and_execution_facades_only_advise(self) -> None:
        self.onboarding.allowed = True
        self.onboarding.add_wake("ref:one", "wake:one", 1)
        stored = self.remember()
        card_id = stored["card"]["card_id"]

        recalled = self.service.recall_for_injection(query="home.arrival")
        self.assertEqual("surface_short_summary", recalled["decision"])
        self.assertFalse(recalled["execution_performed"])
        summary = recalled["envelopes"][0]["content"]["scene_summary"]
        self.assertLessEqual(len(summary), 100)

        gate = self.service.execution_gate(
            card_id=card_id,
            current_user_intent=True,
            authorization_verified=True,
            current_confirmation=True,
        )
        self.assertEqual("allowed_to_attempt", gate["decision"])
        self.assertFalse(gate["execution_performed"])
        for forbidden_method in (
            "execute_tool",
            "call_any_tool",
            "grant_permission",
            "set_tool_available",
        ):
            self.assertFalse(hasattr(self.service, forbidden_method))

    def test_service_rejects_secrets_without_echoing_them(self) -> None:
        self.onboarding.allowed = True
        self.onboarding.add_wake("ref:one", "wake:one", 1)
        secret = "Bearer abcdefghijklmnopqrstuvwxyz"
        rejected = self.remember(call_notes=f"Authorization: {secret}")
        self.assertEqual("reject", rejected["decision"])
        self.assertEqual(["credential_or_secret_detected"], rejected["reason_codes"])
        self.assertNotIn(secret, str(rejected))
        self.assertEqual(0, self.service.status()["counts"]["active_cards"])

    def test_experience_cannot_claim_receipt_or_verified_provenance(self) -> None:
        self.onboarding.allowed = True
        self.onboarding.add_wake("ref:one", "wake:one", 1)
        stored = self.remember()
        rejected = self.service.record_experience(
            write_context_ref="ref:one",
            expected_tool_row_version=1,
            card_id=stored["card"]["card_id"],
            outcome="success",
            reason_code="claimed_success",
            attempt_summary="I saw a result but this test must not accept a client receipt claim.",
            lesson="Keep v0.2 provenance explicitly ai_reported.",
            confidence=60,
            verified=True,
            receipt={"tool_call_id": "forged"},
        )
        self.assertEqual("reject", rejected["decision"])
        self.assertEqual(["invalid_tool_guidance_fields"], rejected["reason_codes"])
        self.assertEqual(0, self.service.status()["counts"]["experiences"])
        self.assertEqual(1, self.service.status()["row_version"])


if __name__ == "__main__":
    unittest.main()
