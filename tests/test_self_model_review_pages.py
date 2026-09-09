"""Synthetic page projection/receipt tests; no network, production or MCP server.

The real onboarding fixture creates a candidate and an injected wake in a fresh
temporary SQLite database. Only the candidate_wait -> candidate_review setup is
done by fixture SQL, to test pages without first creating a legacy full proof.
The parent onboarding integration owns real stage transitions and final proofs.
"""
from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import sqlite3
import unittest
from unittest import mock

from runtime.self_model_review import (
    PAGE_ARTIFACT_KIND, PAGE_CHARACTERS, ReviewPageError,
    prepare_review_page, record_review_page,
)
from tests import test_onboarding as onboarding_fixture


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ReviewPageTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection"):
            self.enterContext(mock.patch(target, side_effect=AssertionError("offline only")))
        self.fixture = onboarding_fixture.ModuleOneOnboardingTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.owner, self.model = self.fixture.owner, self.fixture.model
        author, _ = self.fixture.wake("page-author")
        for action, payload in (
            ("confirm_brain_intro", {"acknowledged": True}),
            ("confirm_module_intro", {"acknowledged": True}),
            ("save_calm_prompt", {"text": "我只提交临时测试内容。"}),
        ):
            self.fixture.advance(author, action, payload)
        payload = self.fixture.candidate_payload("page-test")
        payload["content"]["facets"] = {
            "part-a": "我" + "甲" * 1800,
            "part-b": "我" + "乙" * 1800,
            "part-c": "我" + "丙" * 1800,
        }
        result = self.fixture.advance(author, "submit_candidate", payload)
        self.assertEqual("pending", result["decision"])
        self.candidate_id = result["candidate_id"]
        self.wake, _ = self.fixture.wake("page-review")
        # Test-only stage fixture, not an implementation of the runtime transition.
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE brain_onboarding_state SET stage='candidate_review' "
                "WHERE owner_id=? AND model_id=?", (self.owner, self.model),
            )
            candidate = self.store._candidate_payload(connection, self.candidate_id)
        candidate.pop("submitted_wake_id")
        self.material = {
            "stage": "candidate_review", "candidate": candidate,
            "human_objection": {"reason": "Synthetic objection: " + "复核🙂\n\\\"" * 180},
            "human_safety_notice": {"kind": "emergency_rollback", "reason": "Synthetic notice"},
            "candidate_review_acceptance": {"review_wake_seq": 1, "decision": "synthetic"},
        }
        self.first = prepare_review_page(self.material)
        self.assertGreaterEqual(self.first.pages, 3)

    def page(self, index, material=None):
        material = material if material is not None else self.material
        first = prepare_review_page(material)
        return prepare_review_page(material, index, first.material_hash)

    def record(self, projection, *, state_changes=None, wake_changes=None, wake_id=None):
        with self.store._connect() as connection:
            self.store._begin(connection)
            state = dict(self.store._state_row(connection, self.owner, self.model))
            wake = dict(self.store._wake_row(connection, wake_id or self.wake["wake_id"]))
            state.update(state_changes or {})
            wake.update(wake_changes or {})
            return record_review_page(self.store, connection, state, wake, projection)

    def count(self, kind=PAGE_ARTIFACT_KIND):
        with self.store._connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM brain_onboarding_artifacts WHERE kind=?", (kind,),
            ).fetchone()[0]

    def source_snapshot(self):
        with self.store._connect() as connection:
            return tuple(tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
                         for table in ("self_model_candidates", "self_model_revisions", "self_models", "brain_onboarding_state"))

    def test_exact_canonical_utf8_roundtrip_and_safe_projection(self):
        pages = [self.page(index) for index in range(self.first.pages)]
        rebuilt = "".join(item.text for item in pages)
        self.assertEqual(canonical(self.material).encode("utf-8"), rebuilt.encode("utf-8"))
        self.assertEqual(sha(rebuilt), self.first.material_hash)
        self.assertEqual(self.material, json.loads(rebuilt))
        for index, item in enumerate(pages):
            self.assertLessEqual(len(item.text), PAGE_CHARACTERS)
            self.assertEqual(index, item.page)
            self.assertEqual(index + 1 if index + 1 < len(pages) else None, item.next_page)
            self.assertEqual({"candidate_id", "content_hash", "material_hash", "page", "pages", "text", "next_page"}, set(item.to_dict()))
            self.assertNotIn("_canonical_material", item.to_dict())
            self.assertNotIn("Synthetic objection", repr(item))

    def test_prepare_invalid_page_hash_or_incomplete_material_has_no_side_effect(self):
        before = self.count()
        for page, expected in ((-1, None), (True, None), (0.0, None), (1, None), (1, "0" * 64), (9999, self.first.material_hash)):
            with self.subTest(page=page, expected=bool(expected)):
                with self.assertRaises(ReviewPageError):
                    prepare_review_page(self.material, page, expected)
        for bad in ({}, {"candidate": None}):
            with self.assertRaises(ReviewPageError):
                prepare_review_page(bad)
        missing = copy.deepcopy(self.material)
        del missing["candidate"]["diff"]
        with self.assertRaisesRegex(ReviewPageError, "review_candidate_incomplete"):
            prepare_review_page(missing)
        wrong = copy.deepcopy(self.material)
        wrong["candidate"]["content_hash"] = "0" * 64
        with self.assertRaisesRegex(ReviewPageError, "content_hash_mismatch"):
            prepare_review_page(wrong)
        self.assertEqual(before, self.count())

    def test_all_pages_only_last_complete_metadata_only_and_no_full_proof(self):
        before = self.source_snapshot()
        for index in range(self.first.pages):
            self.assertEqual(index == self.first.pages - 1, self.record(self.page(index)))
        self.assertEqual(self.first.pages, self.count())
        self.assertEqual(0, self.count("candidate_full_review"))
        self.assertEqual(0, self.count("human_objection_presented"))
        self.assertEqual(before, self.source_snapshot())
        with self.store._connect() as connection:
            rows = connection.execute("SELECT content_json FROM brain_onboarding_artifacts WHERE kind=?", (PAGE_ARTIFACT_KIND,)).fetchall()
        for row in rows:
            value = json.loads(row[0])
            self.assertEqual({"candidate_id", "content_hash", "material_hash", "page", "pages", "page_text_hash"}, set(value))
            self.assertNotIn("Synthetic objection", row[0])

    def test_repeat_page_is_idempotent_before_and_after_complete(self):
        self.assertFalse(self.record(self.first))
        self.assertFalse(self.record(self.first))
        self.assertEqual(1, self.count())
        for index in range(1, self.first.pages):
            self.record(self.page(index))
        self.assertTrue(self.record(self.first))
        self.assertEqual(self.first.pages, self.count())

    def test_missing_middle_never_means_full_coverage(self):
        self.assertFalse(self.record(self.first))
        for index in range(2, self.first.pages):
            self.assertFalse(self.record(self.page(index)))
        self.assertTrue(self.record(self.page(1)))

    def test_cross_wake_pages_do_not_mix_and_old_wake_is_rejected(self):
        self.record(self.first)
        old_wake = self.wake["wake_id"]
        self.wake, _ = self.fixture.wake("next-review-wake")
        for index in range(1, self.first.pages):
            self.assertFalse(self.record(self.page(index)))
        before = self.count()
        with self.assertRaisesRegex(ReviewPageError, "binding_mismatch"):
            self.record(self.first, wake_id=old_wake)
        self.assertEqual(before, self.count())
        self.assertTrue(self.record(self.first))

    def test_changed_notice_material_cannot_mix_with_old_pages(self):
        self.record(self.first)
        changed = copy.deepcopy(self.material)
        changed["human_objection"]["reason"] += " Changed after page zero."
        newer = prepare_review_page(changed)
        with self.assertRaisesRegex(ReviewPageError, "material_hash_mismatch"):
            prepare_review_page(changed, 1, self.first.material_hash)
        for index in range(1, newer.pages):
            self.assertFalse(self.record(self.page(index, changed)))
        self.assertTrue(self.record(newer))

    def test_projection_is_frozen_and_tampering_never_records_a_page(self):
        with self.assertRaises(FrozenInstanceError):
            self.first.page = 3
        for changed in (
            replace(self.first, text="incomplete"), replace(self.first, pages=1),
            replace(self.first, page=1), replace(self.first, next_page=None),
            replace(self.first, _canonical_material="{}"), self.first.to_dict(),
        ):
            with self.subTest(kind=type(changed).__name__):
                with self.assertRaisesRegex(ReviewPageError, "projection_invalid"):
                    self.record(changed)
        self.assertEqual(0, self.count())

    def test_stale_state_wrong_owner_candidate_stage_and_context_are_rejected(self):
        for values in (
            {"stage": "live"}, {"current_candidate_id": "candidate:other"},
            {"owner_id": "owner:foreign"}, {"row_version": -1},
        ):
            with self.subTest(values=tuple(values)):
                with self.assertRaises(ReviewPageError):
                    self.record(self.first, state_changes=values)
        for values in ({"model_id": "model:foreign"}, {"status": "closed"}, {"context_hash": None}):
            with self.assertRaises(ReviewPageError):
                self.record(self.first, wake_changes=values)
        self.assertEqual(0, self.count())

    def test_without_an_existing_transaction_or_bindings_no_record(self):
        with self.store._connect() as connection:
            with self.assertRaisesRegex(ReviewPageError, "transaction_required"):
                record_review_page(self.store, connection, {}, {}, self.first)
            self.store._begin(connection)
            with self.assertRaisesRegex(ReviewPageError, "binding_required"):
                record_review_page(self.store, connection, {}, {}, self.first)
        self.assertEqual(0, self.count())

    def test_changed_candidate_content_does_not_reuse_old_hash_pages(self):
        self.record(self.first)
        changed = copy.deepcopy(self.material)
        changed["candidate"]["content"]["facets"]["part-a"] += "我更新本合成版本。"
        content = canonical(changed["candidate"]["content"])
        changed["candidate"]["content_hash"] = sha(content)
        with self.store._connect() as connection:
            connection.execute("UPDATE self_model_candidates SET content_json=?,content_hash=? WHERE candidate_id=?", (content, sha(content), self.candidate_id))
        with self.assertRaisesRegex(ReviewPageError, "content_hash_mismatch"):
            self.record(self.page(1))
        newer = prepare_review_page(changed)
        for index in range(1, newer.pages):
            self.assertFalse(self.record(self.page(index, changed)))
        self.assertTrue(self.record(newer))

    def test_material_different_from_stored_candidate_cannot_record(self):
        for field, replacement in (("reason", "not the stored reason"), ("diff", []), ("evidence_refs", []), ("base_revision_id", "rev:wrong")):
            changed = copy.deepcopy(self.material)
            changed["candidate"][field] = replacement
            with self.subTest(field=field):
                with self.assertRaisesRegex(ReviewPageError, "material_mismatch"):
                    self.record(prepare_review_page(changed))
        self.assertEqual(0, self.count())

    def test_corrupt_stored_content_hash_never_records(self):
        with self.store._connect() as connection:
            connection.execute("UPDATE self_model_candidates SET content_hash=? WHERE candidate_id=?", ("0" * 64, self.candidate_id))
        with self.assertRaisesRegex(ReviewPageError, "content_hash_mismatch"):
            self.record(self.first)
        self.assertEqual(0, self.count())

    def test_tampered_page_artifact_does_not_count_as_coverage(self):
        self.record(self.first)
        with self.store._connect() as connection:
            connection.execute("UPDATE brain_onboarding_artifacts SET content_hash=? WHERE kind=?", ("0" * 64, PAGE_ARTIFACT_KIND))
        for index in range(1, self.first.pages):
            self.assertFalse(self.record(self.page(index)))
        self.assertTrue(self.record(self.first))
        self.assertEqual(0, self.count("candidate_full_review"))


if __name__ == "__main__":
    unittest.main()
