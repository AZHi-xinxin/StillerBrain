"""Direct configuration revisions, with legacy and core boundaries intact."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from runtime.self_governance import SelfGovernanceError, SelfGovernanceStore
from runtime.injection_control import InjectionControlError, InjectionControlStore


def content(text: str = "我自行选择本次范围的提示。") -> dict:
    return {"schema_version": "0.1.0", "text": text,
            "trigger_mode": "manual_only", "scene_tags": []}


class DirectGovernanceInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "brain.db"
        self.gov = SelfGovernanceStore(self.database)
        self.inj = InjectionControlStore(self.database)
        self.binding = dict(owner_id="owner:test", model_id="model:test", wake_id="wake:1", wake_seq=1)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def gov_commit(self, **overrides) -> dict:
        args = dict(self.binding, scope="learning_memory", operation="set", content=content(),
                    expected_row_version=0, expected_active_revision=None)
        args.update(overrides)
        return self.gov.commit_revision(**args)

    def inj_commit(self, **overrides) -> dict:
        args = dict(self.binding, scope="learning_memory", operation="set", target_mode="paused",
                    expected_row_version=0, expected_active_revision=None)
        args.update(overrides)
        return self.inj.commit_revision(**args)

    def query(self, sql: str, args: tuple = ()) -> list:
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(sql, args).fetchall()

    def test_governance_direct_set_clear_rollback_same_wake_no_fake_candidate(self) -> None:
        first = self.gov_commit()
        second = self.gov_commit(operation="clear", content=None, expected_row_version=1,
                                 expected_active_revision=first["revision_id"])
        third = self.gov_commit(operation="rollback", content=None, expected_row_version=2,
                                expected_active_revision=second["revision_id"],
                                target_revision_id=first["revision_id"])
        self.assertEqual([1, 2, 3], [first["row_version"], second["row_version"], third["row_version"]])
        self.assertTrue(third["state_changed"])
        self.assertFalse(third["candidate_created"])
        self.assertFalse(third["review_performed"])
        self.assertFalse(third["external_permission_changed"])
        self.assertEqual([(0,)], self.query("SELECT COUNT(*) FROM self_governance_candidates"))
        revisions = self.query("SELECT operation,content_json,parent_revision_id,rollback_of_revision_id "
                               "FROM self_governance_revisions ORDER BY revision_number")
        self.assertEqual(["set", "clear", "rollback"], [row[0] for row in revisions])
        self.assertEqual(content(), json.loads(revisions[0][1]))
        self.assertIsNone(revisions[1][1])
        self.assertEqual(revisions[0][1], revisions[2][1])
        self.assertEqual(first["revision_id"], revisions[1][2])
        self.assertEqual(first["revision_id"], revisions[2][3])
        event = json.loads(self.query("SELECT details_json FROM self_governance_events ORDER BY event_seq LIMIT 1")[0][0])
        self.assertIsNone(event["reason_hash"])
        self.assertFalse(event["review_performed"])

    def test_all_governance_profiles_accept_ai_chosen_person_and_direct_versions(self) -> None:
        for scope in ("global", "self_revision", "emotional_memory", "learning_memory", "tool_use"):
            first = self.gov_commit(scope=scope, content=content("该 AI 自主选择这条边界。"))
            second = self.gov_commit(scope=scope, operation="clear", content=None,
                expected_row_version=1, expected_active_revision=first["revision_id"])
            third = self.gov_commit(scope=scope, operation="rollback", content=None,
                expected_row_version=2, expected_active_revision=second["revision_id"],
                target_revision_id=first["revision_id"])
            self.assertEqual("committed", third["decision"])
        self.assertEqual([(0,)], self.query("SELECT COUNT(*) FROM self_governance_candidates"))
        with self.assertRaisesRegex(SelfGovernanceError, "invalid_governance_scope"):
            self.gov_commit(scope="planning_memory")

    def test_governance_cas_exact_active_and_rollback_scope_are_atomic(self) -> None:
        first = self.gov_commit()
        for overrides, error in (
            ({}, "governance_version_conflict"),
            ({"expected_row_version": 1}, "active_governance_revision_conflict"),
            ({"expected_row_version": True}, "expected_row_version_required"),
            ({"owner_id": "owner:other", "operation": "rollback", "content": None,
              "target_revision_id": first["revision_id"]}, "rollback_target_not_found"),
            ({"scope": "tool_use", "operation": "rollback", "content": None,
              "target_revision_id": first["revision_id"]}, "rollback_target_not_found"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(SelfGovernanceError, error):
                self.gov_commit(**overrides)
        self.assertEqual([(1,)], self.query("SELECT COUNT(*) FROM self_governance_revisions"))
        self.assertEqual([(1,)], self.query("SELECT COUNT(*) FROM self_governance_scope_state"))

    def test_governance_keeps_existing_pending_unchanged(self) -> None:
        pending = self.gov.propose_candidate(**self.binding, scope="learning_memory", operation="set",
            content=content("我保留原来的候选。"), reason="我提出候选。", expected_row_version=0, expected_active_revision=None)
        self.gov_commit(expected_row_version=1)
        self.assertEqual([("pending",)], self.query("SELECT status FROM self_governance_candidates WHERE candidate_id=?",
                                                    (pending["candidate_id"],)))
        self.assertEqual([(1,)], self.query("SELECT COUNT(*) FROM self_governance_revisions"))

    def test_direct_authorship_secrets_invalid_payload_do_not_write(self) -> None:
        for override, error in (({"actor": "human"}, "ai_authorship_required"),
                                ({"reason": "token=synthetic-test-only-value"}, "credential_or_secret_detected"),
                                ({"wake_seq": 0}, "invalid_wake_seq")):
            with self.subTest(override=override), self.assertRaisesRegex((SelfGovernanceError, InjectionControlError), error):
                self.gov_commit(**override)
            with self.subTest(override=override), self.assertRaisesRegex(InjectionControlError, error):
                self.inj_commit(**override)
        with self.assertRaisesRegex(SelfGovernanceError, "target_revision_not_allowed"):
            self.gov_commit(target_revision_id="foreign")
        with self.assertRaisesRegex(InjectionControlError, "target_revision_only_for_rollback"):
            self.inj_commit(target_revision_id="foreign")
        self.assertEqual([(0,)], self.query("SELECT COUNT(*) FROM self_governance_revisions"))
        self.assertEqual([(0,)], self.query("SELECT COUNT(*) FROM injection_control_revisions"))

    def test_injection_direct_set_clear_rollback_same_wake_truthful_audit(self) -> None:
        first = self.inj_commit()
        second = self.inj_commit(operation="clear", target_mode=None, expected_row_version=1,
                                 expected_active_revision=first["revision_id"])
        third = self.inj_commit(operation="rollback", target_mode=None, expected_row_version=2,
                                expected_active_revision=second["revision_id"], target_revision_id=first["revision_id"])
        self.assertEqual(["paused", "enabled", "paused"], [first["mode"], second["mode"], third["mode"]])
        self.assertEqual("next_real_wake", third["takes_effect"])
        self.assertTrue(third["current_wake_is_not_retroactively_changed"])
        self.assertTrue(third["storage_and_explicit_query_unchanged"])
        self.assertFalse(third["candidate_created"])
        self.assertFalse(third["review_performed"])
        self.assertEqual([(0,)], self.query("SELECT COUNT(*) FROM injection_control_candidates"))
        self.assertEqual([(None, ""), (None, ""), (None, "")],
                         self.query("SELECT candidate_id,reason FROM injection_control_revisions ORDER BY revision_number"))

    def test_injection_global_priority_vault_default_and_status_only(self) -> None:
        for operation in ("set", "clear", "rollback"):
            with self.subTest(operation=operation), self.assertRaisesRegex(
                    InjectionControlError, "vault_control_requires_legacy_authorization"):
                self.inj_commit(scope="hallucination_vault", operation=operation)
        self.assertEqual("hard_off", self.inj.effective_mode(
            owner_id="owner:test", model_id="model:test", scope="hallucination_vault"))
        self.inj_commit(scope="planning_memory", target_mode="enabled")
        self.inj_commit(scope="self_model", target_mode="paused")
        self.inj_commit(scope="global", target_mode="hard_off")
        self.assertEqual("hard_off", self.inj.effective_mode(owner_id="owner:test", model_id="model:test", scope="planning_memory"))
        repeat = self.inj.emergency_off(**self.binding, expected_row_version=0, reason="我保持关闭。")
        self.assertEqual("already_hard_off", repeat["decision"])
        self.assertFalse(repeat["state_changed"])
        with self.assertRaisesRegex(InjectionControlError, "status_only_reserved_for_hallucination_vault"):
            self.inj_commit(target_mode="status_only")
        self.assertIn("永不投影正文", self.inj.manual(owner_id="owner:test", model_id="model:test")["modes"]["status_only"])

    def test_injection_cas_scope_isolation_and_old_pending(self) -> None:
        pending = self.inj.propose_mode(**self.binding, scope="learning_memory", target_mode="hard_off",
            reason="我暂留候选。", expected_row_version=0, expected_active_revision=None)
        first = self.inj_commit(expected_row_version=1)
        self.assertEqual([("pending",)], self.query("SELECT status FROM injection_control_candidates WHERE candidate_id=?",
                                                    (pending["candidate_id"],)))
        for overrides, error in (
            ({"expected_row_version": 1}, "injection_control_version_conflict"),
            ({"expected_row_version": 2}, "active_injection_revision_conflict"),
            ({"expected_row_version": True}, "injection_control_version_conflict"),
            ({"owner_id": "owner:other", "operation": "rollback", "target_mode": None,
              "target_revision_id": first["revision_id"]}, "rollback_target_not_found"),
            ({"scope": "planning_memory", "operation": "rollback", "target_mode": None,
              "target_revision_id": first["revision_id"]}, "rollback_target_not_found"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(InjectionControlError, error):
                self.inj_commit(**overrides)
        self.assertEqual([(1,)], self.query("SELECT COUNT(*) FROM injection_control_revisions"))


if __name__ == "__main__":
    unittest.main()
