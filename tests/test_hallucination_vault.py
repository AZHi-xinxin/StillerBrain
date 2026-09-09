from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from runtime.hallucination_vault import (
    HallucinationVaultError,
    HallucinationVaultStore,
    LearningQuarantineAdapter,
    _sha256,
)
from runtime.learning_memory import LearningMemoryStore


OWNER = "owner-a"
MODEL = "model-a"
WARNING = "我将看到自己主动隔离的旧内容；我不会把它自动当成当前事实。"
SUFFIX = "我已经读完这一条，并会继续区分旧断言、当前依据与仍存的不确定。"


class HallucinationVaultStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.main_db = root / "main.db"
        self.vault_db = root / "vault.db"
        self.learning = LearningMemoryStore(self.main_db, idea_database=root / "ideas.db")
        self.learning.ensure_state(owner_id=OWNER, model_id=MODEL)
        self.adapter = LearningQuarantineAdapter(self.main_db)
        self.vault = HallucinationVaultStore(
            self.vault_db, source_adapters={"learning": self.adapter}
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed_learning(self, learning_id: str = "learn_seed") -> tuple[str, dict]:
        now = "2026-08-30T00:00:00.000+00:00"
        content = {
            "kind": "concept",
            "title": "蓝塔红钥匙用途",
            "summary": "红钥匙用途的一条待复核说法。",
            "current_understanding": "红钥匙能打开北门。",
            "steps": [],
            "application_contexts": ["讨论蓝塔红钥匙"],
            "scene_tags": ["蓝塔红钥匙"],
            "preceding_context_summary": "",
            "uncertainties": ["未实际核验"],
            "domain": "虚构游戏",
            "keywords": ["蓝塔", "红钥匙"],
            "entities": [],
            "epistemic_status": "reported",
            "confidence": 30,
            "time_sensitivity": "stable",
            "valid_as_of": "",
            "review_after": "",
            "importance": 50,
            "sensitivity": "private",
            "context_policy": "normal",
            "recall_mode": "normal",
            "allow_contexts": [],
            "deny_contexts": [],
            "default_decision": "background_reference",
            "explicit_request_override": "allow_after_confirmation",
            "disclosure": "bounded_excerpt",
            "lifecycle": "active",
            "provenance_badge": "reported",
            "referent_bindings": [],
        }
        content_json = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = _sha256(content)
        with closing(sqlite3.connect(self.main_db)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO learning_items "
                "(learning_id,owner_id,model_id,kind,lifecycle,current_version,current_json,current_hash,"
                "created_wake_id,created_wake_seq,created_at,updated_at) "
                "VALUES (?,?,?,'concept','active',1,?,?, 'wake-seed',1,?,?)",
                (learning_id, OWNER, MODEL, content_json, content_hash, now, now),
            )
            connection.execute(
                "INSERT INTO learning_versions "
                "(version_id,learning_id,version,previous_version,mutable_json,mutable_hash,ai_diff,"
                "canonical_diff_json,correctness_assessment,reason,rollback_to_version,wake_id,created_at) "
                "VALUES (?,?,1,NULL,?,?,?,?,'reported and unverified','seed',NULL,'wake-seed',?)",
                (
                    f"lv_{learning_id}",
                    learning_id,
                    content_json,
                    content_hash,
                    "initial",
                    "[]",
                    now,
                ),
            )
            connection.commit()
        return f"learning://{learning_id}@1", content

    @staticmethod
    def _record_fields() -> dict:
        return {
            "neutral_title": "红钥匙用途判断隔离",
            "isolated_content": "我曾把红钥匙用途的一条说法当成已经确定的事实。",
            "current_account": "我当前只确认这里存在待核验说法，不确认哪一条为真。",
            "basis": "我尚无实际游玩或独立证据，因此主动隔离原先的确定语气。",
            "reflection": "我会把来源状态和真假判断分开。",
            "uncertainty_status": "still_uncertain",
            "reason": "我决定把未经核验的确定断言放入隔离区。",
            "ai_confirmation": True,
        }

    def test_default_is_hard_off_and_no_automatic_recall_surface_exists(self) -> None:
        status = self.vault.status(owner_id=OWNER, model_id=MODEL)
        self.assertEqual(status["automatic_exposure"], "hard_off")
        self.assertFalse(status["automatic_injection"])
        self.assertFalse(hasattr(self.vault, "build_envelopes"))
        with closing(sqlite3.connect(self.vault_db)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM vault_owner_state").fetchone()[0],
                0,
            )

    def test_first_hold_is_atomic_with_ai_authored_warning(self) -> None:
        missing = self.vault.hold(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
            **self._record_fields(),
        )
        self.assertEqual(missing["decision"], "setup_required")
        self.assertEqual(self.vault.open(owner_id=OWNER, model_id=MODEL)["total"], 0)
        stored = self.vault.hold(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
            warning_text=WARNING,
            warning_suffix=SUFFIX,
            **self._record_fields(),
        )
        self.assertEqual(stored["decision"], "stored_quarantined")
        self.assertEqual(stored["vault_row_version"], 1)
        self.assertTrue(self.vault.status(owner_id=OWNER, model_id=MODEL)["warning_configured"])

    def test_directory_and_body_are_separate_and_owner_scoped(self) -> None:
        stored = self.vault.hold(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
            warning_text=WARNING,
            warning_suffix=SUFFIX,
            **self._record_fields(),
        )
        directory = self.vault.open(owner_id=OWNER, model_id=MODEL)
        self.assertEqual(
            set(directory["entries"][0]),
            {
                "record_id",
                "neutral_title",
                "created_at",
                "lifecycle",
                "review_status",
                "uncertainty_status",
            },
        )
        warning = self.vault.open(
            owner_id=OWNER, model_id=MODEL, record_id=stored["record_id"]
        )
        self.assertEqual(warning["decision"], "warning_confirmation_required")
        self.assertNotIn("content", warning)
        with self.assertRaisesRegex(HallucinationVaultError, "warning_confirmation_mismatch"):
            self.vault.open(
                owner_id=OWNER,
                model_id=MODEL,
                record_id=stored["record_id"],
                warning_confirmation="wrong",
                expected_warning_version=1,
            )
        opened = self.vault.open(
            owner_id=OWNER,
            model_id=MODEL,
            record_id=stored["record_id"],
            warning_confirmation=WARNING,
            expected_warning_version=1,
        )
        self.assertTrue(opened["content_exposed"])
        self.assertIn("isolated_content", opened["content"])
        with self.assertRaisesRegex(HallucinationVaultError, "record_not_found"):
            self.vault.open(
                owner_id="owner-b",
                model_id=MODEL,
                record_id=stored["record_id"],
            )

    def test_learning_transfer_physically_removes_source_and_leaves_redirect(self) -> None:
        source_ref, _ = self._seed_learning()
        preview = self.vault.preview_transfer(
            owner_id=OWNER,
            model_id=MODEL,
            source_ref=source_ref,
            expected_source_row_version=0,
        )
        result = self.vault.commit_transfer(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
            expected_source_row_version=0,
            journal_id=preview["journal_id"],
            expected_preview_hash=preview["preview_hash"],
            warning_text=WARNING,
            warning_suffix=SUFFIX,
            **self._record_fields(),
        )
        self.assertEqual(result["decision"], "transferred_quarantined")
        self.assertFalse(
            self.adapter.source_present(owner_id=OWNER, model_id=MODEL, source_ref=source_ref)
        )
        self.assertEqual(
            self.adapter.redirect_status(owner_id=OWNER, model_id=MODEL, source_ref=source_ref),
            "committed",
        )
        exact = self.learning.recall(
            owner_id=OWNER,
            model_id=MODEL,
            target_ref=source_ref,
        )
        self.assertEqual("content_quarantined", exact["decision"])
        self.assertEqual([], exact["results"])
        self.assertFalse(exact["quarantine_redirect"]["content_exposed"])
        self.assertEqual(result["record_id"], exact["quarantine_redirect"]["record_id"])
        serialized_redirect = json.dumps(exact, ensure_ascii=False)
        self.assertNotIn("红钥匙能打开北门", serialized_redirect)
        semantic = self.learning.recall(
            owner_id=OWNER,
            model_id=MODEL,
            query="蓝塔红钥匙",
        )
        self.assertEqual(0, semantic["result_count"])
        opened = self.vault.open(
            owner_id=OWNER,
            model_id=MODEL,
            record_id=result["record_id"],
            warning_confirmation=WARNING,
            expected_warning_version=1,
        )
        self.assertEqual(opened["source_snapshot"]["source_ref"], source_ref)
        self.assertEqual(
            opened["source_snapshot"]["snapshot"]["item"]["learning_id"], "learn_seed"
        )

    def test_startup_repairs_committed_journal_with_staging_redirect(self) -> None:
        source_ref, _ = self._seed_learning("learn_crash_window")
        preview = self.vault.preview_transfer(
            owner_id=OWNER,
            model_id=MODEL,
            source_ref=source_ref,
            expected_source_row_version=0,
        )
        with mock.patch.object(
            self.adapter,
            "mark_redirect_committed",
            side_effect=RuntimeError("simulated crash after vault finalize"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.vault.commit_transfer(
                    owner_id=OWNER,
                    model_id=MODEL,
                    wake_id="wake-1",
                    wake_seq=1,
                    expected_row_version=0,
                    expected_source_row_version=0,
                    journal_id=preview["journal_id"],
                    expected_preview_hash=preview["preview_hash"],
                    warning_text=WARNING,
                    warning_suffix=SUFFIX,
                    **self._record_fields(),
                )
        self.assertEqual(
            self.adapter.redirect_status(
                owner_id=OWNER, model_id=MODEL, source_ref=source_ref
            ),
            "staging",
        )
        recovered_adapter = LearningQuarantineAdapter(self.main_db)
        recovered_vault = HallucinationVaultStore(
            self.vault_db, source_adapters={"learning": recovered_adapter}
        )
        self.assertEqual(
            recovered_adapter.redirect_status(
                owner_id=OWNER, model_id=MODEL, source_ref=source_ref
            ),
            "committed",
        )
        self.assertEqual(recovered_vault.open(owner_id=OWNER, model_id=MODEL)["total"], 1)

    def test_source_change_after_preview_fails_without_half_record(self) -> None:
        source_ref, content = self._seed_learning("learn_changed")
        preview = self.vault.preview_transfer(
            owner_id=OWNER,
            model_id=MODEL,
            source_ref=source_ref,
            expected_source_row_version=0,
        )
        content["summary"] = "changed after preview"
        payload = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with closing(sqlite3.connect(self.main_db)) as connection:
            connection.execute(
                "UPDATE learning_items SET current_json=?, current_hash=? WHERE learning_id='learn_changed'",
                (payload, _sha256(content)),
            )
            connection.commit()
        with self.assertRaisesRegex(HallucinationVaultError, "source_changed_since_preview"):
            self.vault.commit_transfer(
                owner_id=OWNER,
                model_id=MODEL,
                wake_id="wake-1",
                wake_seq=1,
                expected_row_version=0,
                expected_source_row_version=0,
                journal_id=preview["journal_id"],
                expected_preview_hash=preview["preview_hash"],
                warning_text=WARNING,
                warning_suffix=SUFFIX,
                **self._record_fields(),
            )
        self.assertTrue(
            self.adapter.source_present(owner_id=OWNER, model_id=MODEL, source_ref=source_ref)
        )
        self.assertEqual(self.vault.open(owner_id=OWNER, model_id=MODEL)["total"], 0)

    def test_restore_requires_later_wake_open_and_cas(self) -> None:
        source_ref, _ = self._seed_learning("learn_restore")
        preview = self.vault.preview_transfer(
            owner_id=OWNER,
            model_id=MODEL,
            source_ref=source_ref,
            expected_source_row_version=0,
        )
        transferred = self.vault.commit_transfer(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
            expected_source_row_version=0,
            journal_id=preview["journal_id"],
            expected_preview_hash=preview["preview_hash"],
            warning_text=WARNING,
            warning_suffix=SUFFIX,
            **self._record_fields(),
        )
        candidate = self.vault.propose_restore(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=1,
            record_id=transferred["record_id"],
            expected_record_version=1,
            destination_module="learning_memory",
            destination_row_version=1,
            reason="我获得了新的依据，想在下一轮复核后恢复原卡。",
            ai_confirmation=True,
        )
        common = dict(
            owner_id=OWNER,
            model_id=MODEL,
            expected_row_version=2,
            candidate_id=candidate["candidate_id"],
            expected_candidate_version=1,
            expected_candidate_hash=candidate["candidate_hash"],
            expected_base_record_version=1,
            action="activate",
            reason="我在较晚唤醒重新核对，决定恢复。",
            ai_confirmation=True,
            expected_destination_row_version=1,
        )
        with self.assertRaisesRegex(HallucinationVaultError, "later_real_wake_required"):
            self.vault.review_restore(wake_id="wake-1", wake_seq=1, **common)
        with self.assertRaisesRegex(HallucinationVaultError, "restore_candidate_open_required"):
            self.vault.review_restore(wake_id="wake-2", wake_seq=2, **common)
        self.vault.open(
            owner_id=OWNER,
            model_id=MODEL,
            record_id=transferred["record_id"],
            warning_confirmation=WARNING,
            expected_warning_version=1,
            wake_id="wake-2",
            wake_seq=2,
        )
        restored = self.vault.review_restore(wake_id="wake-2", wake_seq=2, **common)
        self.assertEqual(restored["decision"], "restored")
        self.assertTrue(
            self.adapter.source_present(owner_id=OWNER, model_id=MODEL, source_ref=source_ref)
        )
        self.assertEqual(
            self.adapter.redirect_status(owner_id=OWNER, model_id=MODEL, source_ref=source_ref),
            "restored",
        )

    def test_restore_history_key_collision_fails_closed_and_keeps_candidate(self) -> None:
        source_ref, _ = self._seed_learning("learn_collision")
        now = "2026-08-30T00:00:01.000+00:00"
        with closing(sqlite3.connect(self.main_db)) as connection:
            connection.execute(
                "INSERT INTO learning_audit_events "
                "(event_seq,event_id,owner_id,model_id,learning_id,candidate_id,action,actor,"
                "wake_id,decision,reason_codes_json,details_json,details_hash,created_at) "
                "VALUES (17,'audit-original',?,?, 'learn_collision',NULL,'create','ai',"
                "'wake-seed','created','[]','{}','hash-original',?)",
                (OWNER, MODEL, now),
            )
            connection.commit()
        preview = self.vault.preview_transfer(
            owner_id=OWNER,
            model_id=MODEL,
            source_ref=source_ref,
            expected_source_row_version=0,
        )
        transferred = self.vault.commit_transfer(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=0,
            expected_source_row_version=0,
            journal_id=preview["journal_id"],
            expected_preview_hash=preview["preview_hash"],
            warning_text=WARNING,
            warning_suffix=SUFFIX,
            **self._record_fields(),
        )
        # A different event claimed the old numeric audit key while the source
        # was quarantined. Restore must not overwrite or renumber either history.
        with closing(sqlite3.connect(self.main_db)) as connection:
            connection.execute(
                "INSERT INTO learning_audit_events "
                "(event_seq,event_id,owner_id,model_id,learning_id,candidate_id,action,actor,"
                "wake_id,decision,reason_codes_json,details_json,details_hash,created_at) "
                "VALUES (17,'audit-new',?,?,NULL,NULL,'unrelated','system',"
                "'wake-between','kept','[]','{}','hash-new',?)",
                (OWNER, MODEL, now),
            )
            connection.commit()
        candidate = self.vault.propose_restore(
            owner_id=OWNER,
            model_id=MODEL,
            wake_id="wake-1",
            wake_seq=1,
            expected_row_version=1,
            record_id=transferred["record_id"],
            expected_record_version=1,
            destination_module="learning_memory",
            destination_row_version=1,
            reason="我想在下一轮重新核对后恢复。",
            ai_confirmation=True,
        )
        self.vault.open(
            owner_id=OWNER,
            model_id=MODEL,
            record_id=transferred["record_id"],
            warning_confirmation=WARNING,
            expected_warning_version=1,
            wake_id="wake-2",
            wake_seq=2,
        )
        with self.assertRaisesRegex(HallucinationVaultError, "restore_snapshot_conflict"):
            self.vault.review_restore(
                owner_id=OWNER,
                model_id=MODEL,
                wake_id="wake-2",
                wake_seq=2,
                expected_row_version=2,
                candidate_id=candidate["candidate_id"],
                expected_candidate_version=1,
                expected_candidate_hash=candidate["candidate_hash"],
                expected_base_record_version=1,
                action="activate",
                reason="我决定恢复，但不会覆盖已有历史。",
                ai_confirmation=True,
                expected_destination_row_version=1,
            )
        pending = self.vault.pending_restore_candidates(owner_id=OWNER, model_id=MODEL)
        self.assertEqual([item["candidate_id"] for item in pending], [candidate["candidate_id"]])
        self.assertFalse(
            self.adapter.source_present(owner_id=OWNER, model_id=MODEL, source_ref=source_ref)
        )
        self.assertEqual(
            self.adapter.redirect_status(owner_id=OWNER, model_id=MODEL, source_ref=source_ref),
            "committed",
        )


if __name__ == "__main__":
    unittest.main()
