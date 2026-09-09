"""Service-layer regression for general7914, using real synthetic stores.

The fixture completes module one through its real multi-wake authoring flow,
enters self-edit through SelfModelAccessService, and issues real execution
leases for DailyMemoryAccessService. No permission/context gate is mocked.
There is no production server import, service environment, socket or live DB.
"""
from __future__ import annotations

import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from mcp_server.daily_memory_service import DailyMemoryAccessService
from mcp_server.emotional_service import EmotionalMemoryAccessService
from mcp_server.planning_service import PlanningMemoryAccessService
from runtime.emotional_memory import EmotionalMemoryStore
from runtime.execution_binding import ExecutionStore, canonical_hash
from runtime.planning_memory import PlanningMemoryStore


MODULES = ("emotional_memory", "planning_memory")
CORE_TABLES = (
    "brain_onboarding_state", "brain_module_unlocks", "brain_edit_challenges",
    "self_models", "self_model_candidates", "self_model_revisions", "self_revision_events",
)


class DailyMemoryDuringSelfEditTests(unittest.TestCase):
    def setUp(self) -> None:
        # Import inside setUp so unittest does not collect the helper TestCase.
        from mcp_server.tests.test_compact_open import CompactOpenStateTests
        self.fixture = CompactOpenStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("offline service test")))
        self.database = self.fixture.database.resolve()
        real_connect = sqlite3.connect

        def synthetic_connect(database, *args, **kwargs):
            if Path(database).resolve() != self.database:
                raise AssertionError("only this test's temporary database is allowed")
            return real_connect(database, *args, **kwargs)

        self.enterContext(patch("sqlite3.connect", side_effect=synthetic_connect))
        self.host = self.fixture.onboarding
        self.self_service = self.fixture.service
        self.common = {"owner_id": self.self_service.owner_id, "model_id": self.self_service.model_id}
        self.emotion = EmotionalMemoryAccessService(EmotionalMemoryStore(self.database),
                                                    onboarding=self.host, **self.common)
        self.planning = PlanningMemoryAccessService(PlanningMemoryStore(self.database),
                                                    onboarding=self.host, **self.common)
        self.daily = DailyMemoryAccessService(self.host, self.emotion, None, self.planning, **self.common)
        self.schema_hash = canonical_hash({"type": "object", "fixture": "self-edit-daily-service"})
        entries = [{"canonical_name": "remember_memory", "schema_hash": self.schema_hash}]
        self.catalog = {"contract": "advertised-tools/1", "catalog_complete": True,
                        "catalog_hash": canonical_hash(entries), "entries": entries}
        self.call_sequence = 0

    def core_snapshot(self) -> dict:
        """Compare complete synthetic core records, not merely reported booleans."""
        with closing(sqlite3.connect(self.database)) as connection:
            return {name: connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
                    for name in CORE_TABLES}

    def memory_counts(self) -> tuple[int, int, int]:
        with closing(sqlite3.connect(self.database)) as connection:
            return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("emotion_memories", "planning_items", "planning_change_candidates"))

    def enter_edit(self, *, confirmed: bool) -> str:
        self.fixture.bootstrap_live()  # Real activation, wholly inside TemporaryDirectory.
        status = self.self_service.module_one_status()["state"]
        self.assertEqual("live", status["stage"])
        self.assertEqual("complete", status["module_one_status"])
        self.fixture.wake("synthetic-self-edit-author")
        opened = self.self_service.open_brain()
        begun = self.self_service.submit_self_model_candidate(
            intent="begin_edit", write_context_ref=opened["write_context_ref"],
            expected_row_version=opened["row_version"],
        )
        self.assertEqual("challenge_issued", begun["decision"])
        self.assertNotIn("challenge_response", begun)
        if confirmed:
            response = self.self_service.submit_self_model_candidate(
                intent="confirm_edit", write_context_ref=opened["write_context_ref"],
                expected_row_version=begun["state"]["row_version"],
                payload={"challenge_id": begun["challenge_id"], "ai_confirmation": True},
            )
            self.assertEqual("confirmed", response["decision"])
        stage = "edit_body_draft" if confirmed else "edit_consent"
        state = self.self_service.module_one_status()["state"]
        self.assertEqual(stage, state["stage"])
        self.assertEqual("complete", state["module_one_status"])
        with closing(sqlite3.connect(self.database)) as connection:
            active = connection.execute("SELECT active_revision_id FROM self_models WHERE model_id = ?",
                                        (self.common["model_id"],)).fetchone()[0]
            unlock = connection.execute("SELECT unlocked, basis_revision_id FROM brain_module_unlocks "
                "WHERE owner_id = ? AND model_id = ? AND module_name = 'module_one'",
                (self.common["owner_id"], self.common["model_id"])).fetchone()
        self.assertIsNotNone(active)
        self.assertEqual((1, active), unlock)
        self.assertEqual(active, state["base_revision_id"])
        return stage

    def prepare_daily_wake(self, event: str) -> None:
        self.executions = ExecutionStore(self.database, deployment_epoch="synthetic-self-edit-daily",
            capability_secret=b"compact-open-isolated-test-secret-32-bytes")
        self.wake = self.host.issue_wake(**self.common, host_id="synthetic-self-edit-host",
            thread_id="synthetic-self-edit-thread", source_kind="human_message", source_event_id=event)
        prepared = self.host.build_pre_generation_context(**self.common, wake_id=self.wake["wake_id"],
            wake_capability=self.wake["wake_capability"], source_digest="synthetic-daily-event:" + event,
            host_contract_digest="synthetic-daily-contract", advertised_tools=self.catalog)
        injected = self.host.confirm_context_injected(**self.common, wake_id=self.wake["wake_id"],
            wake_capability=self.wake["wake_capability"], context_hash=prepared["context_hash"])
        self.assertEqual("injected", injected["decision"])

    def remember_bound(self, module: str, content: str) -> dict:
        self.call_sequence += 1
        arguments = {"module": module, "content": content}
        batch = {**self.common, "wake_id": self.wake["wake_id"],
                 "wake_capability": self.wake["wake_capability"],
                 "batch_id": f"synthetic-edit-daily-batch-{self.call_sequence}", "revision": 1}
        issued = self.executions.issue_batch(**batch, calls=[{
            "call_id": f"synthetic-daily-call-{self.call_sequence}",
            "advertised_name": "remember_memory", "canonical_tool": "remember_memory",
            "schema_hash": self.schema_hash, "catalog_hash": self.catalog["catalog_hash"],
            "arguments_hash": canonical_hash(arguments),
        }])
        ref = issued["executions"][0]["execution_ref"]
        claim = self.executions.claim(**self.common, execution_ref=ref,
                                      tool_name="remember_memory", arguments=arguments)
        try:
            with self.executions.bind(claim):
                result = self.daily.remember(**arguments)
        finally:
            self.executions.finish(claim)
        self.assertEqual(1, self.executions.batch_status(**batch)["counts"]["completed"])
        self.assertNotIn(ref, json.dumps(result))
        return result

    def assert_daily_modules_work_without_core_change(self, *, confirmed: bool) -> None:
        stage = self.enter_edit(confirmed=confirmed)
        # An unsubmitted body belongs to the caller, not to a nonexistent draft
        # database table. Do not submit it just to fabricate a persistence test.
        unsubmitted_draft = {"active_identity_capsule": "Synthetic not-yet-submitted self edit."}
        original_draft = copy.deepcopy(unsubmitted_draft)
        before = self.core_snapshot()
        self.prepare_daily_wake("synthetic-daily-while-" + stage)
        self.assertEqual(before, self.core_snapshot())
        self.assertEqual((0, 0, 0), self.memory_counts())
        originals = {}
        for module in MODULES:
            with self.subTest(module=module, stage=stage):
                originals[module] = f"  Synthetic ordinary {module} while editing.\nExact original spacing.  "
                result = self.remember_bound(module, originals[module])
                self.assertEqual("stored", result["decision"], result.get("reason_codes"))
                self.assertIs(result["stored"], True)
                self.assertIs(result["state_changed"], True)
                self.assertEqual(1, result["count"])
                self.assertEqual(1, result["version"])
                self.assertEqual(before, self.core_snapshot())
                self.assertEqual(stage, self.self_service.module_one_status()["state"]["stage"])
        self.assertEqual((1, 1, 0), self.memory_counts())
        self.assertEqual(original_draft, unsubmitted_draft)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(originals["emotional_memory"], connection.execute(
                "SELECT original_text FROM emotion_memories").fetchone()[0])
            self.assertEqual(originals["planning_memory"], json.loads(connection.execute(
                "SELECT content_json FROM planning_versions").fetchone()[0])["original_text"])
            candidate_bodies = [row[0] for row in connection.execute("SELECT content_json FROM self_model_candidates")]
            self.assertFalse(any(unsubmitted_draft["active_identity_capsule"] in text for text in candidate_bodies))

    def test_emotional_and_planning_during_edit_body_draft_preserve_active_core_and_unsubmitted_draft(self):
        self.assert_daily_modules_work_without_core_change(confirmed=True)

    def test_emotional_and_planning_during_edit_consent_preserve_pending_challenge(self):
        self.assert_daily_modules_work_without_core_change(confirmed=False)

    def test_factory_not_completed_still_rejects_both_daily_modules(self):
        state = self.self_service.module_one_status()["state"]
        self.assertEqual("factory", state["stage"])
        self.assertNotEqual("complete", state["module_one_status"])
        before = self.core_snapshot()
        self.prepare_daily_wake("synthetic-not-onboarded-daily")
        for module in MODULES:
            with self.subTest(module=module):
                result = self.remember_bound(module, "Synthetic ordinary request before first completion.")
                self.assertEqual("reject", result["decision"])
                self.assertEqual(["module_one_required"], result["reason_codes"])
                self.assertIs(result["stored"], False)
                self.assertIs(result["state_changed"], False)
                self.assertEqual(0, result["count"])
        self.assertEqual((0, 0, 0), self.memory_counts())
        self.assertEqual(before, self.core_snapshot())

    def test_edit_state_does_not_remove_real_execution_binding_requirement(self):
        self.enter_edit(confirmed=True)
        before = self.core_snapshot()
        for module in MODULES:
            result = self.daily.remember(module, "Synthetic request with no execution lease.")
            self.assertEqual(["execution_binding_required"], result["reason_codes"])
            self.assertIs(result["stored"], False)
        self.assertEqual((0, 0, 0), self.memory_counts())
        self.assertEqual(before, self.core_snapshot())


if __name__ == "__main__":
    unittest.main()
