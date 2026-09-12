"""Real synthetic pre-generation frames: recall before activation, capture after.

All rows live in a new TemporaryDirectory. Direct ephemeral inserts below are
fixture preparation only, not claims that public pre-activation writes succeed.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime.emotional_memory import EmotionalMemoryStore
from runtime.onboarding import ModuleOneOnboardingStore
from tests import test_onboarding as fixture


class ReadOnlyEphemeralCaptureTests(unittest.TestCase):
    # Reuse the real three-wake activation fixture, not its test methods or a
    # synthetic 'complete' flag. No MCP/server import or service is involved.
    wake = fixture.ModuleOneOnboardingTests.wake
    advance = fixture.ModuleOneOnboardingTests.advance
    open_brain = fixture.ModuleOneOnboardingTests.open_brain
    candidate_payload = staticmethod(fixture.ModuleOneOnboardingTests.candidate_payload)
    bootstrap_to_wait = fixture.ModuleOneOnboardingTests.bootstrap_to_wait
    bootstrap_live = fixture.ModuleOneOnboardingTests.bootstrap_live

    def setUp(self):
        for target in ("socket.socket", "socket.create_connection"):
            self.enterContext(patch(target, side_effect=AssertionError("offline test")))
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="readonly-ephemeral-synthetic-"))
        self.database = Path(directory) / "synthetic.db"
        self.owner, self.model = "owner:synthetic-capture", "model:synthetic-capture"
        self.emotional = EmotionalMemoryStore(self.database)
        self.store = ModuleOneOnboardingStore(
            self.database, capability_secret=b"synthetic-capture-secret-at-least-32-bytes",
            emotional_store=self.emotional, ordinary_memory_independent=True,
        )
        self.store.ensure_state(owner_id=self.owner, model_id=self.model)
        self.emotional.ensure_state(owner_id=self.owner, model_id=self.model)

    def rows(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT ephemeral_id,role,content,content_hash,status,expires_at,cleared_at "
                "FROM emotion_ephemeral ORDER BY ephemeral_id"
            ).fetchall()

    def seed_existing(self, text="Synthetic existing continuity."):
        result = self.emotional.capture_ephemeral(
            owner_id=self.owner, model_id=self.model, thread_id="thread:test",
            source_event_id="fixture-existing", items=[{"role": "assistant", "content": text}],
        )
        self.assertEqual(1, result["inserted"])
        return text

    def frame(self, event, *, items=None, query="Synthetic continuity", stable=True):
        wake = self.store.issue_wake(
            owner_id=self.owner, model_id=self.model, host_id="host:synthetic",
            thread_id="thread:test", source_kind="human_message", source_event_id=event,
        )
        frame = {
            "query_text": query, "thread_id": "thread:test" if stable else None,
            "lineage_stable": stable, "source_event_id": event,
            "capture_items": items if items is not None else [
                {"role": "user", "content": "Synthetic current message " + event}],
        }
        result = self.store.build_pre_generation_context(
            owner_id=self.owner, model_id=self.model, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], source_digest="synthetic:" + event,
            host_contract_digest="synthetic:host", source_frame=frame,
        )
        self.assertEqual("context_prepared", result["decision"])
        return result

    def test_first_activation_pending_stable_frame_does_not_capture_content(self):
        with patch.object(self.emotional, "capture_ephemeral", wraps=self.emotional.capture_ephemeral) as capture:
            result = self.frame("before-first-activation")
        capture.assert_not_called()
        self.assertEqual([], self.rows())
        self.assertFalse(self.store.state(owner_id=self.owner, model_id=self.model)["module_one_unlocked"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM brain_context_snapshots").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM emotion_memories").fetchone()[0])
        self.assertNotIn("Synthetic current message", result["message"]["content"])

    def test_existing_content_can_recall_without_new_capture_or_budget_clearing(self):
        original = self.seed_existing()
        before = self.rows()
        # Far exceeds the 600-token thread budget if capture is accidentally run.
        items = [{"role": "user", "content": ("合成新内容" + str(index)) * 240} for index in range(4)]
        with patch.object(self.emotional, "capture_ephemeral", wraps=self.emotional.capture_ephemeral) as capture:
            result = self.frame("read-existing", items=items)
        capture.assert_not_called()
        self.assertEqual(before, self.rows())
        self.assertIn(original, result["message"]["content"])
        self.assertNotIn("合成新内容", result["message"]["content"])

    def test_submitted_real_candidate_is_not_first_activation(self):
        self.bootstrap_to_wait()
        self.assertEqual("candidate_wait", self.store.state(owner_id=self.owner, model_id=self.model)["state"]["stage"])
        with patch.object(self.emotional, "capture_ephemeral", wraps=self.emotional.capture_ephemeral) as capture:
            self.frame("waiting-candidate")
        capture.assert_not_called()
        self.assertEqual([], self.rows())
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM self_model_revisions").fetchone()[0])

    def test_real_three_wake_activation_allows_capture_after_recall(self):
        self.bootstrap_live()
        original = self.seed_existing()
        new_text = "Synthetic newly captured message after actual activation."
        with patch.object(self.emotional, "capture_ephemeral", wraps=self.emotional.capture_ephemeral) as capture:
            result = self.frame("after-activation", items=[{"role": "user", "content": new_text}])
        capture.assert_called_once()
        self.assertEqual(2, len(self.rows()))
        self.assertIn(new_text, [row[2] for row in self.rows()])
        self.assertIn(original, result["message"]["content"])
        self.assertNotIn(new_text, result["message"]["content"])

    def test_real_edit_candidate_keeps_activated_basis_and_capture(self):
        revision = self.bootstrap_live()
        wake, _ = self.wake("start-real-edit")
        challenge = self.advance(wake, "begin_edit")
        self.advance(wake, "confirm_edit", {
            "challenge_id": challenge["challenge_id"], "challenge_response": challenge["challenge_response"],
        })
        submitted = self.advance(wake, "submit_candidate", self.candidate_payload("synthetic-edit", expected=revision))
        self.assertEqual("pending", submitted["decision"])
        state = self.store.state(owner_id=self.owner, model_id=self.model)
        self.assertEqual(("edit", "candidate_wait"), (state["state"]["flow_kind"], state["state"]["stage"]))
        self.assertTrue(state["module_one_unlocked"])
        with patch.object(self.emotional, "capture_ephemeral", wraps=self.emotional.capture_ephemeral) as capture:
            self.frame("during-real-edit")
        capture.assert_called_once()
        self.assertEqual(1, len(self.rows()))

    def test_unlock_flag_alone_does_not_establish_capture_permission(self):
        # Corrupt only this test fixture; never a real installation.
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE brain_module_unlocks SET unlocked=1 WHERE module_name='module_one'")
            connection.commit()
        with patch.object(self.emotional, "capture_ephemeral", wraps=self.emotional.capture_ephemeral) as capture:
            self.frame("flag-only")
        capture.assert_not_called()
        self.assertEqual([], self.rows())

    def test_existing_ttl_privacy_cleanup_and_recall_audit_are_preserved(self):
        self.seed_existing()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE emotion_ephemeral SET expires_at='2000-01-01T00:00:00+00:00'")
            connection.commit()
        with patch.object(self.emotional, "capture_ephemeral", wraps=self.emotional.capture_ephemeral) as capture:
            result = self.frame("existing-ttl-expired")
        capture.assert_not_called()
        rows = self.rows()
        self.assertEqual(1, len(rows))
        self.assertIsNone(rows[0][2])
        self.assertEqual("expired", rows[0][4])
        self.assertNotIn("Synthetic existing continuity", result["message"]["content"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertGreater(connection.execute(
                "SELECT COUNT(*) FROM emotion_audit_events WHERE action='automatic_recall'"
            ).fetchone()[0], 0)

    def test_unstable_lineage_still_cannot_capture_after_activation(self):
        self.bootstrap_live()
        with patch.object(self.emotional, "capture_ephemeral", wraps=self.emotional.capture_ephemeral) as capture:
            self.frame("unstable-lineage", stable=False)
        capture.assert_not_called()
        self.assertEqual([], self.rows())


if __name__ == "__main__":
    unittest.main()
