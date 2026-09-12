"""Single author submissions and legacy queue safety, on temporary databases."""
import json
import unittest

from runtime.learning_memory import LearningMemoryError
from tests import test_learning_memory as fixtures


class LearningAuthorSubmissionTests(unittest.TestCase):
    setUp = fixtures.LearningMemoryRuntimeTests.setUp
    tearDown = fixtures.LearningMemoryRuntimeTests.tearDown
    card = fixtures.LearningMemoryRuntimeTests.card
    remember = fixtures.LearningMemoryRuntimeTests.remember
    evidence_item = staticmethod(fixtures.LearningMemoryRuntimeTests.evidence_item)
    calm_check = staticmethod(fixtures.LearningMemoryRuntimeTests.calm_check)
    integration_candidate = fixtures.LearningMemoryRuntimeTests.integration_candidate

    def binding(self, version):
        return dict(owner_id=self.owner, model_id=self.model, wake_id="author",
                    wake_seq=3, expected_row_version=version)

    def counts(self):
        with self.store._connect() as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("learning_items", "learning_versions", "learning_change_candidates",
                                  "learning_verification_events", "learning_direct_integrations",
                                  "learning_integrations", "learning_audit_events")}

    def integrate(self, sources, version, **extra):
        return self.store.integrate(**self.binding(version),
                                    source_learning_ids=[item["item_ref"] for item in sources],
                                    synthesis_kind="summary", **extra,
                                    **self.card("synthesis", referent_bindings=[]))

    def test_semantic_revision_is_one_call_no_fake_self_rating_or_verification(self):
        original = self.remember()
        result = self.store.revise(**self.binding(1), target_ref=original["item_ref"],
                                  changes={"current_understanding": "Author changed their understanding."},
                                  calm_check={"rollback_understood": False}, ai_confirmation=None)
        self.assertEqual("applied", result["decision"])
        self.assertEqual(2, result["item_version"])
        self.assertNotIn("candidate_id", result)
        self.assertEqual(0, self.counts()["learning_change_candidates"])
        self.assertEqual(0, self.counts()["learning_verification_events"])
        with self.store._connect() as connection:
            rows = connection.execute("SELECT * FROM learning_versions ORDER BY version").fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual("", rows[1]["correctness_assessment"])
        self.assertEqual("", rows[1]["ai_diff"])
        self.assertNotEqual(rows[0]["mutable_json"], rows[1]["mutable_json"])

    def test_target_ref_and_module_cas_are_not_replaced_by_latest(self):
        original = self.remember()
        before = self.counts()
        for overrides, error in (({"expected_target_version": 2}, "target_version_conflict"),
                                 ({"expected_target_version": True}, "target_version_conflict"),
                                 ({"expected_row_version": 0}, "row_version_conflict")):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(LearningMemoryError, error):
                self.store.revise(**{**self.binding(1), **overrides},
                                  target_ref=original["item_ref"], changes={"summary": "changed"})
            self.assertEqual(before, self.counts())

    def test_rollback_appends_history_without_replacing_original(self):
        original = self.remember()
        changed = self.store.revise(**self.binding(1), target_ref=original["item_ref"],
                                    changes={"current_understanding": "New understanding"})
        restored = self.store.revise(**self.binding(2), target_ref=changed["item_ref"],
                                    action="rollback", rollback_to_version=1)
        self.assertEqual(3, restored["item_version"])
        with self.store._connect() as connection:
            rows = connection.execute("SELECT mutable_hash FROM learning_versions ORDER BY version").fetchall()
        self.assertEqual(rows[0][0], rows[2][0])
        self.assertNotEqual(rows[0][0], rows[1][0])

    def test_integration_pins_sources_and_does_not_create_review_or_verified_fact(self):
        one, two = self.remember(0, "one"), self.remember(1, "two")
        result = self.integrate([one, two], 2)
        self.assertEqual("applied", result["decision"])
        self.assertEqual([one["item_ref"], two["item_ref"]], [s["ref"] for s in result["source_snapshot"]])
        self.assertEqual(3, self.counts()["learning_items"])
        self.assertEqual(0, self.counts()["learning_change_candidates"])
        self.assertEqual(0, self.counts()["learning_verification_events"])
        self.assertEqual(2, self.counts()["learning_integrations"])
        with self.store._connect() as connection:
            lineage = self.store._accepted_integration_lineage(connection, owner_id=self.owner, model_id=self.model)
        self.assertEqual(2, len(lineage[result["learning_id"]]))

    def test_integration_requires_exact_source_versions_and_stale_source_is_atomic(self):
        one, two = self.remember(0, "one"), self.remember(1, "two")
        before = self.counts()
        with self.assertRaisesRegex(LearningMemoryError, "source_version_required"):
            self.store.integrate(**self.binding(2), source_learning_ids=[one["learning_id"], two["learning_id"]],
                                 synthesis_kind="summary", **self.card("new"))
        self.assertEqual(before, self.counts())
        self.store.revise(**self.binding(2), target_ref=one["item_ref"], changes={"summary": "edited"})
        before = self.counts()
        with self.assertRaisesRegex(LearningMemoryError, "source_version_conflict"):
            self.integrate([one, two], 3)
        self.assertEqual(before, self.counts())

    def test_explicit_source_versions_mapping_is_compatible_and_archive_is_versioned(self):
        one, two = self.remember(0, "one"), self.remember(1, "two")
        result = self.store.integrate(**self.binding(2),
            source_learning_ids=[one["learning_id"], two["learning_id"]],
            source_versions={one["learning_id"]: 1, two["learning_id"]: 1},
            synthesis_kind="summary", source_action="archive_after_accept", **self.card("combined"))
        self.assertEqual("applied", result["decision"])
        self.assertEqual(5, self.counts()["learning_versions"])
        with self.store._connect() as connection:
            rows = connection.execute("SELECT * FROM learning_items WHERE learning_id IN (?, ?)",
                                      (one["learning_id"], two["learning_id"])).fetchall()
        self.assertTrue(all(row["current_version"] == 2 and row["lifecycle"] == "archived" for row in rows))
        self.assertTrue(all(json.loads(row["current_json"])["lifecycle"] == "archived" for row in rows))

    def test_quarantine_and_owner_isolation_survive_simplification(self):
        quarantined = self.remember(0, "isolated", epistemic_status="disputed")
        ordinary = self.remember(1, "ordinary")
        before = self.counts()
        with self.assertRaisesRegex(LearningMemoryError, "integration_source_quarantined"):
            self.integrate([quarantined, ordinary], 2)
        with self.assertRaises(LearningMemoryError):
            self.store.revise(**{**self.binding(2), "owner_id": "another-owner"},
                              target_ref=ordinary["item_ref"], changes={"summary": "cross owner"})
        self.assertEqual(before, self.counts())

    def test_new_submission_does_not_touch_old_pending_and_prior_full_presentation_is_enough(self):
        one, two = self.remember(0, "one"), self.remember(1, "two")
        pending = self.integration_candidate(row_version=2, wake_seq=3, sources=[one, two], suffix="legacy")
        self.integrate([one, two], 3)
        self.assertEqual(1, self.store.status(owner_id=self.owner, model_id=self.model)["counts"]["pending_changes"])
        self.store.review_snapshot(owner_id=self.owner, model_id=self.model, wake_id="earlier", wake_seq=3)
        accepted = self.store.review_change(**{**self.binding(4), "wake_id": "new-wake", "wake_seq": 4},
            candidate_id=pending["candidate_id"], expected_candidate_version=1,
            expected_candidate_hash=pending["candidate_hash"], expected_base_version=0,
            action="accept", ai_confirmation=True)
        self.assertEqual("accepted", accepted["decision"])

    def test_damaged_source_or_invalid_pins_do_not_partially_write_integration(self):
        one, two = self.remember(0, "one"), self.remember(1, "two")
        before = self.counts()
        with self.assertRaisesRegex(LearningMemoryError, "source_versions_invalid"):
            self.integrate([one, two], 2, source_versions={one["learning_id"]: True})
        self.assertEqual(before, self.counts())
        with self.store._connect() as connection:
            connection.execute("UPDATE learning_versions SET mutable_json='{}' WHERE learning_id=?", (one["learning_id"],))
        with self.assertRaisesRegex(LearningMemoryError, "integration_source_integrity_mismatch"):
            self.integrate([one, two], 2)
        self.assertEqual(before, self.counts())

    def test_unknown_merge_suggestion_cannot_be_silently_consumed(self):
        one, two = self.remember(0, "one"), self.remember(1, "two")
        before = self.counts()
        with self.assertRaisesRegex(LearningMemoryError, "merge_suggestion_not_open"):
            self.integrate([one, two], 2, merge_suggestion_id="nonexistent")
        self.assertEqual(before, self.counts())

    def test_old_candidate_hash_tamper_cannot_accept_but_can_reject_without_reopening(self):
        one, two = self.remember(0, "one"), self.remember(1, "two")
        pending = self.integration_candidate(row_version=2, wake_seq=3, sources=[one, two], suffix="legacy")
        self.store.review_snapshot(owner_id=self.owner, model_id=self.model, wake_id="earlier", wake_seq=3)
        with self.store._connect() as connection:
            row = connection.execute("SELECT proposed_json FROM learning_change_candidates").fetchone()
            proposed = json.loads(row[0]); proposed["summary"] = "tampered"
            connection.execute("UPDATE learning_change_candidates SET proposed_json=?", (json.dumps(proposed),))
        fields = dict(**self.binding(3), candidate_id=pending["candidate_id"], expected_candidate_version=1,
                      expected_candidate_hash=pending["candidate_hash"], expected_base_version=0)
        with self.assertRaisesRegex(LearningMemoryError, "candidate_content_hash_mismatch"):
            self.store.review_change(**fields, action="accept", ai_confirmation=True)
        with self.store._connect() as connection:
            connection.execute("DELETE FROM learning_versions WHERE learning_id=?", (one["learning_id"],))
        result = self.store.review_change(**fields, action="reject")
        self.assertEqual("rejected", result["decision"])


if __name__ == "__main__":
    unittest.main()
