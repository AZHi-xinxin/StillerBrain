"""Isolated ordinary planning writes; no live services, credentials or private DB.

These exercise the runtime's storage contract after the service has validated
the exact source-bound reference. Synthetic references are not authentication
tests, and no test calls or substitutes the production binding service.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime.planning_memory import PlanningMemoryError, PlanningMemoryStore
from tests.test_planning_memory import calm, content as advanced_content


OWNER = "synthetic-owner"
MODEL = "synthetic-model"


class OrdinaryPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="planning-ordinary-test-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "synthetic.sqlite3"
        self.store = PlanningMemoryStore(self.database)
        self.counter = 0
        self.enterContext(patch("socket.socket", side_effect=AssertionError("offline only")))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("offline only")))

    def rows(self, table):
        self.assertIn(table, {
            "planning_module_state", "planning_items", "planning_versions",
            "planning_events", "planning_edges", "planning_change_candidates",
            "planning_audit_events", "planning_idempotency_records",
        })
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)]

    def write(self, **overrides):
        self.counter += 1
        states = self.rows("planning_module_state")
        version = next((row["row_version"] for row in states
                        if row["owner_id"] == OWNER and row["model_id"] == MODEL), 0)
        args = dict(owner_id=OWNER, model_id=MODEL,
                    write_context_ref="artifact_synthetic_open_1", wake_id="wake-synthetic-1",
                    wake_seq=1, expected_row_version=version,
                    content="A synthetic ordinary plan", idempotency_key=f"request-{self.counter}")
        args.update(overrides)
        return self.store.remember_ordinary(**args)

    def read(self, result):
        return self.store.recall(owner_id=OWNER, model_id=MODEL,
                                 plan_ref=result["plan_ref"], include_history=True)["plans"][0]

    def test_minimal_direct_write_preserves_original_and_has_no_fake_review(self):
        original = "  A synthetic plan\nwith original spacing.  "
        result = self.write(content=original)
        self.assertEqual(result["decision"], "stored")
        self.assertEqual(result["state"], "active")
        self.assertEqual(result["plan_version"], 1)
        self.assertEqual(result["planning_row_version"], 1)
        self.assertFalse(result["candidate_created"])
        self.assertFalse(result["review_performed"])
        self.assertEqual(result["display_excerpt_fields"], ["title", "summary", "reminder"])
        saved = self.read(result)
        self.assertEqual(saved["content"]["original_text"], original)
        self.assertEqual(saved["content"]["write_mode"], "ordinary_record")
        self.assertNotIn("ai_adoption_statement", saved["content"])
        self.assertNotIn("calm_check", saved["content"])
        self.assertEqual(saved["content"]["presence_mode"], "relevant")
        self.assertFalse(saved["content"]["allow_coordination_hint"])
        self.assertEqual(saved["events"][0]["event_type"], "created")
        self.assertEqual(saved["events"][0]["evidence"], [])
        self.assertEqual(saved["versions"][0]["reason"], "ordinary_record")
        self.assertEqual(self.rows("planning_change_candidates"), [])
        audit = self.rows("planning_audit_events")
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "remember_ordinary")
        self.assertIsNone(audit[0]["candidate_id"])

    def test_every_supplied_kind_can_be_flat_without_silent_conversion(self):
        for kind in ("vision", "goal", "milestone", "task", "commitment"):
            with self.subTest(kind=kind):
                result = self.write(kind=kind, track="relational", reminder="")
                saved = self.read(result)["content"]
                self.assertEqual(saved["kind"], kind)
                self.assertEqual(saved["track"], "relational")
                self.assertIsNone(saved["parent_ref"])
        self.assertEqual(len(self.rows("planning_items")), 5)
        self.assertEqual(self.rows("planning_edges"), [])

    def test_supplied_display_fields_not_replaced_and_internal_empty_reminder_allowed(self):
        result = self.write(content="Original", title="Given title", summary="Given summary",
                            reminder="", importance=61)
        saved = self.read(result)["content"]
        self.assertEqual(saved["title"], "Given title")
        self.assertEqual(saved["summary"], "Given summary")
        self.assertEqual(saved["reminder"], "")
        self.assertEqual(saved["importance"], 61)
        self.assertEqual(result["display_excerpt_fields"], [])

    def test_idempotent_retry_does_not_duplicate_even_with_refreshed_module_cas(self):
        first = self.write(idempotency_key="stable-request")
        replay = self.write(idempotency_key="stable-request")
        self.assertEqual(first["plan_ref"], replay["plan_ref"])
        self.assertFalse(replay["state_changed"])
        self.assertFalse(replay["active_plan_changed"])
        self.assertTrue(replay["idempotent_replay"])
        for table in ("planning_items", "planning_versions", "planning_events",
                      "planning_audit_events", "planning_idempotency_records"):
            self.assertEqual(len(self.rows(table)), 1)
        with self.assertRaisesRegex(PlanningMemoryError, "idempotency_key_reused"):
            self.write(idempotency_key="stable-request", content="Changed request")
        with self.assertRaisesRegex(PlanningMemoryError, "idempotency_key_reused"):
            self.write(idempotency_key="stable-request", wake_id="wake-other", wake_seq=2)
        self.write(content="Another independent ordinary record")
        after_other_write = self.write(idempotency_key="stable-request")
        self.assertEqual(after_other_write["planning_row_version"], 2)
        self.assertEqual(len(self.rows("planning_items")), 2)

    def test_cas_conflict_has_no_partial_plan_event_or_state_initialization(self):
        with self.assertRaisesRegex(PlanningMemoryError, "planning_row_version_conflict"):
            self.write(expected_row_version=9)
        for table in ("planning_module_state", "planning_items", "planning_versions",
                      "planning_events", "planning_audit_events", "planning_idempotency_records"):
            self.assertEqual(self.rows(table), [])
        self.write()
        with self.assertRaisesRegex(PlanningMemoryError, "planning_row_version_conflict"):
            self.write(expected_row_version=0)
        self.assertEqual(len(self.rows("planning_items")), 1)
        self.assertEqual(self.rows("planning_module_state")[0]["row_version"], 1)

    def test_invalid_context_shape_secret_and_content_reject_before_mutation(self):
        cases = (
            ({"write_context_ref": ""}, "invalid_write_context_ref"),
            ({"write_context_ref": "$.write_context_ref"}, "write_context_ref_placeholder"),
            ({"wake_id": ""}, "invalid_wake_id"),
            ({"wake_seq": True}, "invalid_wake_seq"),
            ({"expected_row_version": True}, "invalid_expected_planning_version"),
            ({"idempotency_key": ""}, "invalid_idempotency_key"),
            ({"content": "  "}, "invalid_original_text"),
            ({"content": " " + "x" * 2000}, "original_text_too_long"),
            ({"content": "password: synthetic-not-a-real-credential"}, "credential_content_rejected"),
            ({"kind": "unknown"}, "invalid_plan_kind"),
            ({"track": "unknown"}, "invalid_plan_track"),
            ({"start_at": "2026-09-08", "due_at": "2026-09-07"}, "due_before_start"),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(PlanningMemoryError, reason):
                    self.write(**overrides)
                self.assertEqual(self.rows("planning_items"), [])
                self.assertEqual(self.rows("planning_module_state"), [])

    def test_explicit_parent_and_dependency_remain_exact_version_owner_scoped(self):
        parent = self.write(kind="goal")
        child = self.write(kind="task", parent_ref=parent["plan_ref"],
                           dependency_refs=[parent["plan_ref"]])
        self.assertEqual(self.read(child)["content"]["parent_ref"], parent["plan_ref"])
        self.assertEqual(len(self.rows("planning_edges")), 2)
        for overrides, reason in (
            ({"kind": "vision", "parent_ref": parent["plan_ref"]}, "invalid_plan_hierarchy"),
            ({"parent_ref": parent["plan_ref"].replace("@1", "@2")}, "stale_plan_ref"),
            ({"owner_id": "other-owner", "expected_row_version": 0,
              "parent_ref": parent["plan_ref"]}, "plan_not_found"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(PlanningMemoryError, reason):
                    self.write(**overrides)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE planning_items SET recall_lifecycle='quarantined' WHERE plan_id=?",
                               (parent["plan_id"],))
        with self.assertRaisesRegex(PlanningMemoryError, "quarantined_plan_ref"):
            self.write(parent_ref=parent["plan_ref"])
        self.assertEqual(len(self.rows("planning_items")), 2)

    def test_existing_candidate_is_not_activated_by_ordinary_write(self):
        pending = self.store.propose_create(
            owner_id=OWNER, model_id=MODEL, wake_id="advanced-wake", wake_seq=0,
            expected_row_version=0, content=advanced_content("existing candidate"),
            reason="Synthetic independent adoption", calm_check=calm(),
            ai_confirmation=True, idempotency_key="advanced-create",
        )
        before = self.rows("planning_change_candidates")
        self.write()
        self.assertEqual(before, self.rows("planning_change_candidates"))
        self.assertEqual(before[0]["candidate_id"], pending["candidate_id"])
        self.assertEqual(before[0]["status"], "pending")
        self.assertEqual(len(self.rows("planning_items")), 1)

    def test_advanced_creation_cannot_claim_ordinary_mode_to_bypass_legacy_contract(self):
        ordinary = self.write()
        fields = self.read(ordinary)["content"]
        with self.assertRaisesRegex(PlanningMemoryError, "invalid_plan_content"):
            self.store.propose_create(
                owner_id=OWNER, model_id=MODEL, wake_id="advanced-wake", wake_seq=2,
                expected_row_version=1, content=fields, reason="Synthetic request",
                calm_check=calm(), ai_confirmation=True, idempotency_key="bad-advanced",
            )
        self.assertEqual(len(self.rows("planning_items")), 1)
        self.assertEqual(self.rows("planning_change_candidates"), [])

    def test_advanced_revision_cannot_change_write_mode_or_forge_adoption(self):
        ordinary = self.write()
        args = dict(owner_id=OWNER, model_id=MODEL, wake_id="advanced-wake", wake_seq=2,
                    expected_row_version=1, plan_id=ordinary["plan_id"], expected_plan_version=1,
                    intent="revise", reason="Synthetic request", calm_check=calm(),
                    ai_confirmation=True, idempotency_key="bad-mode")
        for changes in ({"write_mode": "legacy"}, {"ai_adoption_statement": "I agree"}):
            with self.subTest(changes=changes):
                with self.assertRaises(PlanningMemoryError):
                    self.store.propose_revision(**args, changes=changes)
        self.assertEqual(self.rows("planning_change_candidates"), [])
        self.assertEqual(len(self.rows("planning_versions")), 1)

    def test_recall_and_injection_work_without_adoption_field(self):
        result = self.write(content="Ordinary orchid watering plan", title="Orchid watering",
                            summary="Water the orchid", scene_tags=["orchid"], keywords=["watering"])
        before = self.rows("planning_events")
        found = self.store.recall(owner_id=OWNER, model_id=MODEL, query="orchid watering")
        self.assertEqual(found["plans"][0]["plan_id"], result["plan_id"])
        injected = self.store.build_injection(owner_id=OWNER, model_id=MODEL,
                                             query="orchid watering")
        self.assertIn(result["plan_id"], json.dumps(injected))
        self.assertNotIn("ai_adoption_statement", json.dumps(injected))
        self.assertNotIn("Ordinary orchid watering plan", json.dumps(injected))
        self.assertEqual(before, self.rows("planning_events"))

    def test_advanced_revision_of_ordinary_keeps_review_and_append_only_history(self):
        initial = self.write(kind="goal", content="Original verbatim text")
        pending = self.store.propose_revision(
            owner_id=OWNER, model_id=MODEL, wake_id="wake-synthetic-1", wake_seq=1,
            expected_row_version=1, plan_id=initial["plan_id"], expected_plan_version=1,
            intent="revise", changes={"summary": "Revised display"}, reason="Synthetic revision",
            calm_check=calm(), ai_confirmation=True, idempotency_key="advanced-revision",
        )
        candidate = self.store.present_pending_candidates(
            owner_id=OWNER, model_id=MODEL, wake_id="wake-synthetic-1", wake_seq=1,
        )[0]
        args = dict(owner_id=OWNER, model_id=MODEL, wake_id="wake-synthetic-1", wake_seq=1,
                    expected_row_version=2, candidate_id=pending["candidate_id"],
                    expected_candidate_version=candidate["candidate_version"],
                    expected_candidate_hash=candidate["candidate_hash"],
                    expected_base_version=1, decision="accept", correctness_assessment="Synthetic assessment",
                    calm_check=calm(), reason="Synthetic review", ai_confirmation=True)
        with self.assertRaisesRegex(PlanningMemoryError, "later_real_wake_required"):
            self.store.review_change(**args)
        self.store.present_pending_candidates(owner_id=OWNER, model_id=MODEL,
                                               wake_id="wake-synthetic-2", wake_seq=2)
        reviewed = self.store.review_change(**{**args, "wake_id": "wake-synthetic-2", "wake_seq": 2})
        latest = self.read(reviewed)
        self.assertEqual(len(latest["versions"]), 2)
        self.assertEqual(latest["content"]["summary"], "Revised display")
        self.assertEqual(latest["content"]["write_mode"], "ordinary_record")
        self.assertEqual(self.read(initial)["content"]["original_text"], "Original verbatim text")
        self.assertEqual(self.read(initial)["content"]["summary"], "Original verbatim text")


if __name__ == "__main__":
    unittest.main(verbosity=2)
