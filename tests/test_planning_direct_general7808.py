"""Direct advanced planning and legacy-candidate regressions, synthetic DB only.

The borrowed setup denies network access and every SQLite path except its own
temporary database. No production service, account, model or memory is used.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp_server.planning_service import PlanningMemoryAccessService
from runtime.execution_binding import ExecutionBindingError
from runtime.planning_memory import PlanningMemoryError
from tests import test_planning_memory as fixtures
from tests import test_planning_persistent_r4h34 as isolated


OWNER, MODEL = fixtures.OWNER, fixtures.MODEL


class DirectPlanningTests(unittest.TestCase):
    setUp = isolated.PlanningPersistentCandidateTests.setUp
    key = fixtures.PlanningMemoryStoreTests.key

    def version(self):
        return self.store.status(owner_id=OWNER, model_id=MODEL)["row_version"]

    def snapshot(self):
        with self.store._connect() as connection:
            return tuple(connection.iterdump())

    def rows(self, table):
        self.assertIn(table, {"planning_versions", "planning_events", "planning_audit_events", "planning_change_candidates"})
        with self.store._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)]

    def read(self, initial):
        return self.store.recall(
            owner_id=OWNER, model_id=MODEL, plan_ref=initial["plan_ref"],
            include_history=True, include_terminal=True,
        )["plans"][0]

    def create_args(self, **overrides):
        return {
            "owner_id": OWNER, "model_id": MODEL, "wake_id": "wake-direct", "wake_seq": 10,
            "expected_row_version": self.version(), "content": fixtures.content("合成最终计划"),
            "reason": "本次决定提交的最终内容。", "idempotency_key": self.key("direct"),
            **overrides,
        }

    def create(self, **overrides):
        return self.store.remember_direct(**self.create_args(**overrides))

    def revise_args(self, initial, **overrides):
        return {
            "owner_id": OWNER, "model_id": MODEL, "wake_id": "wake-direct", "wake_seq": 10,
            "expected_row_version": self.version(), "plan_id": initial["plan_id"],
            "expected_plan_version": initial["plan_version"],
            "expected_event_seq": self.read(initial)["event_seq"],
            "intent": "revise", "changes": {"original_text": "我本次明确修改完整正文。"},
            "reason": "本次明确修改。", "idempotency_key": self.key("revise"),
            **overrides,
        }

    def revise(self, initial, **overrides):
        return self.store.revise_direct(**self.revise_args(initial, **overrides))

    def rejected(self, code, operation):
        before = self.snapshot()
        with self.assertRaisesRegex((PlanningMemoryError, ExecutionBindingError), code):
            operation()
        self.assertEqual(before, self.snapshot())

    def pending(self, initial=None):
        args = {
            "owner_id": OWNER, "model_id": MODEL, "wake_id": "wake-direct", "wake_seq": 10,
            "expected_row_version": self.version(), "reason": "合成升级前候选。",
            "calm_check": fixtures.calm(), "ai_confirmation": True,
            "idempotency_key": self.key("legacy"),
        }
        if initial is None:
            return self.store.propose_create(**args, content=fixtures.content("合成旧候选"))
        return self.store.propose_revision(
            **args, plan_id=initial["plan_id"], expected_plan_version=initial["plan_version"],
            intent="revise", changes={"original_text": "我此前提交的候选正文。"},
        )

    def review(self, candidate, **overrides):
        return self.store.review_change(**{
            "owner_id": OWNER, "model_id": MODEL, "wake_id": "wake-direct", "wake_seq": 10,
            "expected_row_version": self.version(), "candidate_id": candidate["candidate_id"],
            "expected_candidate_hash": candidate["candidate_hash"],
            "expected_candidate_version": candidate["candidate_version"],
            "expected_base_version": candidate["base_version"], "decision": "accept",
            "ai_confirmation": True, "reason": "本次明确确认该精确候选内容。",
            **overrides,
        })

    def test_create_and_advanced_body_revision_commit_in_same_wake_without_calm(self):
        first = self.create()
        second = self.revise(first, changes={
            "original_text": "我本次提交的完整新正文。", "reminder": "我选择的新提醒。",
        })
        self.assertEqual("stored", first["decision"])
        self.assertEqual("revised", second["decision"])
        self.assertEqual(2, second["plan_version"])
        self.assertEqual(first["plan_ref"], second["rollback_ref"])
        self.assertEqual("我本次提交的完整新正文。", self.read(second)["content"]["original_text"])
        self.assertNotEqual(self.read(first)["content"]["original_text"], self.read(second)["content"]["original_text"])
        self.assertEqual([], self.rows("planning_change_candidates"))
        for result in (first, second):
            self.assertFalse(result["candidate_created"])
            self.assertFalse(result["review_performed"])
            self.assertFalse(result["review_requires_later_wake"])
        self.assertEqual({"direct_create", "direct_revise"}, {row["action"] for row in self.rows("planning_audit_events")})
        self.assertEqual({"wake-direct"}, {row["wake_id"] for row in self.rows("planning_events")})

    def test_legacy_calm_fields_are_not_mandatory_true_attestations(self):
        created = self.create(calm_check={"dependencies_checked": False, "notes": "可选说明"})
        self.assertFalse(created["review_performed"])
        self.rejected("ai_confirmation_required", lambda: self.create(ai_confirmation=False))
        self.rejected("credential_content_rejected", lambda: self.create(calm_check={"notes": "token=syntheticcredential0123456789"}))

    def test_create_and_revision_retries_do_not_duplicate_any_record(self):
        args = self.create_args()
        created = self.store.remember_direct(**args)
        before = self.snapshot()
        replay = self.store.remember_direct(**args)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(created["plan_ref"], replay["plan_ref"])
        self.assertFalse(replay["state_changed"])
        self.assertTrue(replay["idempotent_replay"])
        self.rejected("idempotency_key_reused", lambda: self.store.remember_direct(**{**args, "content": fixtures.content("另一个提交")}))
        revision_args = self.revise_args(created)
        revised = self.store.revise_direct(**revision_args)
        before = self.snapshot()
        repeated_revision = self.store.revise_direct(**revision_args)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(revised["plan_ref"], repeated_revision["plan_ref"])
        self.assertFalse(repeated_revision["state_changed"])
        self.assertTrue(repeated_revision["idempotent_replay"])
        self.rejected("plan_version_conflict", lambda: self.revise(created))
        self.assertEqual(2, revised["plan_version"])

    def test_stale_module_version_rolls_back_every_write(self):
        first = self.create()
        self.rejected("planning_row_version_conflict", lambda: self.create(expected_row_version=0))
        self.rejected("planning_row_version_conflict", lambda: self.revise(first, expected_row_version=0))
        self.rejected("invalid_expected_planning_version", lambda: self.create(expected_row_version=True))

    def test_plan_and_event_coordinates_are_independent_and_exact(self):
        first = self.create()
        old_seq = first["event_seq"]
        self.store.record_ordinary_event(
            owner_id=OWNER, model_id=MODEL, wake_id="wake-direct", wake_seq=10,
            expected_row_version=self.version(), plan_id=first["plan_id"], expected_plan_version=1,
            expected_event_seq=old_seq, event_type="pause", reason="我选择暂停。",
        )
        self.rejected("plan_event_seq_conflict", lambda: self.revise(first, expected_event_seq=old_seq))
        self.rejected("invalid_expected_event_seq", lambda: self.revise(first, expected_event_seq=True))
        revised = self.revise(first)
        self.assertEqual("paused", revised["plan_state"]["state"])

    def test_foreign_owner_model_and_execution_wake_cannot_modify(self):
        first = self.create()
        self.rejected("plan_not_found", lambda: self.revise(first, owner_id="foreign-owner"))
        self.rejected("plan_not_found", lambda: self.revise(first, model_id="foreign-model"))
        claim = SimpleNamespace(owner_id=OWNER, model_id=MODEL, wake_id="other-window-wake")
        with patch("runtime.execution_binding.current_execution_claim", return_value=claim):
            self.rejected("execution_wake_mismatch", lambda: self.create())
            self.rejected("execution_wake_mismatch", lambda: self.revise(first))

    def test_cancelled_claim_is_rechecked_inside_transaction(self):
        first = self.create()
        with patch("runtime.execution_binding.assert_bound_execution", side_effect=ExecutionBindingError("execution_claim_not_current")):
            self.rejected("execution_claim_not_current", lambda: self.create())
            self.rejected("execution_claim_not_current", lambda: self.revise(first))

    def test_quarantine_cannot_be_edited_rolled_back_or_revived(self):
        first = self.create()
        second = self.revise(first)
        with self.store._connect() as connection:
            connection.execute("UPDATE planning_items SET recall_lifecycle='quarantined' WHERE plan_id=?", (first["plan_id"],))
        # Explicit coordinates avoid using normal recall to read an isolated item.
        for fields in (
            {"intent": "revise", "changes": {"summary": "不能越过隔离"}},
            {"intent": "rollback", "changes": None, "rollback_to_version": 1},
            {"intent": "revive", "changes": None},
        ):
            self.rejected("plan_quarantined", lambda fields=fields: self.store.revise_direct(
                owner_id=OWNER, model_id=MODEL, wake_id="wake-direct", wake_seq=10,
                expected_row_version=self.version(), plan_id=second["plan_id"], expected_plan_version=2,
                reason="合成隔离负向测试", idempotency_key=self.key("isolated"), **fields,
            ))

    def test_graph_references_hierarchy_and_cycles_are_still_validated(self):
        vision = self.create(content=fixtures.content("合成愿景", kind="vision"))
        goal = self.create(content=fixtures.content("合成目标", kind="goal", parent_ref=vision["plan_ref"]))
        self.rejected("planning_graph_cycle", lambda: self.revise(vision, changes={"dependency_refs": [goal["plan_ref"]]}))
        self.rejected("invalid_plan_hierarchy", lambda: self.create(content=fixtures.content(
            "父项种类不匹配", kind="milestone", parent_ref=vision["plan_ref"])))
        updated = self.revise(vision)
        self.rejected("stale_plan_ref", lambda: self.revise(goal, changes={"summary": "父项已过期"}))
        self.revise(goal, changes={"parent_ref": updated["plan_ref"]})

    def test_all_advanced_kinds_can_be_created_and_revised_without_parent(self):
        for kind in ("vision", "goal", "milestone", "task", "commitment"):
            with self.subTest(kind=kind):
                created = self.create(content=fixtures.content("独立计划-" + kind, kind=kind))
                self.assertEqual("stored", created["decision"])
                self.assertIsNone(self.read(created)["content"]["parent_ref"])
                revised = self.revise(created, changes={"original_text": "我更新这份独立计划。"})
                self.assertEqual("revised", revised["decision"])
                self.assertIsNone(self.read(revised)["content"]["parent_ref"])
                self.assertEqual(created["plan_ref"], revised["rollback_ref"])
        self.assertEqual([], self.rows("planning_change_candidates"))

    def test_advanced_parent_can_be_detached_and_reattached_without_losing_history(self):
        vision = self.create(content=fixtures.content("愿景", kind="vision"))
        goal = self.create(content=fixtures.content("目标", kind="goal", parent_ref=vision["plan_ref"]))
        detached = self.revise(goal, changes={"parent_ref": None})
        self.assertIsNone(self.read(detached)["content"]["parent_ref"])
        self.assertEqual(vision["plan_ref"], self.read(goal)["content"]["parent_ref"])
        attached = self.revise(detached, changes={"parent_ref": vision["plan_ref"]})
        self.assertEqual(vision["plan_ref"], self.read(attached)["content"]["parent_ref"])
        self.rejected("invalid_plan_hierarchy", lambda: self.revise(
            vision, changes={"parent_ref": attached["plan_ref"]}))
        self.rejected("plan_not_found", lambda: self.create(content=fixtures.content(
            "未知父项", kind="goal", parent_ref="plan://plan_" + "f" * 32 + "@1")))

    def test_abandon_revive_archive_and_rollback_have_real_state_constraints(self):
        first = self.create()
        second = self.revise(first)
        rolled = self.revise(second, intent="rollback", changes=None, rollback_to_version=1)
        self.assertEqual(3, rolled["plan_version"])
        self.assertEqual(self.read(first)["content"], self.read(rolled)["content"])
        self.rejected("invalid_plan_state_transition", lambda: self.revise(rolled, intent="archive", changes=None))
        abandoned = self.revise(rolled, intent="abandon", changes=None)
        self.assertEqual("abandoned", abandoned["plan_state"]["state"])
        revived = self.revise(abandoned, intent="revive", changes=None)
        self.assertEqual("active", revived["plan_state"]["state"])
        abandoned = self.revise(revived, intent="abandon", changes=None)
        archived = self.revise(abandoned, intent="archive", changes=None)
        self.rejected("invalid_plan_state_transition", lambda: self.revise(archived))
        self.rejected("invalid_plan_state_transition", lambda: self.revise(archived, intent="revive", changes=None))

    def test_revive_cannot_overfill_persistent_slots(self):
        first = self.create(content=fixtures.content("常驻甲", presence_mode="persistent"))
        abandoned = self.revise(first, intent="abandon", changes=None)
        self.create(content=fixtures.content("常驻乙", presence_mode="persistent"))
        self.create(content=fixtures.content("常驻丙", presence_mode="persistent"))
        self.rejected("persistent_plan_limit", lambda: self.revise(abandoned, intent="revive", changes=None))

    def test_completed_state_needs_evidence_and_cannot_be_faked_as_edit(self):
        first = self.create()
        self.rejected("invalid_plan_changes", lambda: self.revise(first, changes={"state": "completed"}))
        args = dict(owner_id=OWNER, model_id=MODEL, wake_id="wake-direct", wake_seq=10,
                    expected_row_version=self.version(), plan_id=first["plan_id"], expected_plan_version=1,
                    expected_event_seq=first["event_seq"], event_type="complete", reason="真实依据仍必需。")
        self.rejected("evidence_required", lambda: self.store.record_ordinary_event(**args))
        self.store.record_ordinary_event(**args, evidence=fixtures.evidence())
        self.rejected("invalid_plan_state_transition", lambda: self.revise(first))

    def test_legacy_candidates_are_not_auto_activated_by_new_submissions(self):
        candidate = self.pending()
        before = self.rows("planning_change_candidates")
        self.create()
        self.assertEqual(before, self.rows("planning_change_candidates"))
        self.assertEqual(1, self.store.status(owner_id=OWNER, model_id=MODEL)["counts"]["pending_changes"])
        accepted = self.review(candidate)
        self.assertEqual("candidate_accepted", accepted["decision"])
        self.assertNotIn("later_wake_review_complete", json.dumps(self.rows("planning_audit_events")))

    def test_legacy_accept_without_presentation_requires_confirmation_and_exact_hash(self):
        candidate = self.pending()
        for fields, code in (
            ({"ai_confirmation": False}, "ai_confirmation_required"),
            ({"expected_candidate_hash": "0" * 64}, "candidate_hash_mismatch"),
            ({"expected_candidate_version": True}, "candidate_version_mismatch"),
            ({"expected_base_version": 1}, "candidate_base_mismatch"),
            ({"owner_id": "other-owner"}, "candidate_not_found"),
        ):
            self.rejected(code, lambda fields=fields: self.review(candidate, **fields))
        accepted = self.review(candidate)
        self.assertEqual("candidate_accepted", accepted["decision"])
        self.rejected("candidate_not_pending", lambda: self.review(candidate))

    def test_legacy_candidate_body_cannot_change_behind_an_old_hash(self):
        candidate = self.pending()
        with self.store._connect() as connection:
            connection.execute("UPDATE planning_change_candidates SET proposed_content_json=? WHERE candidate_id=?",
                               (json.dumps(fixtures.content("被替换的正文")), candidate["candidate_id"]))
        self.rejected("candidate_hash_mismatch", lambda: self.review(candidate))

    def test_old_candidate_cannot_overwrite_a_newer_direct_version(self):
        first = self.create()
        candidate = self.pending(first)
        self.revise(first)
        self.rejected("candidate_base_changed", lambda: self.review(candidate))
        rejected = self.review(candidate, decision="reject")
        self.assertEqual("candidate_rejected", rejected["decision"])
        self.assertFalse(rejected["active_plan_changed"])

    def test_legacy_candidate_cannot_recover_quarantined_plan(self):
        first = self.create()
        candidate = self.pending(first)
        with self.store._connect() as connection:
            connection.execute("UPDATE planning_items SET recall_lifecycle='quarantined' WHERE plan_id=?", (first["plan_id"],))
        self.rejected("plan_quarantined", lambda: self.review(candidate))
        self.assertEqual("candidate_rejected", self.review(candidate, decision="reject")["decision"])

    def test_facade_uses_direct_submission_and_still_checks_scope(self):
        class Binding:
            allowed = True

            def authorize_other_module_write(self, **fields):
                return {"decision": "allowed"}

            def current_open_write_context(self, **fields):
                return {"write_context_available": self.allowed and fields["required_scope"] == "planning_memory",
                        "wake_id": "wake-direct", "wake_seq": 10}

            def contains_protected_persistence_value(self, **fields):
                return False

        onboarding = Binding()
        service = PlanningMemoryAccessService(self.store, onboarding=onboarding, owner_id=OWNER, model_id=MODEL)
        args = self.create_args()
        fields = {key: value for key, value in args.items() if key not in {"owner_id", "model_id", "wake_id", "wake_seq", "expected_row_version"}}
        with patch.object(self.store, "propose_create", side_effect=AssertionError("must_not_stage")), patch.object(self.store, "review_change", side_effect=AssertionError("must_not_auto_review")):
            stored = service.remember(write_context_ref="synthetic-context-not-a-credential", expected_planning_version=0, **fields)
        self.assertEqual("stored", stored["decision"])
        before = self.snapshot()
        with patch("runtime.execution_binding.assert_bound_execution", side_effect=ExecutionBindingError("execution_claim_not_current")):
            revoked = service.revise(
                write_context_ref="synthetic-context-not-a-credential", expected_planning_version=self.version(),
                plan_id=stored["plan_id"], expected_plan_version=1, intent="revise",
                changes={"summary": "该 claim 已被撤销"}, reason="合成测试", idempotency_key=self.key("revoked"),
            )
        self.assertEqual(["execution_claim_not_current"], revoked["reason_codes"])
        self.assertFalse(revoked["state_changed"])
        self.assertEqual(before, self.snapshot())
        onboarding.allowed = False
        before = self.snapshot()
        denied = service.revise(
            write_context_ref="synthetic-context-not-a-credential", expected_planning_version=self.version(),
            plan_id=stored["plan_id"], expected_plan_version=1, intent="revise",
            changes={"summary": "拒绝跨范围写入"}, reason="合成测试", idempotency_key=self.key("denied"),
        )
        self.assertEqual(["brain_open_required"], denied["reason_codes"])
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
