"""Offline synthetic continuity and authenticated-delivery regressions."""
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from runtime import session_continuity as c
from runtime.onboarding import _estimate_tokens
from tests import test_context_layout as layout_fixture
from tests import test_emotional_memory as emotional_fixture


def frame(source="A", model="route-one"):
    return {"source_key": c._hash(source), "public_model": "synthetic-public", "upstream_model": model}


def item(label, version=1, updated="2026-10-18T00:00:00Z", size=10):
    ref = f"emotion://synthetic-{label}@{version}"
    return {"ref_hash": c._hash(ref), "updated": updated,
            "envelope": {"item_ref": ref, "summary": "合成" * size, "module": "emotional_memory"}}


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        c.initialize(self.db)
        self.seq = 0

    def prepare(self, source="A", items=(), base=None, **kw):
        self.seq += 1
        wake = "wake-" + str(self.seq)
        result = c.prepare(self.db, owner_id="owner", model_id="identity", wake_id=wake,
            wake_seq=self.seq, frame=frame(source, kw.pop("model", "route-one")), items=list(items),
            base=base or {}, estimate=_estimate_tokens, **kw)
        return wake, result.get("session_continuity", {})

    def ack(self, wake):
        return c.acknowledge(self.db, owner_id="owner", model_id="identity", wake_id=wake)

    def refs(self, result):
        return [entry["item_ref"] for entry in result["recent_memories"]]

    def test_prepare_and_cancel_do_not_acknowledge_or_switch_source(self):
        a, _ = self.prepare(items=[item("old")]); self.ack(a)
        b, first = self.prepare("B", [item("old"), item("new")])
        retry, second = self.prepare("B", [item("old"), item("new")])
        self.assertEqual("changed", first["source_transition"])
        self.assertEqual(self.refs(first), self.refs(second))
        self.assertEqual([item("new")["envelope"]["item_ref"]], self.refs(second))
        self.assertEqual(c._hash("A"), self.db.execute("SELECT source_key FROM continuity_heads").fetchone()[0])

    def test_same_window_no_replay_and_return_has_only_new_versions(self):
        a, _ = self.prepare(items=[item("old")]); self.ack(a)
        b, result = self.prepare("B", [item("old"), item("new")]); self.ack(b)
        _, same = self.prepare("B", [item("old"), item("new")])
        self.assertEqual([], self.refs(same))
        a2, result = self.prepare("A", [item("old"), item("new")]); self.ack(a2)
        self.assertEqual(1, len(self.refs(result)))
        _, back = self.prepare("B", [item("old"), item("new"), item("later")])
        self.assertEqual([item("later")["envelope"]["item_ref"]], self.refs(back))

    def test_same_second_new_version_is_not_lost(self):
        a, _ = self.prepare(items=[item("x")]); self.ack(a)
        _, result = self.prepare("B", [item("x", 2)])
        self.assertEqual([item("x", 2)["envelope"]["item_ref"]], self.refs(result))

    def test_same_window_has_no_extra_library_scan(self):
        self.assertTrue(c.needs_candidates(self.db, "owner", "identity", frame()))
        a, _ = self.prepare(items=[item("x")]); self.ack(a)
        self.assertFalse(c.needs_candidates(self.db, "owner", "identity", frame()))
        self.assertTrue(c.needs_candidates(self.db, "owner", "identity", frame("B")))

    def test_latest_three_not_relevance_or_importance(self):
        values = [item(str(i), updated=f"2026-10-18T00:00:0{i}Z") for i in range(6)]
        _, result = self.prepare(items=values)
        self.assertEqual([values[i]["envelope"]["item_ref"] for i in (5, 4, 3)], self.refs(result))

    def test_budget_omission_is_pending_after_success_then_delivered_once(self):
        wake, result = self.prepare(items=[item("pending")], budget=1)
        self.assertEqual({}, result)
        self.ack(wake)
        retry, result = self.prepare(items=[item("pending")])
        self.assertEqual(1, len(self.refs(result)))
        self.ack(retry)
        _, result = self.prepare(items=[item("pending")])
        self.assertEqual([], self.refs(result))

    def test_removed_or_restricted_pending_is_not_replayed(self):
        wake, _ = self.prepare(items=[item("removed")], budget=1); self.ack(wake)
        _, result = self.prepare(items=[])
        self.assertEqual([], self.refs(result))

    def test_late_and_duplicate_ack_cannot_regress_state(self):
        a, _ = self.prepare(); b, _ = self.prepare("B")
        self.assertTrue(self.ack(b))
        self.assertFalse(self.ack(a)); self.assertFalse(self.ack(b))
        self.assertEqual(c._hash("B"), self.db.execute("SELECT source_key FROM continuity_heads").fetchone()[0])

    def test_model_change_is_observed_route_not_identity(self):
        a, first = self.prepare(); self.ack(a)
        _, second = self.prepare(model="route-two")
        self.assertEqual("unknown", first["model_transition"])
        self.assertEqual("changed", second["model_transition"])
        self.assertEqual("same", second["source_transition"])
        self.assertIn("not_weight_attestation", second["model_identity_basis"])

    def test_other_owner_isolated(self):
        a, _ = self.prepare(items=[item("owner-one")]); self.ack(a)
        other = c.prepare(self.db, owner_id="other", model_id="identity", wake_id="other-wake",
            wake_seq=1, frame=frame(), items=[], base={}, estimate=_estimate_tokens)
        self.assertEqual("unknown", other["session_continuity"]["source_transition"])
        self.assertEqual([], other["session_continuity"]["recent_memories"])

    def test_cursor_contains_no_memory_text_or_raw_reference(self):
        a, _ = self.prepare(items=[item("private-reference")]); self.ack(a)
        for table in ("continuity_versions", "continuity_heads", "continuity_windows", "continuity_proposals"):
            text = repr([tuple(row) for row in self.db.execute("SELECT * FROM " + table)])
            self.assertNotIn("private-reference", text)
            self.assertNotIn("合成", text)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = emotional_fixture.EmotionalMemoryTests(methodName="runTest")
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)

    def project(self, query="unrelated", enabled=True):
        f = self.fixture
        store = SimpleNamespace(emotional_store=f.store, learning_store=None, tool_store=None, planning_store=None)
        with f.store._connect() as db:
            return c.candidates(store, db, f.owner, f.model, query, lambda _: enabled)

    def test_recency_ignores_similarity_but_never_expands_original(self):
        self.fixture.remember("RECENCY", original_text="DO_NOT_COPY_RAW_ORIGINAL")
        result = self.project()
        self.assertEqual(1, len(result))
        self.assertNotIn("DO_NOT_COPY_RAW_ORIGINAL", json.dumps(result))

    def test_privacy_policy_and_switch_gates(self):
        for kwargs in ({"sensitivity": "restricted"}, {"sensitivity": "intimate"},
                       {"context_policy": "ask_first"}, {"context_policy": "neutral_hint"},
                       {"recall_mode": "never"}, {"default_decision": "defer"},
                       {"allow_contexts": ["allowed"]}, {"deny_contexts": ["unrelated"]}):
            self.fixture.remember("restricted-test", **kwargs)
        self.assertEqual([], self.project())
        self.fixture.remember("ordinary-test")
        self.assertEqual([], self.project(enabled=False))

    def test_summary_cannot_smuggle_entire_original(self):
        self.fixture.remember("raw-copy", original_text="合成原文不可通过摘要放出。",
                              summary="摘要：合成原文不可通过摘要放出。")
        self.assertEqual([], self.project())

    def test_short_stored_summary_equal_to_original_is_not_discarded(self):
        text = "Synthetic ordinary note about lunch."
        saved = self.fixture.remember("short", original_text=text, summary=text)
        result = self.project()
        self.assertEqual(1, len(result))
        self.assertEqual(text, result[0]["envelope"]["summary"])
        self.assertEqual(saved["memory"]["updated_at"], result[0]["envelope"]["updated_at"])
        self.assertNotIn("original_text", result[0]["envelope"])

    def test_short_equal_summary_still_obeys_all_privacy_and_context_gates(self):
        text = "Synthetic short note."
        for kwargs in ({"sensitivity": "restricted"}, {"sensitivity": "intimate"},
                       {"context_policy": "ask_first"}, {"context_policy": "neutral_hint"},
                       {"recall_mode": "never"}, {"recall_mode": "summary_only"},
                       {"default_decision": "defer"}, {"allow_contexts": ["allowed"]},
                       {"deny_contexts": ["unrelated"]}):
            self.fixture.remember("short-private", original_text=text, summary=text, **kwargs)
        self.assertEqual([], self.project())
        self.fixture.remember("short-ordinary", original_text=text, summary=text)
        self.assertEqual([], self.project(enabled=False))

    def test_short_summary_exception_does_not_use_lossy_normalization(self):
        self.fixture.remember("punctuation", original_text="A-B", summary="AB")
        self.assertEqual([], self.project())


class DeliveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.base = layout_fixture.ContextLayoutTests(methodName="runTest")
        self.base.setUp(); self.addCleanup(self.base.doCleanups)
        self.base.store.ordinary_memory_independent = True
        self.wake = self.base.issue()
        self.frame = {"query_text": "synthetic", "thread_id": "thread:layout", "lineage_stable": True,
                      "source_event_id": "layout-test", "capture_items": [], "continuity": frame()}

    def ack(self, **changes):
        args = {**self.base.scope, "wake_id": self.wake["wake_id"],
                "wake_capability": self.wake["wake_capability"], "context_hash": self.result["context_hash"], **changes}
        return self.base.store.acknowledge_context_delivery(**args)

    def test_prepare_confirm_and_close_do_not_ack_only_completed_receipt_does(self):
        self.result = self.base.prepare(self.wake, source_frame=self.frame)
        self.assertTrue(self.result["continuity_delivery_receipt"])
        self.assertEqual("delivery_rejected", self.ack()["decision"])
        self.base.confirm(self.wake, self.result["context_hash"])
        self.assertEqual("delivery_rejected", self.ack()["decision"])
        self.base.store.close_context_snapshot(**self.base.scope, wake_id=self.wake["wake_id"],
            wake_capability=self.wake["wake_capability"])
        self.assertEqual(0, self.base.fixture.count("continuity_heads"))
        self.assertEqual("delivery_rejected", self.ack(wake_capability="wrong")["decision"])
        self.assertEqual("delivery_rejected", self.ack(context_hash="wrong")["decision"])
        self.assertTrue(self.ack()["cursor_changed"])
        self.assertFalse(self.ack()["cursor_changed"])

    def test_snapshot_reuse_is_frozen_and_receipt_negotiated(self):
        self.result = self.base.prepare(self.wake, source_frame=self.frame)
        reused = self.base.prepare(self.wake, source_frame=self.frame)
        self.assertEqual(self.result["context_hash"], reused["context_hash"])
        self.assertTrue(reused["continuity_delivery_receipt"])
        self.assertEqual(1, self.base.fixture.count("continuity_proposals"))

    def test_legacy_without_source_is_unchanged(self):
        result = self.base.prepare(self.wake)
        self.assertFalse(result["continuity_delivery_receipt"])
        self.assertNotIn("session_continuity", result["message"]["content"])


if __name__ == "__main__":
    unittest.main()
