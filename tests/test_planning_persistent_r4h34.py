"""Synthetic candidate-layer coverage; no live databases or network access."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime.planning_memory import PlanningMemoryError, PlanningMemoryStore
from tests import test_planning_memory as fixtures


class PlanningPersistentCandidateTests(unittest.TestCase):
    key = fixtures.PlanningMemoryStoreTests.key
    propose = fixtures.PlanningMemoryStoreTests.propose
    accept = fixtures.PlanningMemoryStoreTests.accept
    create = fixtures.PlanningMemoryStoreTests.create
    record = fixtures.PlanningMemoryStoreTests.record

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="st-persistent-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.database = (Path(self.temp.name) / "planning.sqlite3").resolve()
        original_connect = sqlite3.connect

        def synthetic_connect(database: object, *args: object, **kwargs: object):
            if Path(str(database)).resolve() != self.database:
                raise AssertionError("only_this_synthetic_database_is_allowed")
            return original_connect(database, *args, **kwargs)

        database_guard = patch("sqlite3.connect", side_effect=synthetic_connect)
        database_guard.start()
        self.addCleanup(database_guard.stop)
        for target in ("socket.socket", "socket.create_connection"):
            guard = patch(target, side_effect=AssertionError("network_disabled"))
            guard.start()
            self.addCleanup(guard.stop)
        self.store = PlanningMemoryStore(self.database)
        self.store.ensure_state(owner_id=fixtures.OWNER, model_id=fixtures.MODEL)
        self.wake_seq = 0
        self.key_seq = 0

    def preview(self, query: str = "", **options: object) -> dict[str, object]:
        # The exercised candidate and next-action paths must work with writes
        # prohibited; compare the entire synthetic database, not just row_version.
        with self.store._connect() as connection:
            connection.execute("PRAGMA query_only = ON")
            before = tuple(connection.iterdump())
            result = self.store.build_injection(
                owner_id=options.pop("owner_id", fixtures.OWNER),
                model_id=options.pop("model_id", fixtures.MODEL),
                query=query,
                connection=connection,
                **options,
            )
            self.assertEqual(before, tuple(connection.iterdump()))
        self.assertIs(result["state_changed"], False)
        self.assertIs(result["active_plan_changed"], False)
        self.assertLessEqual(len(result["envelopes"]), 3)
        return result

    @staticmethod
    def plan_id(accepted: dict[str, object]) -> str:
        return str(accepted["plan_ref"]).split("//", 1)[1].split("@", 1)[0]

    @staticmethod
    def refs(result: dict[str, object]) -> list[str]:
        return [item["item_ref"] for item in result["envelopes"]]

    def retire(self, accepted: dict[str, object], intent: str) -> None:
        self.wake_seq += 1
        pending = self.store.propose_revision(
            owner_id=fixtures.OWNER,
            model_id=fixtures.MODEL,
            wake_id=f"wake-{self.wake_seq}",
            wake_seq=self.wake_seq,
            expected_row_version=self.store.status(
                owner_id=fixtures.OWNER, model_id=fixtures.MODEL
            )["row_version"],
            plan_id=self.plan_id(accepted),
            expected_plan_version=1,
            intent=intent,
            reason="我选择结束这项合成计划。",
            calm_check=fixtures.calm(),
            ai_confirmation=True,
            idempotency_key=self.key(intent),
        )
        self.accept(pending)

    def test_active_persistent_surfaces_without_keywords_or_session_start(self) -> None:
        accepted = self.create(fixtures.content("合成常驻", presence_mode="persistent"))
        for query in ("", "zzzzzzzzzz"):
            for session_start in (False, True):
                with self.subTest(query_empty=not query, session_start=session_start):
                    result = self.preview(query, session_start=session_start)
                    self.assertEqual([accepted["plan_ref"]], self.refs(result))
                    item = result["envelopes"][0]
                    self.assertEqual(0, item["selection_priority"])
                    self.assertEqual(0.0, item["semantic_score"])

    def test_two_persistent_precede_start_and_high_relevance_candidates(self) -> None:
        first = self.create(fixtures.content("常驻甲", presence_mode="persistent"))
        second = self.create(fixtures.content("常驻乙", presence_mode="persistent"))
        starts = [self.create(fixtures.content(label, presence_mode="session_start"))
                  for label in ("开场甲", "开场乙")]
        self.create(fixtures.content("zzzzzzzzzz", track="relational"))
        result = self.preview("zzzzzzzzzz", session_start=True)
        self.assertEqual(sorted([first["plan_ref"], second["plan_ref"]]), self.refs(result)[:2])
        self.assertIn(self.refs(result)[2], {item["plan_ref"] for item in starts})
        self.assertEqual([0, 0, 1], [item["selection_priority"] for item in result["envelopes"]])
        self.assertEqual(5, result["candidate_count"])
        self.assertIs(result["truncated"], True)

    def test_session_start_and_relevance_semantics_are_preserved(self) -> None:
        starts = [self.create(fixtures.content(label, presence_mode="session_start"))
                  for label in ("开场甲", "开场乙")]
        self.assertEqual([], self.refs(self.preview(session_start=False)))
        result = self.preview(session_start=True)
        self.assertEqual(1, len(result["envelopes"]))
        self.assertEqual(1, result["envelopes"][0]["selection_priority"])
        # Outside the opening, a real lexical match keeps the legacy ordinary
        # relevance path; presence_mode does not become a query prohibition.
        relevant = self.preview("开场甲", session_start=False)
        self.assertIn(starts[0]["plan_ref"], self.refs(relevant))
        self.assertTrue(all(item["selection_priority"] == 2 for item in relevant["envelopes"]))

    def test_explicit_limit_and_total_limit_are_not_overridden(self) -> None:
        self.create(fixtures.content("常驻甲", presence_mode="persistent"))
        self.create(fixtures.content("常驻乙", presence_mode="persistent"))
        for limit in (1, 2, 3):
            with self.subTest(limit=limit):
                self.assertEqual(min(limit, 2), len(self.preview(limit=limit)["envelopes"]))
        for invalid in (True, False, 0, 4, 1.0):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(PlanningMemoryError, "invalid_limit"):
                    self.preview(limit=invalid)

    def test_paused_persistent_requires_relevance_and_resume_restores_presence(self) -> None:
        accepted = self.create(fixtures.content("合成暂停", presence_mode="persistent"))
        self.record(accepted, "pause")
        self.assertEqual([], self.refs(self.preview("zzzzzzzzzz", session_start=True)))
        matched = self.preview("合成暂停")
        self.assertEqual([accepted["plan_ref"]], self.refs(matched))
        self.assertEqual("paused", matched["envelopes"][0]["state"])
        self.assertEqual(2, matched["envelopes"][0]["selection_priority"])
        self.record(accepted, "resume")
        self.assertEqual(0, self.preview()["envelopes"][0]["selection_priority"])

    def test_paused_persistent_can_surface_for_review_without_becoming_reserved(self) -> None:
        fields = fixtures.content("合成复核", presence_mode="persistent")
        fields["review_after"] = "2000-01-01T00:00:00Z"
        accepted = self.create(fields)
        self.record(accepted, "pause")
        result = self.preview()
        self.assertEqual([accepted["plan_ref"]], self.refs(result))
        self.assertEqual(2, result["envelopes"][0]["selection_priority"])
        self.assertIsNotNone(result["injection"]["review_hint"])
        quiet = self.preview(recent_nudge_refs=[accepted["plan_ref"]])
        self.assertEqual([accepted["plan_ref"]], self.refs(quiet))
        self.assertIsNone(quiet["injection"]["review_hint"])

    def test_deadline_alone_does_not_unpause_persistent(self) -> None:
        fields = fixtures.content("合成到期", kind="commitment", presence_mode="persistent")
        fields["due_at"] = "2000-01-01T00:00:00Z"
        accepted = self.create(fields)
        self.record(accepted, "pause")
        self.assertEqual([], self.refs(self.preview(session_start=True)))

    def test_all_terminal_states_remain_excluded(self) -> None:
        completed = self.create(fixtures.content("合成完成", presence_mode="persistent"))
        self.record(completed, "complete", event_evidence=fixtures.evidence())
        abandoned = self.create(fixtures.content("合成放弃", presence_mode="persistent"))
        self.retire(abandoned, "abandon")
        archived = self.create(fixtures.content("合成归档", presence_mode="persistent"))
        self.record(archived, "pause")
        self.retire(archived, "archive")
        for query in ("", "合成完成", "合成放弃", "合成归档"):
            with self.subTest(query_empty=not query):
                self.assertEqual([], self.refs(self.preview(query, session_start=True)))

    def test_pending_candidate_is_not_a_persistent_memory(self) -> None:
        self.propose(fixtures.content("合成待审", presence_mode="persistent"))
        self.assertEqual([], self.refs(self.preview("合成待审", session_start=True)))

    def test_owner_model_and_explicit_exclusion_remain_effective(self) -> None:
        accepted = self.create(fixtures.content("合成隔离", presence_mode="persistent"))
        for options in ({"owner_id": "other-synthetic-owner"},
                        {"model_id": "other-synthetic-model"},
                        {"excluded_plan_ids": [self.plan_id(accepted)]}):
            with self.subTest(boundary=next(iter(options))):
                result = self.preview("合成隔离", session_start=True, **options)
                self.assertEqual([], self.refs(result))
                self.assertIsNone(result["injection"])

    def test_relational_persistent_does_not_gain_internal_presence(self) -> None:
        accepted = self.create(fixtures.content(
            "合成关系", track="relational", presence_mode="persistent"
        ))
        self.assertEqual([], self.refs(self.preview(session_start=True)))
        result = self.preview("合成关系")
        self.assertEqual([accepted["plan_ref"]], self.refs(result))
        self.assertEqual(2, result["envelopes"][0]["selection_priority"])

    def test_two_slot_write_guard_still_counts_paused_persistent(self) -> None:
        first = self.create(fixtures.content("合成占位甲", presence_mode="persistent"))
        self.record(first, "pause")
        self.create(fixtures.content("合成占位乙", presence_mode="persistent"))
        with self.assertRaisesRegex(PlanningMemoryError, "persistent_plan_limit"):
            self.propose(fixtures.content("合成占位丙", presence_mode="persistent"))

    def test_mechanical_metadata_copies_reminder_without_original_or_authority(self) -> None:
        fields = fixtures.content("合成机械投影", presence_mode="persistent")
        fields["reminder"] = "我选择保留这一步 🌱：A → B"
        fields["original_text"] = "SYNTHETIC_ORIGINAL_MUST_NOT_BE_INJECTED"
        self.create(fields)
        first = self.preview()
        self.assertEqual(first, self.preview())
        item = first["envelopes"][0]
        self.assertEqual(fields["reminder"], item["reminder"])
        self.assertEqual("persistent", item["presence_mode"])
        self.assertIs(type(item["selection_priority"]), int)
        self.assertIs(type(item["selection_score"]), float)
        self.assertAlmostEqual(0.14, item["selection_score"])
        self.assertEqual("summary_only", item["presentation"])
        self.assertNotIn(fields["original_text"], json.dumps(first, ensure_ascii=False))
        self.assertEqual("none", item["frame"]["instruction_authority"])
        self.assertEqual("none", item["frame"]["permission_authority"])
        self.assertIs(item["frame"]["optional"], True)
        self.assertIs(item["frame"]["mutates_plan"], False)


if __name__ == "__main__":
    unittest.main()
