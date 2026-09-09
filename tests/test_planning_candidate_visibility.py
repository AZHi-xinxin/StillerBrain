"""Offline regressions for the bounded, fair planning-review batch (B3)."""

from __future__ import annotations

from pathlib import Path
import tempfile
from typing import Any
import unittest

from mcp_server.planning_service import PlanningMemoryAccessService
from runtime.planning_memory import PlanningMemoryError, PlanningMemoryStore
from tests.test_planning_memory import calm, content


OWNER = "visibility-owner"
MODEL = "visibility-model"


class ScopedOnboarding:
    """Synthetic binding only; no live onboarding or stored credentials."""

    def __init__(self) -> None:
        self.allowed = True
        self.scopes = {"planning_memory"}
        self.write_context_ref = "offline-visibility-context"
        self.wake_id = "wake-2"
        self.wake_seq = 2

    def authorize_other_module_write(self, **fields: Any) -> dict[str, Any]:
        allowed = self.allowed and (fields["owner_id"], fields["model_id"]) == (
            OWNER,
            MODEL,
        )
        return {"decision": "allowed" if allowed else "reject"}

    def current_open_write_context(self, **fields: Any) -> dict[str, Any]:
        return {
            "write_context_available": (
                fields["write_context_ref"] == self.write_context_ref
                and (fields["owner_id"], fields["model_id"]) == (OWNER, MODEL)
                and fields["required_scope"] in self.scopes
            ),
            "wake_id": self.wake_id,
            "wake_seq": self.wake_seq,
        }

    def contains_protected_persistence_value(self, **_fields: Any) -> bool:
        return False


class PlanningCandidateVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "visibility.sqlite3"
        self.store = PlanningMemoryStore(self.database)
        self.key_seq = 0

    def propose(
        self,
        *,
        wake_seq: int = 1,
        owner_id: str = OWNER,
        model_id: str = MODEL,
    ) -> dict[str, Any]:
        self.key_seq += 1
        return self.store.propose_create(
            owner_id=owner_id,
            model_id=model_id,
            wake_id=f"wake-{wake_seq}",
            wake_seq=wake_seq,
            expected_row_version=self.store.status(owner_id=owner_id, model_id=model_id)[
                "row_version"
            ],
            content=content(f"离线候选 {self.key_seq}"),
            reason="我选择保存此离线测试计划。",
            calm_check=calm(),
            ai_confirmation=True,
            idempotency_key=f"visibility-{self.key_seq}",
        )

    def present(self, wake_seq: int, **fields: Any) -> list[dict[str, Any]]:
        return self.store.present_pending_candidates(
            **{
                "owner_id": OWNER,
                "model_id": MODEL,
                "wake_id": f"wake-{wake_seq}",
                "wake_seq": wake_seq,
                **fields,
            }
        )

    def review(
        self, candidate: dict[str, Any], wake_seq: int, **fields: Any
    ) -> dict[str, Any]:
        return self.store.review_change(
            **{
                "owner_id": OWNER,
                "model_id": MODEL,
                "wake_id": f"wake-{wake_seq}",
                "wake_seq": wake_seq,
                "expected_row_version": self.store.status(owner_id=OWNER, model_id=MODEL)[
                    "row_version"
                ],
                "candidate_id": candidate["candidate_id"],
                "expected_candidate_version": candidate["candidate_version"],
                "expected_candidate_hash": candidate["candidate_hash"],
                "expected_base_version": candidate["base_version"],
                "decision": "accept",
                "correctness_assessment": "我已独立核对完整内容与差异。",
                "calm_check": calm(),
                "reason": "我在较晚唤醒中独立作出选择。",
                "ai_confirmation": True,
                **fields,
            }
        )

    def candidate_rows(self) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM planning_change_candidates ORDER BY created_at, candidate_id"
                ).fetchall()
            ]

    def semantic_snapshot(self) -> dict[str, Any]:
        ignored = {"presented_wake_id", "presented_wake_seq", "updated_at"}
        snapshot: dict[str, Any] = {
            "candidates": [
                {key: value for key, value in row.items() if key not in ignored}
                for row in self.candidate_rows()
            ]
        }
        with self.store._connect() as connection:
            for table in (
                "planning_module_state",
                "planning_items",
                "planning_versions",
                "planning_edges",
                "planning_events",
                "planning_audit_events",
                "planning_idempotency_records",
            ):
                snapshot[table] = [
                    dict(row)
                    for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
                ]
        return snapshot

    @staticmethod
    def ids(candidates: list[dict[str, Any]]) -> list[str]:
        return [candidate["candidate_id"] for candidate in candidates]

    def service(self) -> tuple[PlanningMemoryAccessService, ScopedOnboarding]:
        onboarding = ScopedOnboarding()
        return (
            PlanningMemoryAccessService(
                self.store,
                onboarding=onboarding,  # type: ignore[arg-type]
                owner_id=OWNER,
                model_id=MODEL,
            ),
            onboarding,
        )

    def test_seven_pending_rotate_without_mutating_candidates_or_active_plans(self) -> None:
        active_candidate = self.propose()
        self.present(2)
        self.review(active_candidate, 2)
        pending = [self.propose(wake_seq=3) for _ in range(7)]
        before = self.semantic_snapshot()
        status = self.store.status(owner_id=OWNER, model_id=MODEL)
        seen: set[str] = set()
        for wake_seq in (4, 5, 6):
            batch = self.present(wake_seq)
            self.assertEqual(3, len(batch))
            self.assertTrue(all(candidate["fully_presented"] for candidate in batch))
            seen.update(self.ids(batch))
            rows_after_first_open = self.candidate_rows()
            for _ in range(8):
                self.assertEqual(batch, self.present(wake_seq))
                self.assertEqual(rows_after_first_open, self.candidate_rows())
            self.assertEqual(before, self.semantic_snapshot())
            self.assertEqual(status, self.store.status(owner_id=OWNER, model_id=MODEL))
        self.assertEqual(set(self.ids(pending)), seen)
        self.assertEqual(7, status["counts"]["pending_changes"])
        self.assertEqual(1, status["counts"]["active"])

    def test_fifth_candidate_reachable_without_approving_or_rejecting_blockers(self) -> None:
        pending = [self.propose() for _ in range(5)]
        fifth = pending[4]
        self.assertTrue(all(not item["fully_presented"] for item in self.present(1)))
        with self.assertRaisesRegex(PlanningMemoryError, "later_real_wake_required"):
            self.review(fifth, 1)
        first_batch = self.present(2)
        self.assertNotIn(fifth["candidate_id"], self.ids(first_batch))
        with self.assertRaisesRegex(PlanningMemoryError, "candidate_not_fully_presented"):
            self.review(fifth, 2)
        second_batch = self.present(3)
        visible_fifth = next(
            item for item in second_batch if item["candidate_id"] == fifth["candidate_id"]
        )
        self.assertTrue(visible_fifth["fully_presented"])
        self.assertIn("proposed_content", visible_fifth)
        self.assertIn("canonical_diff", visible_fifth)
        self.assertEqual(fifth["candidate_hash"], visible_fifth["candidate_hash"])

        for fields, error in (
            ({"expected_candidate_hash": "0" * 64}, "candidate_hash_mismatch"),
            ({"expected_candidate_version": 99}, "candidate_version_mismatch"),
            ({"expected_base_version": 99}, "candidate_base_mismatch"),
            ({"expected_row_version": 0}, "planning_row_version_conflict"),
        ):
            with self.subTest(error=error):
                before = self.semantic_snapshot()
                with self.assertRaisesRegex(PlanningMemoryError, error):
                    self.review(visible_fifth, 3, **fields)
                self.assertEqual(before, self.semantic_snapshot())

        # A body/hash from an earlier wake still cannot authorize a new wake.
        with self.assertRaisesRegex(PlanningMemoryError, "candidate_not_fully_presented"):
            self.review(visible_fifth, 4)
        accepted = self.review(visible_fifth, 3)
        self.assertEqual("candidate_accepted", accepted["decision"])
        states = {row["candidate_id"]: row["status"] for row in self.candidate_rows()}
        self.assertEqual("accepted", states[fifth["candidate_id"]])
        self.assertTrue(all(states[item["candidate_id"]] == "pending" for item in pending[:4]))

    def test_same_wake_new_candidates_do_not_displace_reviewable_old_candidates(self) -> None:
        old = [self.propose() for _ in range(3)]
        self.present(2)
        new = [self.propose(wake_seq=3) for _ in range(5)]
        batch = self.present(3)
        self.assertEqual(self.ids(old), self.ids(batch))
        self.assertTrue(all(item["fully_presented"] for item in batch))
        for item in new:
            with self.assertRaisesRegex(PlanningMemoryError, "later_real_wake_required"):
                self.review(item, 3)
        self.propose(wake_seq=3)
        for _ in range(8):
            self.assertEqual(batch, self.present(3))
        next_batch = self.present(4)
        self.assertTrue(set(self.ids(next_batch)).issubset(set(self.ids(new))))
        self.assertTrue(all(item["fully_presented"] for item in next_batch))

    def test_owner_and_model_isolation_in_rotation_and_review(self) -> None:
        mine = [self.propose() for _ in range(5)]
        other = [self.propose(owner_id="other-owner") for _ in range(5)]
        other_model = [self.propose(model_id="other-model") for _ in range(5)]
        foreign_ids = set(self.ids(other + other_model))
        foreign_before = [
            row for row in self.candidate_rows() if row["candidate_id"] in foreign_ids
        ]
        for wake_seq in (2, 3, 4):
            self.assertTrue(set(self.ids(self.present(wake_seq))).issubset(self.ids(mine)))
        self.assertEqual(
            foreign_before,
            [row for row in self.candidate_rows() if row["candidate_id"] in foreign_ids],
        )
        for candidate in (other[0], other_model[0]):
            with self.assertRaisesRegex(PlanningMemoryError, "candidate_not_found"):
                self.review(candidate, 4)

    def test_rotation_and_same_wake_batch_survive_store_recreation(self) -> None:
        pending = [self.propose() for _ in range(7)]
        first = self.present(2)
        self.store = PlanningMemoryStore(self.database)
        self.assertEqual(first, self.present(2))
        second = self.present(3)
        self.assertFalse(set(self.ids(first)) & set(self.ids(second)))
        self.store = PlanningMemoryStore(self.database)
        self.assertEqual(second, self.present(3))
        third = self.present(4)
        self.assertEqual(set(self.ids(pending)), set(self.ids(first + second + third)))
        # This batch mixes old and never-seen candidates; its paths must stay stable.
        self.assertEqual(third, self.present(4))

    def test_limit_remains_bounded(self) -> None:
        for _ in range(13):
            self.propose()
        self.assertEqual(3, len(self.present(2)))
        self.assertEqual(10, len(self.present(3, limit=10)))
        for invalid in (0, -1, 11, True, 3.0, "3"):
            with self.subTest(limit=invalid):
                with self.assertRaisesRegex(PlanningMemoryError, "invalid_limit"):
                    self.present(4, limit=invalid)

    def test_manual_reports_batch_counts_and_only_current_full_review_calls(self) -> None:
        pending = [self.propose() for _ in range(5)]
        service, onboarding = self.service()
        first = service.manual(write_context_ref=onboarding.write_context_ref)
        self.assertEqual(5, first["pending_count"])
        self.assertEqual(
            {
                "limit": 3,
                "shown_count": 3,
                "not_shown_count": 2,
                "selection": "wake_stable_least_recently_presented",
                "advance_requires_later_real_wake": True,
            },
            first["pending_batch"],
        )
        for _ in range(8):
            self.assertEqual(first, service.manual(write_context_ref=onboarding.write_context_ref))
        onboarding.wake_id, onboarding.wake_seq = "wake-3", 3
        second = service.manual(write_context_ref=onboarding.write_context_ref)
        self.assertTrue(set(self.ids(pending[3:])).issubset(self.ids(second["pending_changes"])))
        for index, call in enumerate(second["current_action_contract"]["allowed_calls"]):
            candidate = second["pending_changes"][index]
            self.assertTrue(candidate["fully_presented"])
            self.assertEqual(candidate["candidate_id"], call["fixed_arguments"]["candidate_id"])
            self.assertEqual(
                f"$.planning_memory.pending_changes[{index}].candidate_hash",
                call["argument_sources"]["expected_candidate_hash"],
            )
        self.assertEqual([], second["current_action_contract"]["blocked_candidates"])

    def test_manual_requires_valid_planning_scope_before_presenting(self) -> None:
        for _ in range(5):
            self.propose()
        service, onboarding = self.service()
        before = self.candidate_rows()
        no_context = service.manual()
        self.assertEqual([], no_context["pending_changes"])
        self.assertEqual(5, no_context["pending_batch"]["not_shown_count"])
        for mode in ("invalid-context", "wrong-scope", "module-locked"):
            with self.subTest(mode=mode):
                onboarding.allowed = mode != "module-locked"
                onboarding.scopes = set() if mode == "wrong-scope" else {"planning_memory"}
                reference = "offline-invalid" if mode == "invalid-context" else onboarding.write_context_ref
                manual = service.manual(write_context_ref=reference)
                self.assertEqual([], manual["pending_changes"])
                self.assertEqual([], manual["current_action_contract"]["allowed_calls"])
                self.assertEqual(
                    ["module_one_required" if mode == "module-locked" else "brain_open_required"],
                    manual["reason_codes"],
                )
                self.assertEqual(before, self.candidate_rows())


if __name__ == "__main__":
    unittest.main()
