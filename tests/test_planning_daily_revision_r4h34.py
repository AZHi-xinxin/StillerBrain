"""Offline ordinary metadata/event revisions against synthetic temporary stores."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runtime.execution_binding import ExecutionBindingError
from runtime.planning_memory import PlanningMemoryError
from tests import test_planning_memory as fixtures
from tests import test_planning_persistent_r4h34 as persistent_fixtures


class PlanningDailyRevisionTests(unittest.TestCase):
    # Reuse only fixture helpers, not the other suite's test methods. Its setup
    # denies network and any sqlite connection outside this test's one temp DB.
    setUp = persistent_fixtures.PlanningPersistentCandidateTests.setUp
    key = fixtures.PlanningMemoryStoreTests.key
    propose = fixtures.PlanningMemoryStoreTests.propose
    accept = fixtures.PlanningMemoryStoreTests.accept
    create = fixtures.PlanningMemoryStoreTests.create
    record = fixtures.PlanningMemoryStoreTests.record
    retire = persistent_fixtures.PlanningPersistentCandidateTests.retire
    plan_id = staticmethod(persistent_fixtures.PlanningPersistentCandidateTests.plan_id)

    def rows(self, table: str) -> list[dict[str, object]]:
        self.assertIn(table, {
            "planning_items", "planning_versions", "planning_events", "planning_edges",
            "planning_change_candidates", "planning_module_state", "planning_audit_events",
        })
        with self.store._connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)]

    def snapshot(self) -> tuple[str, ...]:
        with self.store._connect() as connection:
            return tuple(connection.iterdump())

    def row_version(self) -> int:
        with self.store._connect() as connection:
            return self.store._state(connection, fixtures.OWNER, fixtures.MODEL)["row_version"]

    def ordinary(self, **overrides: object) -> dict[str, object]:
        self.wake_seq += 1
        arguments = dict(
            owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
            wake_id=f"wake-{self.wake_seq}", wake_seq=self.wake_seq,
            expected_row_version=self.row_version(),
            write_context_ref="artifact_synthetic_context_not_authentication",
            content="  Synthetic original 🌱\nwith retained spacing.  ",
            idempotency_key=self.key("ordinary"),
        )
        arguments.update(overrides)
        return self.store.remember_ordinary(**arguments)

    def read(self, ref: str) -> dict[str, object]:
        return self.store.recall(
            owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
            plan_ref=ref, include_history=True, include_terminal=True,
        )["plans"][0]

    def revise(self, initial: dict[str, object], **overrides: object) -> dict[str, object]:
        arguments = dict(
            owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
            wake_id="wake-daily-revision", wake_seq=100,
            expected_row_version=self.row_version(), plan_id=self.plan_id(initial),
            expected_plan_version=int(str(initial["plan_ref"]).rsplit("@", 1)[1]),
            changes={"summary": "Synthetic revised summary"}, reason="A synthetic display edit",
        )
        arguments.update(overrides)
        return self.store.revise_ordinary(**arguments)

    def advance(self, initial: dict[str, object], **overrides: object) -> dict[str, object]:
        # Test helper models an explicit caller read. Individual stale-state
        # tests override these coordinates with the prior observed values.
        observed = self.read(str(initial["plan_ref"]))
        arguments = dict(
            owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
            wake_id="wake-daily-event", wake_seq=101,
            expected_row_version=self.row_version(), plan_id=self.plan_id(initial),
            expected_plan_version=int(str(initial["plan_ref"]).rsplit("@", 1)[1]),
            expected_event_seq=observed["event_seq"], event_type="pause",
            reason="Synthetic event note", evidence=[],
        )
        arguments.update(overrides)
        return self.store.record_ordinary_event(**arguments)

    def assert_rejected_unchanged(self, code: str, operation) -> None:
        before = self.snapshot()
        with self.assertRaisesRegex((PlanningMemoryError, ExecutionBindingError), code):
            operation()
        self.assertEqual(before, self.snapshot())

    def pending_revision(self, initial: dict[str, object]) -> dict[str, object]:
        self.wake_seq += 1
        return self.store.propose_revision(
            owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
            wake_id=f"wake-{self.wake_seq}", wake_seq=self.wake_seq,
            expected_row_version=self.row_version(), plan_id=self.plan_id(initial),
            expected_plan_version=1, intent="revise",
            changes={"summary": "Synthetic advanced candidate"}, reason="Synthetic later review",
            calm_check=fixtures.calm(), ai_confirmation=True,
            idempotency_key=self.key("pending"),
        )

    def test_revision_versions_four_fields_and_preserves_every_other_value(self) -> None:
        initial = self.ordinary()
        before = self.read(str(initial["plan_ref"]))
        old_versions = self.rows("planning_versions")
        changes = {"title": "New title", "summary": "New summary",
                   "keywords": ["orchid", "watering"], "importance": 81}
        result = self.revise(initial, changes=changes)
        after = self.read(result["ref"])
        self.assertEqual({**before["content"], **changes}, after["content"])
        self.assertEqual(old_versions, self.rows("planning_versions")[:1])
        self.assertEqual(before["content"], self.read(str(initial["plan_ref"]))["content"])
        encoded = json.dumps(after["content"], ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), after["content_hash"])
        self.assertEqual("revised", result["decision"])
        self.assertEqual(2, result["version"])
        self.assertEqual(1, result["previous_version"])
        self.assertIs(result["state_changed"], True)
        self.assertEqual(["created", "revised"], [row["event_type"] for row in after["events"]])
        self.assertEqual([], after["events"][-1]["evidence"])
        self.assertEqual([], self.rows("planning_change_candidates"))
        self.assertNotIn("Synthetic original", json.dumps(result))
        self.assertNotIn("New summary", json.dumps(result))

    def test_advanced_adoption_presence_and_graph_are_not_rewritten(self) -> None:
        parent = self.create(fixtures.content("Synthetic parent", kind="vision"))
        initial = self.create(fixtures.content(
            "Synthetic child", kind="goal", parent_ref=str(parent["plan_ref"]),
            dependency_refs=[str(parent["plan_ref"])], presence_mode="persistent",
        ))
        before = self.read(str(initial["plan_ref"]))["content"]
        old_edges = self.rows("planning_edges")
        result = self.revise(initial, changes={"importance": 92})
        after = self.read(result["ref"])
        self.assertEqual({**before, "importance": 92}, after["content"])
        active_edges = [row for row in self.rows("planning_edges") if row["active"]]
        self.assertEqual({(row["target_plan_id"], row["edge_type"]) for row in old_edges},
                         {(row["target_plan_id"], row["edge_type"]) for row in active_edges})
        self.assertTrue(all(row["source_version"] == 2 for row in active_edges))
        self.assertEqual(4, len(self.rows("planning_edges")))

    def test_system_fields_and_invalid_authored_shapes_are_rejected(self) -> None:
        initial = self.ordinary()
        for field in ("write_mode", "state", "recall_lifecycle", "unknown", "owner_id"):
            with self.subTest(field=field):
                self.assert_rejected_unchanged("invalid_plan_changes",
                    lambda: self.revise(initial, changes={"summary": "Allowed part", field: None}))
        for field, invalid in (("original_text", None), ("reminder", None), ("presence_mode", "invalid"),
                               ("track", "invalid"), ("kind", "invalid"), ("scene_tags", "bad"),
                               ("parent_ref", "bad"), ("dependency_refs", "bad"), ("timezone", None),
                               ("allow_coordination_hint", None)):
            with self.subTest(field=field):
                self.assert_rejected_unchanged("invalid",
                    lambda: self.revise(initial, changes={field: invalid}))
        revised = self.revise(initial, changes={"original_text": "New body", "parent_ref": None,
                                                "dependency_refs": [], "start_at": None, "due_at": None})
        self.assertEqual(2, revised["version"])
        self.assertEqual("New body", self.read(revised["ref"])["content"]["original_text"])

    def test_invalid_metadata_noop_and_secrets_reject_without_partial_writes(self) -> None:
        initial = self.ordinary(title="Same title")
        cases = [({}, "changes_required"), ({"title": " "}, "invalid_title"),
                 ({"title": "x" * 121}, "title_too_long"),
                 ({"summary": "x" * 201}, "summary_too_long"),
                 ({"keywords": "x"}, "invalid_keywords"),
                 ({"keywords": ["x"] * 17}, "invalid_keywords"),
                 ({"keywords": ["x" * 161]}, "keywords_too_long"),
                 ({"importance": True}, "invalid_importance"),
                 ({"importance": 101}, "invalid_importance"),
                 ({"importance": -1}, "invalid_importance"),
                 ({"title": "Same title"}, "no_effective_change"),
                 ({"summary": "password: synthetic-not-real"}, "credential_content_rejected")]
        for changes, code in cases:
            with self.subTest(code=code):
                self.assert_rejected_unchanged(code, lambda: self.revise(initial, changes=changes))
        self.assert_rejected_unchanged("credential_content_rejected",
            lambda: self.revise(initial, reason="token=synthetic-not-a-real-token"))

    def test_item_cas_and_module_cas_are_independent_and_stale_retry_does_not_duplicate(self) -> None:
        initial = self.ordinary()
        self.assert_rejected_unchanged("planning_row_version_conflict",
            lambda: self.revise(initial, expected_row_version=0))
        self.revise(initial)
        self.assert_rejected_unchanged("plan_version_conflict", lambda: self.revise(initial))
        for invalid in (True, 0, -1, 1.0):
            with self.subTest(invalid_version=invalid):
                self.assert_rejected_unchanged("plan_version_conflict",
                    lambda: self.revise(initial, expected_plan_version=invalid))

    def test_paused_metadata_edit_does_not_resume_plan(self) -> None:
        initial = self.ordinary()
        self.advance(initial)
        result = self.revise(initial)
        self.assertEqual("paused", self.read(result["ref"])["state_projection"]["state"])

    def test_terminal_and_quarantined_metadata_cannot_be_restored(self) -> None:
        for state in ("completed", "abandoned", "archived", "quarantined"):
            initial = self.ordinary()
            if state == "completed":
                self.record(initial, "complete", event_evidence=fixtures.evidence())
            elif state == "abandoned":
                self.retire(initial, "abandon")
            elif state == "archived":
                self.record(initial, "pause")
                self.retire(initial, "archive")
            else:
                with self.store._connect() as connection:
                    connection.execute("UPDATE planning_items SET recall_lifecycle='quarantined' WHERE plan_id=?",
                                       (self.plan_id(initial),))
            with self.subTest(state=state):
                code = "plan_quarantined" if state == "quarantined" else "invalid_plan_state_transition"
                self.assert_rejected_unchanged(code, lambda: self.revise(initial))

    def test_old_pending_candidate_is_preserved_but_cannot_overwrite_new_version(self) -> None:
        initial = self.ordinary()
        pending = self.pending_revision(initial)
        candidate_before = self.rows("planning_change_candidates")
        revised = self.revise(initial)
        self.assertEqual(candidate_before, self.rows("planning_change_candidates"))
        self.wake_seq += 1
        wake_id = f"wake-{self.wake_seq}"
        presented = self.store.present_pending_candidates(
            owner_id=fixtures.OWNER, model_id=fixtures.MODEL, wake_id=wake_id, wake_seq=self.wake_seq,
        )[0]
        self.assertEqual(pending["candidate_id"], presented["candidate_id"])
        self.assert_rejected_unchanged("candidate_base_changed", lambda: self.store.review_change(
            owner_id=fixtures.OWNER, model_id=fixtures.MODEL, wake_id=wake_id, wake_seq=self.wake_seq,
            expected_row_version=self.row_version(), candidate_id=presented["candidate_id"],
            expected_candidate_version=presented["candidate_version"],
            expected_candidate_hash=presented["candidate_hash"], expected_base_version=1,
            decision="accept", correctness_assessment="Synthetic review", calm_check=fixtures.calm(),
            reason="Synthetic review", ai_confirmation=True,
        ))
        self.assertEqual(2, self.read(revised["ref"])["current_version"])

    def test_both_routes_reject_cross_owner_and_model_without_initialization(self) -> None:
        initial = self.ordinary()
        for option in ({"owner_id": "other-owner"}, {"model_id": "other-model"}):
            for operation in (self.revise, self.advance):
                with self.subTest(boundary=next(iter(option)), operation=operation.__name__):
                    self.assert_rejected_unchanged("plan_not_found", lambda: operation(initial, **option))

    def test_both_routes_recheck_bound_claim_before_runtime_writes(self) -> None:
        initial = self.ordinary()
        wrong_owner = SimpleNamespace(owner_id="other", model_id=fixtures.MODEL, wake_id="wake-daily-event")
        with patch("runtime.execution_binding.current_execution_claim", return_value=wrong_owner):
            for operation in (self.revise, self.advance):
                self.assert_rejected_unchanged("execution_owner_mismatch", lambda: operation(initial))
        for operation, wake_id in ((self.revise, "wake-daily-revision"), (self.advance, "wake-daily-event")):
            claim = SimpleNamespace(owner_id=fixtures.OWNER, model_id=fixtures.MODEL, wake_id=wake_id)
            with patch("runtime.execution_binding.current_execution_claim", return_value=claim):
                self.assert_rejected_unchanged("execution_registry_unavailable", lambda: operation(initial))

    def test_late_audit_failure_rolls_back_entire_revision_and_event(self) -> None:
        initial = self.ordinary()
        for operation in (self.revise, self.advance):
            before = self.snapshot()
            with patch.object(self.store, "_audit", side_effect=RuntimeError("synthetic_audit_failure")):
                with self.assertRaisesRegex(RuntimeError, "synthetic_audit_failure"):
                    operation(initial)
            self.assertEqual(before, self.snapshot())

    def test_progress_complete_and_reopen_keep_exact_caller_evidence(self) -> None:
        initial = self.ordinary()
        supplied = [{"source_kind": "human_report", "source_ref": "document://synthetic-event",
                     "evidence_summary": "Synthetic report, not host verification", "provenance": "reported"}]
        initial_version = self.rows("planning_versions")
        sequence = self.read(str(initial["plan_ref"]))["event_seq"]
        for event_type, state in (("progress", "active"), ("complete", "completed"), ("reopen", "active")):
            result = self.advance(initial, event_type=event_type, evidence=supplied)
            self.assertEqual("event_recorded", result["decision"])
            self.assertEqual(state, result["state"])
            self.assertEqual(1, result["version"])
            self.assertGreater(result["event_seq"], sequence)
            self.assertEqual(sequence, result["previous_event_seq"])
            sequence = result["event_seq"]
            read = self.read(str(initial["plan_ref"]))
            self.assertEqual(sequence, read["event_seq"])
            self.assertEqual(supplied, read["events"][-1]["evidence"])
            self.assertNotIn("evidence_summary", json.dumps(result))
            self.assertNotIn("Synthetic event note", json.dumps(result))
        self.assertEqual(initial_version, self.rows("planning_versions"))

    def test_old_event_seq_rejects_despite_same_plan_version_and_fresh_module_cas(self) -> None:
        initial = self.ordinary()
        observed = self.read(str(initial["plan_ref"]))
        self.advance(initial)
        self.assertEqual(1, self.read(str(initial["plan_ref"]))["current_version"])
        self.assert_rejected_unchanged("plan_event_seq_conflict", lambda: self.advance(
            initial, event_type="resume", expected_event_seq=observed["event_seq"],
        ))

    def test_pause_progress_resume_preserve_state_rules(self) -> None:
        initial = self.ordinary()
        self.assertEqual("paused", self.advance(initial)["state"])
        self.assert_rejected_unchanged("invalid_plan_state_transition", lambda: self.advance(initial))
        self.assertEqual("paused", self.advance(initial, event_type="progress", evidence=fixtures.evidence())["state"])
        self.assertEqual("active", self.advance(initial, event_type="resume")["state"])
        self.assert_rejected_unchanged("invalid_plan_state_transition",
            lambda: self.advance(initial, event_type="resume"))

    def test_required_evidence_and_shape_are_not_manufactured(self) -> None:
        initial = self.ordinary()
        for event_type in ("progress", "complete", "reopen"):
            self.assert_rejected_unchanged("evidence_required",
                lambda: self.advance(initial, event_type=event_type))
        for value in (None, "claimed evidence", [{"evidence_summary": "incomplete"}]):
            self.assert_rejected_unchanged("invalid_evidence",
                lambda: self.advance(initial, evidence=value))
        self.assert_rejected_unchanged("invalid_planning_event_type",
            lambda: self.advance(initial, event_type="defer_review"))

    def test_event_cas_types_and_module_conflict_reject_without_writes(self) -> None:
        initial = self.ordinary()
        for value in (True, False, -1, 1.0, None):
            self.assert_rejected_unchanged("invalid_expected_event_seq",
                lambda: self.advance(initial, expected_event_seq=value))
        self.assert_rejected_unchanged("plan_event_seq_conflict",
            lambda: self.advance(initial, expected_event_seq=0))
        self.assert_rejected_unchanged("planning_row_version_conflict",
            lambda: self.advance(initial, expected_row_version=0))

    def test_content_revision_makes_old_event_target_ref_stale(self) -> None:
        initial = self.ordinary()
        self.revise(initial)
        self.assert_rejected_unchanged("plan_version_conflict", lambda: self.advance(initial))

    def test_another_plan_event_does_not_replace_this_plans_observed_sequence(self) -> None:
        initial = self.ordinary()
        observed = self.read(str(initial["plan_ref"]))["event_seq"]
        other = self.ordinary(content="Another synthetic plan")
        self.advance(other)
        result = self.advance(initial, expected_event_seq=observed)
        self.assertEqual("paused", result["state"])
        self.assertEqual(observed, result["previous_event_seq"])
        self.assertGreater(result["event_seq"], observed + 1)

    def test_vision_and_unfinished_child_completion_guards_remain(self) -> None:
        vision = self.ordinary(kind="vision")
        self.assert_rejected_unchanged("vision_cannot_complete",
            lambda: self.advance(vision, event_type="complete", evidence=fixtures.evidence()))
        parent = self.ordinary(kind="goal")
        self.ordinary(parent_ref=parent["plan_ref"])
        self.assert_rejected_unchanged("active_children_remaining",
            lambda: self.advance(parent, event_type="complete", evidence=fixtures.evidence()))

    def test_event_preserves_pending_candidate_and_cannot_revive_terminal_or_quarantine(self) -> None:
        initial = self.ordinary()
        self.pending_revision(initial)
        candidates = self.rows("planning_change_candidates")
        self.advance(initial, event_type="progress", evidence=fixtures.evidence())
        self.assertEqual(candidates, self.rows("planning_change_candidates"))
        terminal = self.ordinary(content="Synthetic terminal boundary")
        self.retire(terminal, "abandon")
        self.assert_rejected_unchanged("invalid_plan_state_transition",
            lambda: self.advance(terminal, event_type="reopen", evidence=fixtures.evidence()))
        other = self.ordinary()
        with self.store._connect() as connection:
            connection.execute("UPDATE planning_items SET recall_lifecycle='quarantined' WHERE plan_id=?",
                               (self.plan_id(other),))
        # Read of quarantined content is deliberately unavailable, so explicitly
        # supply the last valid observation instead of using the read helper.
        self.assert_rejected_unchanged("plan_quarantined", lambda: self.store.record_ordinary_event(
            owner_id=fixtures.OWNER, model_id=fixtures.MODEL, wake_id="synthetic-wake", wake_seq=200,
            expected_row_version=self.row_version(), plan_id=self.plan_id(other), expected_plan_version=1,
            expected_event_seq=0, event_type="pause", reason="Synthetic note",
        ))

    def test_read_state_event_seq_and_history_use_one_ledger_snapshot(self) -> None:
        initial = self.ordinary()
        original = self.store._event_rows
        with self.store._connect() as connection:
            item = self.store._item(connection, fixtures.OWNER, fixtures.MODEL, self.plan_id(initial))
            with patch.object(self.store, "_event_rows", wraps=original) as read_events:
                observed = self.store._public_plan(
                    connection, owner_id=fixtures.OWNER, model_id=fixtures.MODEL,
                    item=item, include_history=True,
                )
        self.assertEqual(1, read_events.call_count)
        self.assertEqual(1, observed["current_version"])
        self.assertEqual("active", observed["state_projection"]["state"])
        self.assertIs(type(observed["event_seq"]), int)
        self.assertGreater(observed["event_seq"], 0)


if __name__ == "__main__":
    unittest.main()
