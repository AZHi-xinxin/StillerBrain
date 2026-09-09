"""Synthetic daily storage contracts and real ExecutionStore integration.

DailyMemoryServiceTests isolates the facade behind a fake host boundary;
DailyMemoryExecutionIntegrationTests uses real onboarding and execution leases.
Neither class runs the MCP dispatcher, a client, live store or network request.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mcp_server.daily_memory_service import DailyMemoryAccessService
from mcp_server.emotional_service import EmotionalMemoryAccessService
from mcp_server.learning_service import LearningMemoryAccessService
from mcp_server.planning_service import PlanningMemoryAccessService
from runtime.emotional_memory import EmotionalMemoryStore
from runtime.learning_memory import LearningMemoryStore
from runtime.planning_memory import PlanningMemoryStore
from runtime.execution_binding import ExecutionClaim, ExecutionBindingError, ExecutionStore, canonical_hash


OWNER, MODEL = "synthetic-daily-owner", "synthetic-daily-model"


class SyntheticHost:
    def __init__(self):
        self.wake_id, self.wake_seq = "synthetic-wake-1", 1
        self.ref = "synthetic-open-ref"
        self.mode = "gateway_injected"
        self.scopes = {"emotional_memory", "learning_memory", "planning_memory"}
        self.allowed = True
        self.open_calls, self.binding_calls = [], []

    def authorize_other_module_write(self, **fields):
        return {"decision": "allowed" if self.allowed and (fields["owner_id"], fields["model_id"]) == (OWNER, MODEL) else "reject"}

    def contains_protected_persistence_value(self, **fields):
        return "synthetic-protected-value" in json.dumps(fields["value"])

    def open_brain_context(self, **fields):
        self.open_calls.append(fields)
        if fields["expected_wake_id"] != self.wake_id:
            return {"write_context_available": False, "reason_code": "execution_wake_mismatch"}
        return {"write_context_available": True, "write_context_ref": self.ref}

    def current_open_write_context(self, **fields):
        self.binding_calls.append(fields)
        allowed = (fields["owner_id"], fields["model_id"]) == (OWNER, MODEL)
        allowed = allowed and fields["write_context_ref"] == self.ref and fields["required_scope"] in self.scopes
        allowed = allowed and fields.get("expected_wake_id") in (None, self.wake_id)
        return {"write_context_available": allowed, "wake_id": self.wake_id, "wake_seq": self.wake_seq,
                "context_mode": self.mode, "authorized_scopes": sorted(self.scopes),
                "reason_code": "direct_scope_not_authorized" if not allowed else None}


class DailyMemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.socket", side_effect=AssertionError("offline test")))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("offline test")))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("offline test")))
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="daily-memory-synthetic-"))
        self.database = Path(directory) / "synthetic-main.sqlite3"
        idea_database = Path(directory) / "synthetic-ideas.sqlite3"
        real_connect = sqlite3.connect
        allowed = {self.database.resolve(), idea_database.resolve()}

        def synthetic_connect(path, *args, **kwargs):
            if Path(path).resolve() not in allowed:
                raise AssertionError("only synthetic test databases may be opened")
            return real_connect(path, *args, **kwargs)

        self.enterContext(patch("sqlite3.connect", side_effect=synthetic_connect))
        self.host = SyntheticHost()
        common = dict(onboarding=self.host, owner_id=OWNER, model_id=MODEL)
        self.emotional = EmotionalMemoryAccessService(EmotionalMemoryStore(self.database), **common)
        self.learning = LearningMemoryAccessService(LearningMemoryStore(self.database, idea_database=idea_database), **common)
        self.planning = PlanningMemoryAccessService(PlanningMemoryStore(self.database), **common)
        self.daily = DailyMemoryAccessService(self.host, self.emotional, self.learning, self.planning, OWNER, MODEL)
        self.claim = ExecutionClaim("synthetic-execution-ref", OWNER, MODEL, self.host.wake_id,
                                    "synthetic-batch", "synthetic-call", "remember_memory",
                                    "synthetic-epoch", "synthetic-claim", str(self.database))
        self.bound = self.enterContext(patch("mcp_server.daily_memory_service.current_execution_claim", return_value=self.claim))

    def rows(self, table):
        self.assertIn(table, {"emotion_memories", "learning_items", "learning_versions", "planning_items",
                             "planning_versions", "planning_events", "planning_change_candidates"})
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)]

    def counts(self):
        return tuple(len(self.rows(table)) for table in ("emotion_memories", "learning_items", "planning_items"))

    def test_one_call_per_module_stores_preserved_original_without_manual_or_ref_argument(self):
        original = "  Synthetic original\nwith unchanged spacing.  "
        for index, module in enumerate(self.daily.services):
            with self.subTest(module=module):
                self.bound.return_value = replace(self.claim, call_id=f"synthetic-call-{index}")
                result = self.daily.remember(module, original)
                self.assertEqual(result["decision"], "stored")
                self.assertTrue(result["stored"])
                self.assertEqual(result["count"], 1)
                self.assertEqual(result["version"], 1)
                self.assertNotIn("write_context_ref", result)
                self.assertNotIn(original, json.dumps(result))
                self.assertNotIn(self.host.ref, json.dumps(result))
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(self.rows("emotion_memories")[0]["original_text"], original)
        learning = json.loads(self.rows("learning_items")[0]["current_json"])
        self.assertEqual(learning["current_understanding"], original)
        self.assertEqual(learning["source_basis"], "reported")
        planning = json.loads(self.rows("planning_versions")[0]["content_json"])
        self.assertEqual(planning["original_text"], original)
        self.assertEqual(planning["write_mode"], "ordinary_record")
        self.assertNotIn("ai_adoption_statement", planning)
        self.assertEqual(self.rows("planning_change_candidates"), [])
        self.assertTrue(all(call["present_details"] is False for call in self.host.open_calls))
        self.assertTrue(all(call["expected_wake_id"] == self.host.wake_id for call in self.host.open_calls))

    def test_no_claim_does_not_open_or_guess_current_wake(self):
        self.bound.return_value = None
        result = self.daily.remember("planning_memory", "Synthetic")
        self.assertEqual(result["reason_codes"], ["execution_binding_required"])
        self.assertEqual(self.host.open_calls, [])
        self.assertEqual(self.host.binding_calls, [])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_wrong_owner_model_tool_or_nonclaim_never_opens(self):
        for claim in (replace(self.claim, owner_id="other"), replace(self.claim, model_id="other"),
                      replace(self.claim, tool_name="stbrain_open"), object()):
            with self.subTest(claim_type=type(claim).__name__):
                self.bound.return_value = claim
                result = self.daily.remember("learning_memory", "Synthetic")
                self.assertFalse(result["stored"])
        self.assertEqual(self.host.open_calls, [])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_old_execution_wake_cannot_attach_to_new_wake(self):
        self.host.wake_id = "new-synthetic-wake"
        result = self.daily.remember("planning_memory", "Synthetic")
        self.assertEqual(result["reason_codes"], ["execution_wake_mismatch"])
        self.assertEqual(self.host.open_calls[0]["expected_wake_id"], self.claim.wake_id)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_truthy_open_availability_is_not_authority(self):
        with patch.object(self.host, "open_brain_context", return_value={"write_context_available": 1, "write_context_ref": self.host.ref}):
            result = self.daily.remember("planning_memory", "Synthetic")
        self.assertFalse(result["stored"])
        self.assertEqual(self.host.binding_calls, [])

    def test_explicit_ref_cannot_override_normal_claim_or_replace_missing_claim(self):
        result = self.daily.remember("planning_memory", "Synthetic", write_context_ref=self.host.ref)
        self.assertFalse(result["stored"])
        self.assertEqual(self.host.open_calls, [])
        self.bound.return_value = None
        result = self.daily.remember("planning_memory", "Synthetic", write_context_ref=self.host.ref)
        self.assertEqual(result["reason_codes"], ["invalid_direct_context"])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_already_opened_direct_context_requires_exact_scope_and_never_opens_grant(self):
        self.bound.return_value = None
        self.host.mode = "human_attested_direct"
        self.host.scopes = {"planning_memory"}
        denied = self.daily.remember("learning_memory", "Synthetic", write_context_ref=self.host.ref)
        self.assertFalse(denied["stored"])
        stored = self.daily.remember("planning_memory", "Synthetic", write_context_ref=self.host.ref)
        replay = self.daily.remember("planning_memory", "Synthetic", write_context_ref=self.host.ref)
        self.assertTrue(stored["stored"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertFalse(replay["state_changed"])
        self.assertEqual(stored["ref"], replay["ref"])
        self.assertEqual(self.host.open_calls, [])
        self.assertEqual(self.counts(), (0, 0, 1))

    def test_row_version_is_read_automatically_for_each_distinct_write(self):
        for index in range(2):
            self.bound.return_value = replace(self.claim, call_id=f"synthetic-call-{index}")
            result = self.daily.remember("planning_memory", f"Synthetic {index}")
            self.assertTrue(result["stored"])
        self.assertEqual(self.planning.status()["row_version"], 2)

    def test_legacy_permission_secret_and_scope_gates_are_not_skipped(self):
        self.host.allowed = False
        result = self.daily.remember("planning_memory", "Synthetic")
        self.assertEqual(result["reason_codes"], ["module_one_required"])
        self.host.allowed = True
        for module in self.daily.services:
            with self.subTest(module=module):
                result = self.daily.remember(module, "synthetic-protected-value")
                self.assertEqual(result["reason_codes"], ["credential_or_secret_detected"])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_invalid_input_returns_one_actionable_fix_before_open(self):
        for fields, field in (({"content": " "}, "content"), ({"kind": "bad"}, "kind"),
                              ({"importance": True}, "importance"), ({"keywords": "not-list"}, "keywords")):
            with self.subTest(field=field):
                args = {"module": "planning_memory", "content": "Synthetic", **fields}
                result = self.daily.remember(**args)
                self.assertEqual(result["field"], field)
                self.assertTrue(result["next_step"])
        self.assertEqual(self.host.open_calls, [])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_cas_rejection_is_not_retried_and_backend_payload_is_not_exposed(self):
        with patch.object(self.learning, "remember", return_value={"decision": "reject", "reason_codes": ["learning_row_version_conflict"],
                                                                   "status": {"secret": "do-not-echo"}}) as writer:
            result = self.daily.remember("learning_memory", "Synthetic")
        writer.assert_called_once()
        self.assertEqual(result["reason_codes"], ["learning_row_version_conflict"])
        self.assertNotIn("do-not-echo", json.dumps(result))
        self.assertFalse(result["state_changed"])

    def test_default_assessment_is_explicitly_unverified_not_ai_confirmation(self):
        with patch.object(self.learning, "remember", wraps=self.learning.remember) as writer:
            result = self.daily.remember("learning_memory", "Synthetic")
        self.assertTrue(result["stored"])
        fields = writer.call_args.kwargs
        self.assertEqual(fields["source_basis"], "reported")
        self.assertIn("未执行独立核验", fields["correctness_assessment"])
        self.assertNotIn("ai_confirmation", fields)
        self.assertNotIn("calm_check", fields)
        self.assertTrue(fields["preserve_original_text"])

    def test_candidate_result_never_claims_stored_and_keeps_actual_mutation_flag(self):
        with patch.object(self.learning, "remember", return_value={"decision": "candidate_pending", "state_changed": True,
                                                                   "candidate_body": "do-not-echo"}):
            result = self.daily.remember("learning_memory", "Synthetic")
        self.assertFalse(result["stored"])
        self.assertTrue(result["state_changed"])
        self.assertEqual(result["count"], 0)
        self.assertNotIn("do-not-echo", json.dumps(result))

    def test_unknown_execution_exception_is_redacted(self):
        with patch.object(self.host, "open_brain_context", side_effect=ExecutionBindingError("private-value-do-not-echo")):
            result = self.daily.remember("planning_memory", "Synthetic")
        self.assertEqual(result["reason_codes"], ["daily_write_rejected"])
        self.assertNotIn("private-value", json.dumps(result))
        self.assertEqual(self.counts(), (0, 0, 0))


class DailyMemoryExecutionIntegrationTests(unittest.TestCase):
    def setUp(self):
        from mcp_server.tests.test_compact_open import CompactOpenStateTests
        self.fixture = CompactOpenStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("offline test")))
        self.database = self.fixture.database.resolve()
        ideas = (Path(self.fixture.temp.name) / "synthetic-daily-ideas.sqlite3").resolve()
        real_connect = sqlite3.connect
        def synthetic_connect(path, *args, **kwargs):
            if Path(path).resolve() not in {self.database, ideas}:
                raise AssertionError("only synthetic test databases may be opened")
            return real_connect(path, *args, **kwargs)
        self.enterContext(patch("sqlite3.connect", side_effect=synthetic_connect))
        self.fixture.bootstrap_live()  # Fixture name means synthetic module-one active state.
        self.host = self.fixture.onboarding
        self.common = dict(owner_id=self.fixture.service.owner_id, model_id=self.fixture.service.model_id)
        services = [
            EmotionalMemoryAccessService(EmotionalMemoryStore(self.database), onboarding=self.host, **self.common),
            LearningMemoryAccessService(LearningMemoryStore(self.database, idea_database=ideas), onboarding=self.host, **self.common),
            PlanningMemoryAccessService(PlanningMemoryStore(self.database), onboarding=self.host, **self.common),
        ]
        self.daily = DailyMemoryAccessService(self.host, *services, **self.common)
        self.executions = ExecutionStore(self.database, deployment_epoch="synthetic-daily-epoch",
                                        capability_secret=b"compact-open-isolated-test-secret-32-bytes")
        schema_hash = canonical_hash({"type": "object", "synthetic": True})
        entries = [{"canonical_name": "remember_memory", "schema_hash": schema_hash}]
        catalog = {"contract": "advertised-tools/1", "catalog_complete": True,
                   "catalog_hash": canonical_hash(entries), "entries": entries}
        self.wake = self.host.issue_wake(**self.common, host_id="synthetic-host", thread_id="synthetic-thread",
                                         source_kind="human_message", source_event_id="synthetic-daily-real-lease")
        prepared = self.host.build_pre_generation_context(**self.common, wake_id=self.wake["wake_id"],
            wake_capability=self.wake["wake_capability"], source_digest="synthetic-source",
            host_contract_digest="synthetic-contract", advertised_tools=catalog)
        self.host.confirm_context_injected(**self.common, wake_id=self.wake["wake_id"],
            wake_capability=self.wake["wake_capability"], context_hash=prepared["context_hash"])
        self.batch = {**self.common, "wake_id": self.wake["wake_id"], "wake_capability": self.wake["wake_capability"],
                      "batch_id": "synthetic-daily-batch", "revision": 1}
        self.original = "  Synthetic exact original\nwith final spacing.  "
        self.arguments = [{"module": module, "content": self.original} for module in self.daily.services]
        issued = self.executions.issue_batch(**self.batch, calls=[
            {"call_id": f"synthetic-call-{index}", "advertised_name": "remember_memory",
             "canonical_tool": "remember_memory", "schema_hash": schema_hash,
             "catalog_hash": catalog["catalog_hash"], "arguments_hash": canonical_hash(arguments)}
            for index, arguments in enumerate(self.arguments)])
        self.refs = [item["execution_ref"] for item in issued["executions"]]

    def test_real_issue_claim_bound_same_wake_three_module_storage_and_finish(self):
        for arguments, ref in zip(self.arguments, self.refs):
            with self.subTest(module=arguments["module"]):
                claim = self.executions.claim(**self.common, execution_ref=ref,
                    tool_name="remember_memory", arguments=arguments)
                with self.executions.bind(claim):
                    result = self.daily.remember(**arguments)
                self.assertTrue(result["stored"])
                self.assertEqual(1, result["count"])
                self.assertNotIn("write_context_ref", result)
                self.assertNotIn(ref, json.dumps(result))
                self.executions.finish(claim)
                with self.assertRaises(ExecutionBindingError):
                    self.executions.claim(**self.common, execution_ref=ref,
                        tool_name="remember_memory", arguments=arguments)
                with self.executions.bind(claim):
                    rejected = self.daily.remember(**arguments)
                self.assertFalse(rejected["stored"])
                self.assertFalse(rejected["state_changed"])
        self.assertEqual(3, self.executions.batch_status(**self.batch)["counts"]["completed"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(self.original, connection.execute("SELECT original_text FROM emotion_memories").fetchone()[0])
            self.assertEqual(self.original, json.loads(connection.execute("SELECT current_json FROM learning_items").fetchone()[0])["current_understanding"])
            self.assertEqual(self.original, json.loads(connection.execute("SELECT content_json FROM planning_versions").fetchone()[0])["original_text"])
            self.assertEqual(0, connection.execute("SELECT count(*) FROM planning_change_candidates").fetchone()[0])
        self.assertEqual(0, self.fixture.proof_count("candidate_full_review", self.wake))
        self.assertEqual(0, self.fixture.proof_count("candidate_objection_full_review", self.wake))

    def test_unclaimed_or_forged_claim_cannot_use_real_onboarding(self):
        self.assertFalse(self.daily.remember(**self.arguments[0])["stored"])
        forged = ExecutionClaim(self.refs[0], **self.common, wake_id=self.wake["wake_id"],
            batch_id=self.batch["batch_id"], call_id="synthetic-call-0", tool_name="remember_memory",
            deployment_epoch="synthetic-daily-epoch", claim_id="never-issued-claim", database=str(self.database))
        with self.executions.bind(forged):
            result = self.daily.remember(**self.arguments[0])
        self.assertFalse(result["stored"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(0, connection.execute("SELECT count(*) FROM emotion_memories").fetchone()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
