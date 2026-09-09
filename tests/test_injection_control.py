from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from runtime.injection_control import InjectionControlError, InjectionControlStore


class InjectionControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = InjectionControlStore(Path(self.temp.name) / "brain.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_defaults_and_emergency_brake_are_content_free_and_idempotent(self) -> None:
        store = self.store
        status = store.status(owner_id="owner-a", model_id="model-a")
        self.assertEqual(status["global_mode"], "enabled")
        self.assertEqual(status["scopes"]["hallucination_vault"]["mode"], "hard_off")
        self.assertTrue(status["storage_enabled"])
        self.assertTrue(status["explicit_query_enabled"])

        first = store.emergency_off(
        owner_id="owner-a",
        model_id="model-a",
        reason="我现在选择先停止自动注入。",
        wake_id="wake-1",
        wake_seq=1,
        expected_row_version=0,
    )
        self.assertEqual(first["decision"], "hard_off")
        self.assertEqual(first["takes_effect"], "next_real_wake")
        self.assertTrue(first["current_wake_is_not_retroactively_changed"])

        second = store.emergency_off(
        owner_id="owner-a",
        model_id="model-a",
        reason="我再次确认保持关闭。",
        wake_id="wake-1",
        wake_seq=1,
        expected_row_version=0,
    )
        self.assertEqual(second["decision"], "already_hard_off")
        self.assertFalse(second["state_changed"])
        self.assertEqual(second["row_version"], 1)
        self.assertEqual(
            store.effective_mode(
                owner_id="owner-a", model_id="model-a", scope="learning_memory"
            ),
            "hard_off",
        )


    def test_recovery_requires_candidate_and_later_wake(self) -> None:
        store = self.store
        stopped = store.emergency_off(
        owner_id="owner-a",
        model_id="model-a",
        reason="我先停下自动注入。",
        wake_id="wake-1",
        wake_seq=1,
        expected_row_version=0,
    )
        candidate = store.propose_mode(
        owner_id="owner-a",
        model_id="model-a",
        scope="global",
        target_mode="enabled",
        reason="我复核后愿意重新开启。",
        wake_id="wake-2",
        wake_seq=2,
        expected_row_version=stopped["row_version"],
        expected_active_revision=stopped["revision_id"],
    )
        self.assertEqual(candidate["decision"], "candidate_pending")

        with self.assertRaisesRegex(InjectionControlError, "later_real_wake_required"):
            store.activate_candidate(
            owner_id="owner-a",
            model_id="model-a",
            scope="global",
            candidate_id=candidate["candidate_id"],
            expected_candidate_hash=candidate["candidate_hash"],
            expected_active_revision=stopped["revision_id"],
            expected_row_version=candidate["row_version"],
            wake_id="wake-2",
            wake_seq=2,
            ai_confirmation=True,
        )

        activated = store.activate_candidate(
        owner_id="owner-a",
        model_id="model-a",
        scope="global",
        candidate_id=candidate["candidate_id"],
        expected_candidate_hash=candidate["candidate_hash"],
        expected_active_revision=stopped["revision_id"],
        expected_row_version=candidate["row_version"],
        wake_id="wake-3",
        wake_seq=3,
        ai_confirmation=True,
    )
        self.assertEqual(activated["decision"], "activated")
        self.assertEqual(activated["mode"], "enabled")


    def test_scope_isolation_status_only_and_cas(self) -> None:
        store = self.store
        with self.assertRaisesRegex(
            InjectionControlError,
            "status_only_reserved_for_hallucination_vault",
        ):
            store.propose_mode(
            owner_id="owner-a",
            model_id="model-a",
            scope="learning_memory",
            target_mode="status_only",
            reason="我想试试。",
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
            expected_active_revision=None,
        )

        candidate = store.propose_mode(
        owner_id="owner-a",
        model_id="model-a",
        scope="hallucination_vault",
        target_mode="status_only",
        reason="我只愿意看到内容无关的状态。",
        wake_id="wake-1",
        wake_seq=1,
        expected_row_version=0,
        expected_active_revision=None,
    )
        self.assertEqual(candidate["target_mode"], "status_only")

        with self.assertRaisesRegex(InjectionControlError, "injection_control_version_conflict"):
            store.propose_mode(
            owner_id="owner-a",
            model_id="model-a",
            scope="hallucination_vault",
            target_mode="hard_off",
            reason="过期版本不应成功。",
            wake_id="wake-2",
            wake_seq=2,
            expected_row_version=0,
            expected_active_revision=None,
        )

        other = store.status(owner_id="owner-b", model_id="model-a")
        self.assertEqual(other["scopes"]["hallucination_vault"]["pending_candidates"], [])


    def test_secret_reason_is_rejected_without_state_change(self) -> None:
        store = self.store
        with self.assertRaisesRegex(InjectionControlError, "credential_or_secret_detected"):
            store.emergency_off(
            owner_id="owner-a",
            model_id="model-a",
            reason="token=abcdefghijklmnopqrstuvwxyz123456",
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
        )
        self.assertEqual(
            store.status(owner_id="owner-a", model_id="model-a")["global_mode"],
            "enabled",
        )


if __name__ == "__main__":
    unittest.main()
