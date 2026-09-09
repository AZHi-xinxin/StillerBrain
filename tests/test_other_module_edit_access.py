"""A pending core edit must not revoke an already-completed memory foundation.

All data is synthetic and created by the existing real onboarding fixture in a
temporary directory. No production state or host credentials are used.
"""
from __future__ import annotations

import copy
import unittest

class OtherModuleEditAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        # Avoid unittest discovering the imported helper TestCase a second time.
        from tests.test_onboarding_acceptance import ModuleOneAcceptanceMatrixTests
        self.fixture = ModuleOneAcceptanceMatrixTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.owner = self.fixture.owner
        self.model = self.fixture.model

    def gate(self, module: str = "planning_memory") -> dict:
        return self.store.authorize_other_module_write(
            owner_id=self.owner, model_id=self.model, module_name=module
        )

    def assert_allowed_without_mutation(self) -> None:
        before = self.store.state(owner_id=self.owner, model_id=self.model)
        pointer = self.store.self_store.active_revision(self.model)
        candidates = self.store.self_store.list_candidates(self.model)
        for module in ("emotional_memory", "learning_memory", "tool_guidance", "planning_memory"):
            with self.subTest(module=module):
                result = self.gate(module)
                self.assertEqual("allowed", result["decision"])
                self.assertFalse(result["state_changed"])
                self.assertFalse(result["pointer_changed"])
                self.assertEqual(pointer["revision_id"], result["basis_revision_id"])
        self.assertEqual(before, self.store.state(owner_id=self.owner, model_id=self.model))
        self.assertEqual(pointer, self.store.self_store.active_revision(self.model))
        self.assertEqual(candidates, self.store.self_store.list_candidates(self.model))

    def edit(self) -> tuple[str, dict, dict]:
        revision = self.fixture.bootstrap_live()
        wake, _ = self.fixture.wake("edit-memory-coexistence")
        begun = self.fixture.advance(wake, "begin_edit")
        self.assertEqual("challenge_issued", begun["decision"])
        return revision, wake, begun

    def confirm(self, wake: dict, begun: dict) -> None:
        result = self.fixture.advance(wake, "confirm_edit", {
            "challenge_id": begun["challenge_id"],
            "challenge_response": begun["challenge_response"],
        })
        self.assertEqual("edit_body_draft", result["state"]["stage"])

    def test_existing_permissions_survive_consent_draft_and_scope_rejection(self) -> None:
        revision, wake, begun = self.edit()
        self.assert_allowed_without_mutation()
        self.confirm(wake, begun)
        self.assert_allowed_without_mutation()
        payload = copy.deepcopy(self.fixture.candidate_payload("v2", revision))
        payload["reason"] = "Synthetic MCP boundary rejection"
        rejected = self.fixture.advance(wake, "submit_candidate", payload)
        self.assertIn("scope_boundary_violation", rejected["reason_codes"])
        self.assertFalse(rejected["content_persisted"])
        self.assertEqual("edit_body_draft", self.store.state(
            owner_id=self.owner, model_id=self.model
        )["state"]["stage"])
        self.assert_allowed_without_mutation()

    def test_edit_candidate_wait_and_review_keep_old_basis_without_early_activation(self) -> None:
        revision, wake, begun = self.edit()
        self.confirm(wake, begun)
        submitted = self.fixture.advance(
            wake, "submit_candidate", self.fixture.candidate_payload("v2", revision)
        )
        self.assertEqual("candidate_wait", submitted["state"]["stage"])
        self.assert_allowed_without_mutation()
        early = self.fixture.advance(wake, "accept_candidate_review", {"ai_confirmation": True})
        self.assertIn("same_wake_activation_forbidden", early["reason_codes"])
        review_wake, _ = self.fixture.wake("edit-later-review")
        self.fixture.open_brain()
        self.assert_allowed_without_mutation()
        accepted = self.fixture.advance(review_wake, "accept_candidate_review", {"ai_confirmation": True})
        self.assertEqual("review_accepted", accepted["decision"])
        self.assert_allowed_without_mutation()
        early_activation = self.fixture.advance(review_wake, "activate_candidate", {
            "expected_active_revision": revision, "ai_confirmation": True,
        })
        self.assertIn("review_activation_wake_boundary_required", early_activation["reason_codes"])
        self.assertEqual(revision, self.store.self_store.active_revision(self.model)["revision_id"])

    def test_cancel_is_nondestructive_existing_recovery_path(self) -> None:
        revision, wake, begun = self.edit()
        self.confirm(wake, begun)
        candidates = self.store.self_store.list_candidates(self.model)
        cancelled = self.fixture.advance(wake, "cancel_edit")
        self.assertEqual("cancelled", cancelled["decision"])
        self.assertEqual("live", cancelled["state"]["stage"])
        self.assertFalse(cancelled["pointer_changed"])
        self.assertEqual(revision, self.store.self_store.active_revision(self.model)["revision_id"])
        self.assertEqual(candidates, self.store.self_store.list_candidates(self.model))
        self.assert_allowed_without_mutation()

    def test_initial_onboarding_wait_and_review_do_not_unlock_other_modules(self) -> None:
        self.assertEqual("module_one_required", self.gate()["decision"])
        self.fixture.bootstrap_to_wait()
        self.assertEqual("module_one_required", self.gate()["decision"])
        self.fixture.wake("initial-review")
        self.fixture.open_brain()
        self.assertEqual("module_one_required", self.gate()["decision"])

    def test_edit_rejects_missing_unlock_incomplete_and_isolated_or_recovery_state(self) -> None:
        _, wake, begun = self.edit()
        self.confirm(wake, begun)
        cases = (
            ("brain_onboarding_state", "module_one_status", "in_progress"),
            ("brain_onboarding_state", "injection_policy", "isolated_legacy"),
            ("brain_onboarding_state", "injection_policy", "disabled"),
            ("brain_onboarding_state", "flow_kind", "onboarding"),
            ("brain_onboarding_state", "stage", "body_draft"),
            ("brain_onboarding_state", "stage", "draft_only_recovery"),
            ("brain_onboarding_state", "base_revision_id", None),
            ("brain_onboarding_state", "base_revision_id", "rev-wrong-base"),
            ("brain_module_unlocks", "unlocked", 0),
            ("brain_module_unlocks", "basis_revision_id", None),
            ("brain_module_unlocks", "basis_revision_id", "rev-wrong-unlock-basis"),
        )
        for table, column, bad_value in cases:
            with self.subTest(table=table, column=column, value=bad_value):
                with self.store._connect() as connection:
                    original = connection.execute(
                        f"SELECT {column} FROM {table} WHERE owner_id=? AND model_id=?",
                        (self.owner, self.model),
                    ).fetchone()[column]
                    connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE owner_id=? AND model_id=?",
                        (bad_value, self.owner, self.model),
                    )
                try:
                    result = self.gate()
                    self.assertEqual("module_one_required", result["decision"])
                    self.assertFalse(result["state_changed"])
                    self.assertFalse(result["pointer_changed"])
                finally:
                    with self.store._connect() as connection:
                        connection.execute(
                            f"UPDATE {table} SET {column}=? WHERE owner_id=? AND model_id=?",
                            (original, self.owner, self.model),
                        )
        self.assert_allowed_without_mutation()

    def test_edit_requires_an_existing_unlock_row_and_same_model_revision(self) -> None:
        revision, wake, begun = self.edit()
        self.confirm(wake, begun)
        with self.store._connect() as connection:
            columns = [row["name"] for row in connection.execute("PRAGMA table_info(brain_module_unlocks)")]
            saved_unlock = tuple(connection.execute(
                "SELECT * FROM brain_module_unlocks WHERE owner_id=? AND model_id=?",
                (self.owner, self.model),
            ).fetchone())
            connection.execute("DELETE FROM brain_module_unlocks WHERE owner_id=? AND model_id=?", (self.owner, self.model))
        self.assertEqual("module_one_required", self.gate()["decision"])
        with self.store._connect() as connection:
            connection.execute(
                f"INSERT INTO brain_module_unlocks ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                saved_unlock,
            )
        self.store.ensure_state(owner_id=self.owner, model_id="model:other-synthetic")
        with self.store._connect() as connection:
            connection.execute("UPDATE self_model_revisions SET model_id=? WHERE revision_id=?", ("model:other-synthetic", revision))
        try:
            self.assertEqual("module_one_required", self.gate()["decision"])
        finally:
            with self.store._connect() as connection:
                connection.execute("UPDATE self_model_revisions SET model_id=? WHERE revision_id=?", (self.model, revision))
        self.assert_allowed_without_mutation()

    def test_edit_cannot_use_absent_active_pointer_or_other_owner(self) -> None:
        revision, wake, begun = self.edit()
        self.confirm(wake, begun)
        for column, bad, original in (
            ("active_revision_id", None, revision),
            ("active_revision_id", "rev-nonexistent", revision),
            ("owner_id", "owner:unrelated", self.owner),
        ):
            with self.subTest(column=column, value=bad):
                with self.store._connect() as connection:
                    connection.execute(f"UPDATE self_models SET {column}=? WHERE model_id=?", (bad, self.model))
                try:
                    self.assertEqual("module_one_required", self.gate()["decision"])
                finally:
                    with self.store._connect() as connection:
                        connection.execute(f"UPDATE self_models SET {column}=? WHERE model_id=?", (original, self.model))


if __name__ == "__main__":
    unittest.main()
