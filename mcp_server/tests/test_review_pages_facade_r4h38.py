"""MCP-facade regression: full candidate review without a workspace tool."""
from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from mcp_server.tests import test_compact_open as fixtures


class ReviewPagesFacadeTests(unittest.TestCase):
    setUp = fixtures.CompactOpenStateTests.setUp
    wake = fixtures.CompactOpenStateTests.wake
    advance = fixtures.CompactOpenStateTests.advance
    runtime_open = fixtures.CompactOpenStateTests.runtime_open
    proof_count = fixtures.CompactOpenStateTests.proof_count
    candidate_wait = fixtures.CompactOpenStateTests.candidate_wait
    bootstrap_live = fixtures.CompactOpenStateTests.bootstrap_live

    def large_candidate(self):
        body = fixtures.model_content("synthetic-paged")
        body["facets"] = {f"side_{i}": "平静的合成文字。" * 250 for i in range(4)}
        with mock.patch.object(fixtures, "model_content", return_value=body):
            candidate = self.candidate_wait()
        return body, candidate

    def read_all(self, first=None):
        opened = first or self.service.open_brain(view="review")
        pages = [opened]
        for _ in range(20):
            if opened["next_arguments"] is None:
                break
            opened = self.service.open_brain(**opened["next_arguments"])
            pages.append(opened)
        else:
            self.fail("bounded review did not complete")
        return pages

    def test_three_real_wakes_complete_without_shell_or_all_brain_manual(self):
        body, candidate = self.large_candidate()
        review_wake = self.wake("synthetic-page-review")
        with mock.patch.object(self.service, "content_schema", side_effect=AssertionError("No large schema")), \
             mock.patch("subprocess.run", side_effect=AssertionError("No workspace")):
            first = self.service.open_brain(view="review")
            self.assertFalse(first["review_material_presented"])
            self.assertEqual(0, self.proof_count("candidate_full_review", review_wake))
            denied = self.advance(review_wake, "accept_candidate_review", {"ai_confirmation": True})
            self.assertIn("candidate_full_review_required", denied["reason_codes"])
            pages = self.read_all(first)
        self.assertGreater(len(pages), 1)
        self.assertTrue(pages[-1]["review_material_presented"])
        self.assertEqual(1, self.proof_count("candidate_full_review", review_wake))
        self.assertEqual(1, len({p["write_context_ref"] for p in pages}))
        material = json.loads("".join(p["review_page"]["text"] for p in pages))
        self.assertEqual(body, material["candidate"]["content"])
        for page in pages:
            self.assertLess(len(json.dumps(page, ensure_ascii=False)), 8500)
            self.assertFalse(page["workspace_required"])
            rendered = json.dumps(page)
            for forbidden in ("saved_artifacts", "module_one_content_schema", "wake_capability", "submitted_wake_id"):
                self.assertNotIn(forbidden, rendered)
        accepted = self.service.submit_self_model_candidate(
            intent="accept_review", write_context_ref=pages[-1]["write_context_ref"],
            expected_row_version=pages[-1]["row_version"], payload={"ai_confirmation": True},
        )
        self.assertEqual("review_accepted", accepted["decision"])
        same_wake = self.advance(review_wake, "activate_candidate", {
            "candidate_id": candidate["candidate_id"], "expected_active_revision": None,
            "ai_confirmation": True,
        })
        self.assertIn("review_activation_wake_boundary_required", same_wake["reason_codes"])
        active_wake = self.wake("synthetic-page-activate")
        activation_pages = self.read_all()
        self.assertEqual(1, self.proof_count("candidate_full_review", active_wake))
        active = self.service.activate_self_model_candidate(
            candidate_id=candidate["candidate_id"],
            write_context_ref=activation_pages[-1]["write_context_ref"],
            expected_row_version=activation_pages[-1]["row_version"],
            expected_active_revision=None, ai_confirmation=True,
        )
        self.assertEqual("activate", active["decision"])
        baseline = self.service.query_self_model(view="edit_basis")["result"]
        self.assertEqual(body, baseline["active"]["content"])
        before = copy.deepcopy(self.service.module_one_status()["state"])
        events = self.service.store.list_events(self.service.model_id)
        with mock.patch.object(self.service.store, "build_injection", side_effect=AssertionError("Read is not injection")):
            self.assertTrue(self.service.query_self_model(view="active")["result"]["active_available"])
        self.assertEqual(before, self.service.module_one_status()["state"])
        self.assertEqual(events, self.service.store.list_events(self.service.model_id))

    def test_bad_page_or_hash_does_not_open_or_advance(self):
        self.large_candidate()
        wake = self.wake("synthetic-invalid-page")
        state = copy.deepcopy(self.service.module_one_status()["state"])
        for kwargs in ({"page": 1}, {"page": 0, "expected_material_hash": "0" * 64},
                       {"page": 999, "expected_material_hash": "0" * 64}):
            denied = self.service.open_brain(view="review", **kwargs)
            self.assertFalse(denied["write_context_available"])
            self.assertNotIn("review_page", denied)
            self.assertEqual(state, self.service.module_one_status()["state"])
            self.assertEqual(0, self.proof_count("brain_manual_opened", wake))
            self.assertEqual(0, self.proof_count("candidate_full_review", wake))

    def test_new_objection_invalidates_old_material_pages(self):
        _, candidate = self.large_candidate()
        wake = self.wake("synthetic-objection-change")
        first = self.service.open_brain(view="review")
        self.onboarding.record_human_objection(
            owner_id=self.service.owner_id, model_id=self.service.model_id,
            candidate_id=candidate["candidate_id"], reason="Synthetic additional objection.",
            release_condition="Synthetic independent response required.", actor_id="human:synthetic",
            request_id="synthetic-page-objection",
        )
        denied = self.service.open_brain(**first["next_arguments"])
        self.assertFalse(denied["write_context_available"])
        self.assertEqual(0, self.proof_count("candidate_full_review", wake))
        self.assertEqual(0, self.proof_count("human_objection_presented", wake))
        pages = self.read_all()
        self.assertTrue(pages[-1]["review_material_presented"])
        material = json.loads("".join(p["review_page"]["text"] for p in pages))
        self.assertIn("human_objection", material)
        self.assertEqual(1, self.proof_count("human_objection_presented", wake))
        rejected = self.advance(wake, "accept_candidate_review", {"ai_confirmation": True})
        self.assertIn("human_objection_pending", rejected["reason_codes"])

    def test_old_wake_pages_and_out_of_order_tail_are_not_complete(self):
        self.large_candidate()
        first_wake = self.wake("synthetic-old-pages")
        first = self.service.open_brain(view="review")
        self.assertEqual(0, self.proof_count("candidate_full_review", first_wake))
        current = self.wake("synthetic-current-pages")
        tail = self.service.open_brain(view="review", page=first["review_page"]["pages"] - 1,
                                      expected_material_hash=first["review_page"]["material_hash"])
        self.assertFalse(tail["review_material_presented"])
        self.assertIsNotNone(tail["next_arguments"])
        self.assertEqual(0, self.proof_count("candidate_full_review", current))
        pages = self.read_all(self.service.open_brain(**tail["next_arguments"]))
        self.assertTrue(pages[-1]["review_material_presented"])


if __name__ == "__main__":
    unittest.main()
