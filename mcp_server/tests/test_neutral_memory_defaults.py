"""Synthetic ordinary source/score/type defaults, persistence and recall."""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from mcp_server.daily_revision_service import DailyRevisionAccessService
from mcp_server.tests import test_daily_memory_service as fixture
from runtime.emotional_memory import EmotionalMemoryError
from runtime.learning_memory import LearningMemoryError


class NeutralMemoryDefaultsTests(unittest.TestCase):
    setUp = fixture.DailyMemoryServiceTests.setUp
    rows = fixture.DailyMemoryServiceTests.rows
    counts = fixture.DailyMemoryServiceTests.counts

    def _content(self, module, item_id):
        if module == "emotional_memory":
            return next(row for row in self.rows("emotion_memories") if row["memory_id"] == item_id)
        row = next(row for row in self.rows("learning_items") if row["learning_id"] == item_id)
        return json.loads(row["current_json"])

    def _revise(self, ref, changes):
        service = DailyRevisionAccessService(
            self.host, self.emotional, self.learning, self.planning, fixture.OWNER, fixture.MODEL,
        )
        self.host.mode = "human_attested_direct"
        with patch("mcp_server.daily_revision_service.current_execution_claim", return_value=None):
            result = service.revise(ref, changes, write_context_ref=self.host.ref)
        self.host.mode = "gateway_injected"
        self.assertEqual(result["decision"], "revised", result)
        return result

    def test_omitted_values_are_unmarked_null_and_emotion_unclassified(self):
        for module in ("emotional_memory", "learning_memory"):
            result = self.daily.remember(module, "Synthetic preserved original.")
            self.assertTrue(result["stored"], result)
            row = self._content(module, result["id"])
            self.assertIsNone(row["confidence"])
            self.assertEqual(row["origin" if module == "emotional_memory" else "source_basis"], "unmarked")
            if module == "emotional_memory":
                self.assertEqual(row["memory_type"], "unclassified")
            else:
                self.assertEqual(row["epistemic_status"], "unmarked")
                self.assertEqual(row["provenance_badge"], "unmarked")
                self.assertEqual(row["claim_review"], {"status": "ordinary"})
                self.assertEqual(row["kind"], "fact")

    def test_explicit_sources_and_zero_fifty_hundred_are_preserved(self):
        for module in ("emotional_memory", "learning_memory"):
            for source in ("observed", "reported", "inferred", "unmarked"):
                for score in (0, 50, 100, None):
                    result = self.daily.remember(module, f"Synthetic {source} {score}.",
                                                 source_basis=source, confidence=score)
                    self.assertTrue(result["stored"], result)
                    row = self._content(module, result["id"])
                    expected = "firsthand" if module == "emotional_memory" and source == "observed" else source
                    self.assertEqual(row["origin" if module == "emotional_memory" else "source_basis"], expected)
                    self.assertEqual(row["confidence"], score)

    def test_explicit_type_is_authored_and_bad_values_remain_rejected(self):
        saved = self.daily.remember("emotional_memory", "Synthetic classified memory.", kind="shared_event")
        self.assertEqual(self._content("emotional_memory", saved["id"])["memory_type"], "shared_event")
        for score in (-1, 101, True, "50", 1.5):
            with self.subTest(score=score):
                result = self.daily.remember("learning_memory", "Synthetic.", confidence=score)
                self.assertFalse(result["stored"])
        for fields in ({"source_basis": "guessed"}, {"kind": "guessed"}):
            self.assertFalse(self.daily.remember("emotional_memory", "Synthetic.", **fields)["stored"])

    def test_changes_null_clears_and_omission_preserves_score_and_history(self):
        for module in ("emotional_memory", "learning_memory"):
            saved = self.daily.remember(module, "Synthetic original.", source_basis="observed", confidence=100)
            changed = self._revise(saved["ref"], {"summary": "Synthetic revised summary."})
            row = self._content(module, saved["id"])
            self.assertEqual(row["confidence"], 100)
            source_field = "origin" if module == "emotional_memory" else "source_basis"
            cleared = self._revise(changed["ref"], {source_field: "unmarked", "confidence": None})
            row = self._content(module, saved["id"])
            self.assertIsNone(row["confidence"])
            self.assertEqual(row[source_field], "unmarked")
            with closing(sqlite3.connect(self.database)) as db:
                table, key = ("emotion_memory_versions", "memory_id") if module == "emotional_memory" else ("learning_versions", "learning_id")
                versions = [json.loads(v[0]) for v in db.execute(
                    f"SELECT mutable_json FROM {table} WHERE {key}=? ORDER BY version", (saved["id"],))]
            self.assertEqual([v["confidence"] for v in versions], [100, 100, None])
            self.assertEqual(cleared["version"], 3)

    def test_unmarked_learning_score_survives_rollback(self):
        saved = self.daily.remember("learning_memory", "Synthetic rollback original.")
        changed = self._revise(saved["ref"], {"source_basis": "observed", "confidence": 100})
        result = self.learning.store.revise(
            owner_id=fixture.OWNER, model_id=fixture.MODEL, wake_id=self.host.wake_id,
            wake_seq=self.host.wake_seq, expected_row_version=self.learning.status()["row_version"],
            target_ref=changed["ref"], action="rollback", rollback_to_version=1, reason="Synthetic rollback.",
        )
        self.assertEqual(result["decision"], "applied")
        row = self._content("learning_memory", saved["id"])
        self.assertIsNone(row["confidence"])
        self.assertEqual(row["source_basis"], "unmarked")

    def test_unmarked_read_and_auto_recall_are_not_low_score(self):
        cue = "Synthetic neutral memory spotlight"
        emotion = self.daily.remember("emotional_memory", cue, keywords=[cue], importance=80)
        learning = self.daily.remember("learning_memory", cue, keywords=[cue], importance=80)
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory = sqlite3.Row
            erow = db.execute("SELECT * FROM emotion_memories WHERE memory_id=?", (emotion["id"],)).fetchone()
            lrow = db.execute("SELECT * FROM learning_items WHERE learning_id=?", (learning["id"],)).fetchone()
            ep = self.emotional.store._public_memory(erow, include_original=True)
            lp = self.learning.store._public_item(lrow)
        self.assertEqual(ep["origin_label"], "来源未标注（置信度 未标注）")
        self.assertIn("未标注", lp["knowledge_axes"]["source_confidence_label"])
        injection = self.emotional.store.build_injection(owner_id=fixture.OWNER, model_id=fixture.MODEL, query=cue)
        self.assertTrue(injection["injection"]["memories"])
        self.assertIsNone(injection["injection"]["memories"][0]["confidence"])
        envelopes = self.learning.store.build_envelopes(owner_id=fixture.OWNER, model_id=fixture.MODEL, query=cue)
        self.assertTrue(envelopes)
        self.assertIsNone(envelopes[0]["confidence"])
        self.assertNotIn("low_confidence_neutral_hint", envelopes[0]["reason_codes"])
        self.assertEqual(envelopes[0]["gate_decision"], "background_reference")
        inventory = self.learning.store.inventory(owner_id=fixture.OWNER, model_id=fixture.MODEL)
        self.assertIsNone(inventory["items"][0]["confidence"])

    def test_reported_without_score_is_not_implicitly_low_confidence(self):
        cue = "Synthetic reported unscored lesson"
        self.daily.remember("learning_memory", cue, source_basis="reported", keywords=[cue])
        envelopes = self.learning.store.build_envelopes(owner_id=fixture.OWNER, model_id=fixture.MODEL, query=cue)
        self.assertTrue(envelopes)
        self.assertIsNone(envelopes[0]["confidence"])
        self.assertNotIn("low_confidence_neutral_hint", envelopes[0]["reason_codes"])

    def test_advanced_emotion_defaults_match_ordinary_defaults(self):
        saved = self.emotional.remember(
            write_context_ref=self.host.ref, expected_emotion_version=self.emotional.status()["row_version"],
            memory_type="feeling", original_text="Synthetic advanced emotion.",
            summary="Synthetic summary.", primary_emotion="joy", reason="Synthetic reason.",
        )
        self.assertEqual(saved["decision"], "stored")
        self.assertEqual(saved["memory"]["origin"], "unmarked")
        self.assertIsNone(saved["memory"]["confidence"])

    def test_integration_keeps_unmarked_score_and_source_versions(self):
        emotional = [self.daily.remember("emotional_memory", f"Synthetic emotion source {i}.") for i in range(2)]
        combined = self.emotional.store.integrate(
            owner_id=fixture.OWNER, model_id=fixture.MODEL, wake_id=self.host.wake_id,
            expected_row_version=self.emotional.status()["row_version"],
            source_memory_ids=[item["id"] for item in emotional],
            original_text="Synthetic combined emotion.", summary="Synthetic combined summary.",
            primary_emotion="joy", reason="Synthetic integration.",
        )
        self.assertIsNone(combined["aggregate_memory"]["confidence"])
        self.assertEqual(combined["aggregate_memory"]["origin"], "unmarked")
        learning = [self.daily.remember("learning_memory", f"Synthetic lesson source {i}.") for i in range(2)]
        combined_learning = self.learning.store.integrate(
            owner_id=fixture.OWNER, model_id=fixture.MODEL, wake_id=self.host.wake_id,
            wake_seq=self.host.wake_seq, expected_row_version=self.learning.status()["row_version"],
            source_learning_ids=[item["ref"] for item in learning], synthesis_kind="summary",
            kind="fact", title="Synthetic combined lesson", summary="Synthetic combined summary.",
            current_understanding="Synthetic combined knowledge.", source_basis="unmarked", confidence=None,
            claim_review={"status": "ordinary"}, reason="Synthetic integration.",
        )
        row = self._content("learning_memory", combined_learning["learning_id"])
        self.assertIsNone(row["confidence"])
        self.assertEqual(row["source_basis"], "unmarked")


if __name__ == "__main__":
    unittest.main()
