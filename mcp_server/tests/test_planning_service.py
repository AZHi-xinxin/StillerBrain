from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mcp_server.planning_service import PlanningMemoryAccessService
from runtime.planning_memory import PLANNING_MODULE, PlanningMemoryStore


def calm() -> dict[str, object]:
    return {
        "authorship_confirmed": True,
        "current_state_checked": True,
        "dependencies_checked": True,
        "consequences_reviewed": True,
        "rollback_understood": True,
        "notes": "我已完成平静复核。",
    }


def plan_content(title: str = "完成明日测试") -> dict[str, object]:
    return {
        "kind": "task",
        "track": "internal",
        "title": title,
        "original_text": f"我愿意安排并完成：{title}",
        "summary": title,
        "reminder": "到合适场景再想起",
        "importance": 80,
        "presence_mode": "relevant",
        "scene_tags": ["明日测试", title],
        "keywords": [title],
        "start_at": None,
        "due_at": None,
        "timezone": "Asia/Shanghai",
        "review_after": None,
        "allow_coordination_hint": True,
        "parent_ref": None,
        "dependency_refs": [],
        "ai_adoption_statement": f"我愿意把“{title}”作为自己的计划。",
    }


class FakeOnboarding:
    def __init__(self) -> None:
        self.allowed = True
        self.write_context_ref = "artifact_open_context"
        self.wake_id = "wake-1"
        self.wake_seq = 1

    def authorize_other_module_write(self, **fields: object) -> dict[str, object]:
        self.last_module = fields.get("module_name")
        return {"decision": "allowed" if self.allowed else "reject"}

    def current_open_write_context(self, **fields: object) -> dict[str, object]:
        available = fields.get("write_context_ref") == self.write_context_ref
        return {
            "write_context_available": available,
            "wake_id": self.wake_id,
            "wake_seq": self.wake_seq,
        }

    def contains_protected_persistence_value(self, **_fields: object) -> bool:
        return False


class PlanningServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = PlanningMemoryStore(Path(self.temp.name) / "planning.sqlite3")
        self.onboarding = FakeOnboarding()
        self.service = PlanningMemoryAccessService(
            self.store,
            onboarding=self.onboarding,  # type: ignore[arg-type]
            owner_id="owner-a",
            model_id="model-a",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def remember(self) -> dict[str, object]:
        return self.service.remember(
            write_context_ref=self.onboarding.write_context_ref,
            expected_planning_version=self.service.status()["row_version"],
            content=plan_content(),
            reason="我决定保存这个计划。",
            calm_check=calm(),
            ai_confirmation=True,
            idempotency_key="remember-1",
        )

    def activate_pending(self, pending: dict[str, object]) -> dict[str, object]:
        self.onboarding.wake_id = "wake-2"
        self.onboarding.wake_seq = 2
        manual = self.service.manual(write_context_ref=self.onboarding.write_context_ref)
        candidate = manual["pending_changes"][0]
        self.assertTrue(candidate["fully_presented"])
        self.assertEqual(1, len(manual["current_action_contract"]["allowed_calls"]))
        return self.service.review(
            write_context_ref=self.onboarding.write_context_ref,
            expected_planning_version=manual["planning_row_version"],
            candidate_id=candidate["candidate_id"],
            expected_candidate_version=candidate["candidate_version"],
            expected_candidate_hash=candidate["candidate_hash"],
            expected_base_version=candidate["base_version"],
            decision="accept",
            correctness_assessment="候选完整、来源明确，符合我的计划。",
            calm_check=calm(),
            reason="我在新的真实唤醒中接受。",
            ai_confirmation=True,
        )

    def test_locked_and_invalid_context_writes_are_rejected(self) -> None:
        self.onboarding.allowed = False
        locked = self.service.remember(
            write_context_ref=self.onboarding.write_context_ref,
            expected_planning_version=0,
            content=plan_content(),
            reason="保存。",
            calm_check=calm(),
            ai_confirmation=True,
            idempotency_key="locked",
        )
        self.assertEqual("reject", locked["decision"])
        self.assertEqual(["module_one_required"], locked["reason_codes"])
        self.assertEqual(0, self.service.status()["counts"]["pending_changes"])

        self.onboarding.allowed = True
        invalid = self.service.remember(
            write_context_ref="stale_ref",
            expected_planning_version=0,
            content=plan_content(),
            reason="保存。",
            calm_check=calm(),
            ai_confirmation=True,
            idempotency_key="invalid-context",
        )
        self.assertEqual(["brain_open_required"], invalid["reason_codes"])
        self.assertEqual(PLANNING_MODULE, self.onboarding.last_module)

    def test_public_create_is_direct_and_manual_keeps_legacy_review_optional(self) -> None:
        stored = self.remember()
        self.assertEqual("stored", stored["decision"])
        self.assertTrue(stored["active_plan_changed"])
        self.assertFalse(stored["candidate_created"])
        self.assertFalse(stored["review_performed"])
        self.assertEqual(0, self.service.status()["counts"]["pending_changes"])
        pending = self.store.propose_create(
            owner_id="owner-a", model_id="model-a", wake_id="wake-1", wake_seq=1,
            expected_row_version=self.service.status()["row_version"],
            content=plan_content("旧候选"), reason="合成旧数据", calm_check=calm(),
            ai_confirmation=True, idempotency_key="legacy-fixture",
        )
        same_wake = self.service.manual(write_context_ref=self.onboarding.write_context_ref)
        call = same_wake["current_action_contract"]["allowed_calls"][0]
        self.assertNotIn("calm_check", call["required_arguments"])
        self.assertNotIn("correctness_assessment", call["required_arguments"])
        self.assertNotIn("write_context_ref", call["required_arguments"])
        self.assertNotIn("expected_planning_version", call["required_arguments"])
        self.assertEqual(["write_context_ref", "expected_planning_version"], call["optional_host_arguments"])
        self.assertEqual(call["optional_host_arguments"], call["explicit_direct_required_arguments"])
        self.assertIn("expected_candidate_hash", call["required_arguments"])
        self.assertIn("expected_base_version", call["required_arguments"])
        self.assertIn("ai_confirmation", call["required_arguments"])
        self.assertEqual([], same_wake["current_action_contract"]["blocked_candidates"])
        self.assertEqual(1, self.service.status()["counts"]["pending_changes"])
        accepted = self.activate_pending(pending)
        self.assertEqual("candidate_accepted", accepted["decision"])
        self.assertEqual("accepted", accepted["candidate_lifecycle"])

    def test_advanced_facade_all_kinds_are_standalone_and_manual_says_parent_optional(self) -> None:
        manual = self.service.manual(write_context_ref=self.onboarding.write_context_ref)
        hierarchy = manual["creation_field_rules"]["hierarchy"]
        self.assertIn("都可独立创建", hierarchy)
        self.assertIn("parent_ref 可省略或填 null", hierarchy)
        self.assertNotIn("必须有", hierarchy)
        for kind in ("vision", "goal", "milestone", "task", "commitment"):
            with self.subTest(kind=kind):
                fields = {**plan_content("独立-" + kind), "kind": kind}
                # The flat MCP adapter supplies its optional parent_ref=None
                # in the normalized content object consumed by the facade.
                stored = self.service.remember(
                    write_context_ref=self.onboarding.write_context_ref,
                    expected_planning_version=self.service.status()["row_version"],
                    content=fields, reason="我选择独立保存这份计划。",
                    idempotency_key="standalone-" + kind,
                )
                self.assertEqual("stored", stored["decision"], stored)
                readback = self.service.recall(plan_ref=stored["plan_ref"], include_history=True)["plans"][0]
                self.assertEqual(kind, readback["content"]["kind"])
                self.assertIsNone(readback["content"]["parent_ref"])
                revised = self.service.revise(
                    write_context_ref=self.onboarding.write_context_ref,
                    expected_planning_version=self.service.status()["row_version"],
                    plan_id=stored["plan_id"], expected_plan_version=1,
                    intent="revise", changes={"summary": "修改独立计划-" + kind},
                    reason="我选择修改独立计划。", idempotency_key="revise-standalone-" + kind,
                )
                self.assertEqual("revised", revised["decision"])
                self.assertEqual(2, revised["plan_version"])
        self.assertEqual(0, self.service.status()["counts"]["pending_changes"])

    def test_recall_event_and_injection_are_safe_facade_operations(self) -> None:
        accepted = self.remember()
        recalled = self.service.recall(plan_ref=accepted["plan_ref"], include_history=True)
        self.assertEqual("recalled", recalled["decision"])
        self.assertFalse(recalled["state_changed"])
        plan_id = str(accepted["plan_ref"]).split("//", 1)[1].split("@", 1)[0]

        missing = self.service.record_event(
            write_context_ref=self.onboarding.write_context_ref,
            expected_planning_version=self.service.status()["row_version"],
            plan_id=plan_id,
            expected_plan_version=1,
            event_type="complete",
            reason="完成。",
            evidence=[],
            ai_confirmation=True,
            idempotency_key="missing-evidence",
        )
        self.assertEqual(["evidence_required"], missing["reason_codes"])

        recorded = self.service.record_event(
            write_context_ref=self.onboarding.write_context_ref,
            expected_planning_version=self.service.status()["row_version"],
            plan_id=plan_id,
            expected_plan_version=1,
            event_type="complete",
            reason="完成且已经核验。",
            evidence=[
                {
                    "source_kind": "current_conversation",
                    "source_ref": "wake://2/result",
                    "evidence_summary": "本轮用户实际测试通过。",
                    "provenance": "verified",
                }
            ],
            ai_confirmation=True,
            idempotency_key="complete-1",
        )
        self.assertEqual("event_recorded", recorded["decision"])
        self.assertEqual("completed", recorded["plan_state"]["state"])

        before = self.service.status()["row_version"]
        injection = self.service.build_injection(query="明日测试", session_start=True)
        self.assertLessEqual(len(injection["envelopes"]), 3)
        self.assertEqual(before, self.service.status()["row_version"])


if __name__ == "__main__":
    unittest.main()
