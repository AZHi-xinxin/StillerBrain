"""Ordinary-revision facade/store tests: synthetic databases only, no services.

The facade matrix uses a fake host boundary; the final integration class uses
real synthetic onboarding/execution leases, never a person's database or wake.
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
from mcp_server.daily_revision_service import DailyRevisionAccessService, parse_revision_target
from mcp_server.emotional_service import EmotionalMemoryAccessService
from mcp_server.learning_service import LearningMemoryAccessService
from mcp_server.planning_service import PlanningMemoryAccessService
from mcp_server.tests.test_daily_memory_service import SyntheticHost, OWNER, MODEL
from runtime.emotional_memory import EmotionalMemoryStore, EmotionalMemoryError
from runtime.learning_memory import LearningMemoryStore, LearningMemoryError
from runtime.planning_memory import PlanningMemoryStore
from runtime.execution_binding import ExecutionClaim, ExecutionStore, ExecutionBindingError, canonical_hash


class OrdinaryRevisionTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("socket.socket", side_effect=AssertionError("offline test")))
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("offline test")))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("offline test")))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="revision-synthetic-")))
        self.database, ideas = self.directory / "main.sqlite3", self.directory / "ideas.sqlite3"
        connect = sqlite3.connect
        allowed = {self.database.resolve(), ideas.resolve()}
        def synthetic_connect(path, *args, **kwargs):
            if Path(path).resolve() not in allowed:
                raise AssertionError("only synthetic databases")
            return connect(path, *args, **kwargs)
        self.enterContext(patch("sqlite3.connect", side_effect=synthetic_connect))
        self.host = SyntheticHost()
        common = dict(onboarding=self.host, owner_id=OWNER, model_id=MODEL)
        self.emotional = EmotionalMemoryAccessService(EmotionalMemoryStore(self.database), **common)
        self.learning = LearningMemoryAccessService(LearningMemoryStore(self.database, idea_database=ideas), **common)
        self.planning = PlanningMemoryAccessService(PlanningMemoryStore(self.database), **common)
        self.daily = DailyMemoryAccessService(self.host, self.emotional, self.learning, self.planning, OWNER, MODEL)
        self.revisions = DailyRevisionAccessService(self.host, self.emotional, self.learning, self.planning, OWNER, MODEL)
        self.claim = ExecutionClaim("synthetic-execution", OWNER, MODEL, self.host.wake_id,
                                    "synthetic-batch", "synthetic-call", "revise_memory",
                                    "synthetic-epoch", "synthetic-claim", str(self.database))
        self.bound = self.enterContext(patch("mcp_server.daily_revision_service.current_execution_claim", return_value=self.claim))
        self.original = "  Synthetic unchanged original\nwith trailing spacing.  "
        self.refs = {}
        with patch("mcp_server.daily_memory_service.current_execution_claim", return_value=replace(self.claim, tool_name="remember_memory")):
            for module in self.daily.services:
                result = self.daily.remember(module, self.original, title="Synthetic title", summary="Synthetic summary")
                self.assertEqual("stored", result["decision"])
                self.refs[module] = result["ref"]
        self.host.open_calls.clear()
        self.host.binding_calls.clear()

    def rows(self, table):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertIn(table, {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")})
            return [dict(row) for row in connection.execute('SELECT * FROM "' + table + '"')]

    def snapshot(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return tuple(connection.iterdump())

    def revise(self, module="learning_memory", changes=None, **fields):
        return self.revisions.revise(self.refs[module], {"summary": "Synthetic corrected summary"} if changes is None else changes, **fields)

    def test_three_modules_one_call_exact_version_and_compact_receipt(self):
        for module in self.refs:
            with self.subTest(module=module):
                result = self.revise(module)
                self.assertEqual("revised", result["decision"], result)
                self.assertEqual(2, result["version"])
                self.assertEqual(self.refs[module], result["previous_ref"])
                self.assertNotIn(self.original, json.dumps(result))
                self.assertNotIn(self.host.ref, json.dumps(result))
                self.assertEqual(self.claim.wake_id, self.host.open_calls[-1]["expected_wake_id"])
                self.assertIs(False, self.host.open_calls[-1]["present_details"])

    def test_original_and_unmodified_learning_values_and_versions_are_preserved(self):
        before = json.loads(self.rows("learning_items")[0]["current_json"])
        old_version = self.rows("learning_versions")[0]
        result = self.revise(changes={"summary": "Synthetic corrected", "title": "New title", "keywords": ["retrieval"]})
        self.assertTrue(result["revised"])
        after = json.loads(self.rows("learning_items")[0]["current_json"])
        self.assertEqual(self.original, after["current_understanding"])
        for key in set(before) - {"summary", "title", "keywords"}:
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(old_version, self.rows("learning_versions")[0])
        added = self.rows("learning_versions")[-1]
        self.assertEqual(1, added["previous_version"])
        self.assertIn("未执行独立核验", added["correctness_assessment"])
        self.assertTrue(added["ai_diff"].startswith("server_computed_fields:"))
        self.assertEqual([], self.rows("learning_verification_events"))
        self.assertEqual([], self.rows("learning_change_candidates"))

    def test_emotional_original_policy_and_history_unchanged(self):
        before = self.rows("emotion_memories")[0]
        old_version = self.rows("emotion_memory_versions")[0]
        result = self.revise("emotional_memory", {"summary": "New summary", "importance": 61})
        self.assertTrue(result["revised"])
        after = self.rows("emotion_memories")[0]
        for key in set(before) - {"summary", "importance", "current_version", "updated_at"}:
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(self.original, after["original_text"])
        self.assertEqual(old_version, self.rows("emotion_memory_versions")[0])
        self.assertEqual(2, len(self.rows("emotion_memory_versions")))

    def test_stale_target_is_never_promoted_to_latest_or_retried(self):
        for module in self.refs:
            self.assertTrue(self.revise(module)["revised"])
            before = self.snapshot()
            store = self.revisions.services[module].store
            with patch.object(store, "revise_ordinary", wraps=store.revise_ordinary) as callback:
                result = self.revise(module, {"summary": "Unseen target overwrite"})
            self.assertFalse(result["revised"])
            self.assertEqual(1, callback.call_count)
            self.assertEqual(before, self.snapshot())

    def test_module_cas_race_rolls_back_entire_target_version(self):
        for module in self.refs:
            component = self.revisions.services[module]
            before = self.snapshot()
            with patch.object(component, "status", return_value={"row_version": 0}):
                result = self.revise(module)
            self.assertFalse(result["revised"])
            self.assertEqual(before, self.snapshot())

    def test_invalid_refs_rejected_before_open_or_lookup(self):
        for target in (None, "", "learning://learn_" + "a" * 32, self.refs["learning_memory"] + " ",
                       self.refs["learning_memory"].replace("@1", "@latest"),
                       self.refs["learning_memory"].replace("@1", "@0"),
                       self.refs["learning_memory"].replace("@1", "@01"),
                       self.refs["learning_memory"].replace("learning:", "emotion:"),
                       "self://core@1", {"malformed": True}):
            self.assertEqual("versioned_target_ref_required", self.revisions.revise(target, {"summary": "x"})["reason_codes"][0])
        self.assertEqual([], self.host.open_calls)

    def test_advanced_fields_never_create_candidate_or_fake_verification(self):
        blocked = {
            "emotional_memory": {"original_text", "origin", "confidence", "sensitivity", "disclosure", "lifecycle", "referent_bindings"},
            "learning_memory": {"current_understanding", "source_basis", "claim_review", "confidence", "evidence", "steps", "disclosure", "lifecycle", "referent_bindings"},
            "planning_memory": {"goal", "original_text", "status", "completion_evidence", "parent_ref", "adoption_statement"},
        }
        before = self.snapshot()
        for module, keys in blocked.items():
            for key in keys:
                result = self.revise(module, {key: "synthetic"})
                self.assertEqual("ordinary_revision_requires_advanced", result["reason_codes"][0])
                self.assertIn("advanced_tool", result)
        self.assertEqual([], self.host.open_calls)
        self.assertEqual(before, self.snapshot())

    def test_no_claim_never_looks_up_latest_wake(self):
        self.bound.return_value = None
        self.assertEqual("execution_binding_required", self.revise()["reason_codes"][0])
        self.assertEqual([], self.host.open_calls)
        self.assertEqual([], self.host.binding_calls)

    def test_foreign_claim_wrong_tool_and_foreign_wake_fail_closed(self):
        before = self.snapshot()
        for claim in (replace(self.claim, owner_id="foreign"), replace(self.claim, model_id="foreign"),
                      replace(self.claim, tool_name="remember_memory"), object()):
            self.bound.return_value = claim
            self.assertFalse(self.revise()["revised"])
        self.assertEqual([], self.host.open_calls)
        self.bound.return_value = replace(self.claim, wake_id="old-wake")
        self.assertEqual("execution_wake_mismatch", self.revise()["reason_codes"][0])
        self.assertEqual(before, self.snapshot())

    def test_direct_ref_requires_matching_human_attested_scope_without_open(self):
        self.bound.return_value = None
        self.assertEqual("invalid_direct_context", self.revise(write_context_ref=self.host.ref)["reason_codes"][0])
        self.host.mode = "human_attested_direct"
        self.host.scopes = {"emotional_memory"}
        self.assertFalse(self.revise(write_context_ref=self.host.ref)["revised"])
        self.assertTrue(self.revise("emotional_memory", write_context_ref=self.host.ref)["revised"])
        self.assertEqual([], self.host.open_calls)

    def test_explicit_ref_does_not_override_bound_claim(self):
        self.assertEqual("explicit_context_conflicts_with_bound_execution", self.revise(write_context_ref=self.host.ref)["reason_codes"][0])
        self.assertEqual([], self.host.open_calls)

    def test_actual_module_permission_secret_gates_not_skipped(self):
        before = self.snapshot()
        self.host.allowed = False
        self.assertEqual("module_one_required", self.revise()["reason_codes"][0])
        self.host.allowed = True
        for module in self.refs:
            result = self.revise(module, {"summary": "synthetic-protected-value"})
            self.assertEqual("credential_or_secret_detected", result["reason_codes"][0])
        self.assertEqual(before, self.snapshot())

    def test_bad_input_no_change_and_noop_do_not_bump_state(self):
        before = self.snapshot()
        for module in self.refs:
            for changes in ({}, {"importance": True}, {"importance": 101}, {"summary": " "},
                            {"summary": "x" * 10000}, {"keywords": "not-list"}, {"keywords": [""]}):
                self.assertFalse(self.revise(module, changes)["revised"])
            self.assertFalse(self.revise(module, {"summary": "Synthetic summary"})["revised"])
        self.assertEqual(before, self.snapshot())

    def test_reason_optional_is_neutral_and_supplied_reason_is_not_rewritten(self):
        self.assertTrue(self.revise("emotional_memory", reason="My actual correction reason")["revised"])
        self.assertEqual("My actual correction reason", self.rows("emotion_memory_versions")[-1]["reason"])
        self.assertTrue(self.revise()["revised"])
        self.assertIn("不代表独立核验", self.rows("learning_versions")[-1]["reason"])

    def test_callback_binding_race_is_not_attached_to_new_wake(self):
        before = self.snapshot()
        original = self.host.current_open_write_context
        count = 0
        def racing(**fields):
            nonlocal count
            count += 1
            result = original(**fields)
            if count > 1:
                result["wake_id"] = "foreign-new-wake"
            return result
        with patch.object(self.host, "current_open_write_context", side_effect=racing):
            self.assertEqual("execution_wake_mismatch", self.revise()["reason_codes"][0])
        self.assertEqual(before, self.snapshot())

    def test_runtime_foreign_owner_and_bool_or_stale_version_fail(self):
        module, item_id, version = parse_revision_target(self.refs["learning_memory"])
        args = dict(owner_id=OWNER, model_id=MODEL, wake_id=self.host.wake_id, wake_seq=1,
                    expected_row_version=self.learning.status()["row_version"], learning_id=item_id,
                    expected_item_version=version, changes={"summary": "new"}, reason="correction")
        before = self.snapshot()
        for change in ({"owner_id": "foreign"}, {"model_id": "foreign"}, {"expected_item_version": True},
                       {"expected_item_version": 2}, {"changes": {"current_understanding": "forbidden"}}):
            with self.assertRaises(LearningMemoryError):
                self.learning.store.revise_ordinary(**{**args, **change})
        self.assertEqual(before, self.snapshot())

    def test_runtime_emotion_original_cannot_be_rewritten_even_without_facade(self):
        _, item_id, version = parse_revision_target(self.refs["emotional_memory"])
        before = self.snapshot()
        with self.assertRaisesRegex(EmotionalMemoryError, "ordinary_revision_requires_advanced"):
            self.emotional.store.revise_ordinary(owner_id=OWNER, model_id=MODEL, wake_id=self.host.wake_id,
                expected_row_version=1, memory_id=item_id, expected_memory_version=version,
                changes={"original_text": "forbidden"}, reason="correction")
        self.assertEqual(before, self.snapshot())

    def test_uncertain_success_receipt_never_claims_zero_state_change_or_retries(self):
        for fake in (None, {"decision": "revised", "state_changed": True},
                     {"decision": "candidate_pending", "state_changed": True}):
            with patch.object(self.learning, "_write", return_value=fake) as write:
                result = self.revise()
            self.assertEqual("not_confirmed_revised", result["decision"])
            self.assertNotIn("state_changed", result)
            self.assertEqual(1, write.call_count)

    def event_seq(self):
        item_id = parse_revision_target(self.refs["planning_memory"])[1]
        return max(row["event_seq"] for row in self.rows("planning_events") if row["plan_id"] == item_id)

    def advance(self, kind, *, seq=None, evidence=None, target=None, note="Synthetic actual progress"):
        self.bound.return_value = replace(self.claim, tool_name="advance_plan")
        return self.revisions.advance(target or self.refs["planning_memory"],
            self.event_seq() if seq is None else seq, kind, note, evidence=evidence)

    @staticmethod
    def evidence():
        return [{"source_kind": "human_report", "source_ref": "message://synthetic-progress",
                 "evidence_summary": "Synthetic report received", "provenance": "reported"}]

    def test_advance_append_only_pause_resume_and_evidenced_completion(self):
        initial_version = self.rows("planning_versions")[0]
        for event_type, state, evidence in (("pause", "paused", None), ("resume", "active", None),
                                            ("progress", "active", self.evidence()),
                                            ("complete", "completed", self.evidence()),
                                            ("reopen", "active", self.evidence())):
            old_seq = self.event_seq()
            result = self.advance(event_type, evidence=evidence)
            self.assertEqual("event_recorded", result["decision"], result)
            self.assertEqual(state, result["state"])
            self.assertEqual(old_seq, result["previous_event_seq"])
            self.assertGreater(result["event_seq"], old_seq)
            self.assertEqual(1, result["version"])
            self.assertNotIn("evidence", result)
            self.assertEqual("Synthetic actual progress", self.rows("planning_events")[-1]["reason"])
        self.assertEqual([initial_version], self.rows("planning_versions"))

    def test_advance_does_not_fabricate_required_evidence(self):
        before = self.snapshot()
        for kind in ("progress", "complete", "reopen"):
            result = self.advance(kind)
            self.assertEqual("event_evidence_required", result["reason_codes"][0])
        self.assertEqual([], self.host.open_calls)
        self.assertEqual(before, self.snapshot())

    def test_advance_observed_event_seq_cas_not_latest_and_no_retry(self):
        seq = self.event_seq()
        self.assertEqual("event_recorded", self.advance("pause", seq=seq)["decision"])
        before = self.snapshot()
        with patch.object(self.planning.store, "record_ordinary_event", wraps=self.planning.store.record_ordinary_event) as callback:
            rejected = self.advance("resume", seq=seq)
        self.assertEqual("plan_event_seq_conflict", rejected["reason_codes"][0])
        self.assertEqual(1, callback.call_count)
        self.assertEqual(before, self.snapshot())

    def test_advance_old_content_ref_cannot_change_current_state(self):
        self.assertTrue(self.revise("planning_memory")["revised"])
        before = self.snapshot()
        self.assertEqual("plan_version_conflict", self.advance("pause")["reason_codes"][0])
        self.assertEqual(before, self.snapshot())

    def test_advance_identity_scope_and_input_gates(self):
        before = self.snapshot()
        self.assertEqual("execution_owner_mismatch", self.revisions.advance(self.refs["planning_memory"], self.event_seq(), "pause", "actual")["reason_codes"][0])
        for seq in (-1, True, "1", None):
            self.assertEqual("invalid_expected_event_seq", self.revisions.advance(self.refs["planning_memory"], seq, "pause", "actual")["reason_codes"][0])
        self.assertEqual("planning_target_required", self.advance("pause", target=self.refs["learning_memory"])["reason_codes"][0])
        self.assertFalse(self.advance("pause", note="synthetic-protected-value")["revised"])
        self.assertEqual(before, self.snapshot())

    def test_advance_other_plan_events_may_make_global_sequence_jump(self):
        expected = self.event_seq()
        with patch("mcp_server.daily_memory_service.current_execution_claim", return_value=replace(self.claim, tool_name="remember_memory", call_id="another")):
            self.assertEqual("stored", self.daily.remember("planning_memory", "Another synthetic plan")["decision"])
        result = self.advance("pause", seq=expected)
        self.assertEqual("event_recorded", result["decision"], result)
        self.assertGreater(result["event_seq"], expected + 1)
        self.assertEqual(expected, result["previous_event_seq"])

    def test_advance_partial_receipt_does_not_lie_about_state_or_retry(self):
        self.bound.return_value = replace(self.claim, tool_name="advance_plan")
        with patch.object(self.planning, "_write", return_value={"decision": "event_recorded", "state_changed": True}) as write:
            result = self.advance("pause")
        self.assertEqual("not_confirmed_event", result["decision"])
        self.assertNotIn("state_changed", result)
        self.assertEqual(1, write.call_count)


class OrdinaryRevisionExecutionTests(unittest.TestCase):
    """Real lease issue/claim/transaction fencing using a synthetic host database."""
    def setUp(self):
        from mcp_server.tests.test_compact_open import CompactOpenStateTests
        self.fixture = CompactOpenStateTests(methodName="test_summary_still_requires_current_confirmed_injection")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("offline test")))
        self.database = self.fixture.database.resolve()
        ideas = (Path(self.fixture.temp.name) / "revision-ideas.sqlite3").resolve()
        connect = sqlite3.connect
        def synthetic_connect(path, *args, **kwargs):
            if Path(path).resolve() not in {self.database, ideas}:
                raise AssertionError("only synthetic databases")
            return connect(path, *args, **kwargs)
        self.enterContext(patch("sqlite3.connect", side_effect=synthetic_connect))
        self.fixture.bootstrap_live()  # Existing fixture name: only synthetic module-one activation.
        self.host = self.fixture.onboarding
        self.common = dict(owner_id=self.fixture.service.owner_id, model_id=self.fixture.service.model_id)
        self.services = [EmotionalMemoryAccessService(EmotionalMemoryStore(self.database), onboarding=self.host, **self.common),
            LearningMemoryAccessService(LearningMemoryStore(self.database, idea_database=ideas), onboarding=self.host, **self.common),
            PlanningMemoryAccessService(PlanningMemoryStore(self.database), onboarding=self.host, **self.common)]
        self.daily = DailyMemoryAccessService(self.host, *self.services, **self.common)
        self.revisions = DailyRevisionAccessService(self.host, *self.services, **self.common)
        self.executions = ExecutionStore(self.database, deployment_epoch="synthetic-revision-epoch",
                                         capability_secret=b"synthetic-revision-secret-at-least-32bytes")
        self.schema_hash = canonical_hash({"type": "object", "synthetic": True})
        entries = [{"canonical_name": name, "schema_hash": self.schema_hash}
                   for name in ("remember_memory", "revise_memory", "advance_plan")]
        self.catalog = {"contract": "advertised-tools/1", "catalog_complete": True,
                        "catalog_hash": canonical_hash(entries), "entries": entries}
        self.wake = self.host.issue_wake(**self.common, host_id="synthetic-host", thread_id="synthetic-thread",
                                         source_kind="human_message", source_event_id="synthetic-revision")
        prepared = self.host.build_pre_generation_context(**self.common, wake_id=self.wake["wake_id"],
            wake_capability=self.wake["wake_capability"], source_digest="synthetic-source",
            host_contract_digest="synthetic-contract", advertised_tools=self.catalog)
        self.host.confirm_context_injected(**self.common, wake_id=self.wake["wake_id"],
            wake_capability=self.wake["wake_capability"], context_hash=prepared["context_hash"])
        self.call_seq = 0
        self.refs = {}
        for module in self.daily.services:
            result, _ = self.execute("remember_memory", {"module": module, "content": "  Synthetic original\n "})
            self.assertTrue(result["stored"])
            self.refs[module] = result["ref"]

    def execute(self, tool_name, arguments):
        self.call_seq += 1
        issued = self.executions.issue_batch(**self.common, wake_id=self.wake["wake_id"],
            wake_capability=self.wake["wake_capability"], batch_id=f"synthetic-batch-{self.call_seq}", revision=1,
            calls=[{"call_id": f"synthetic-call-{self.call_seq}", "advertised_name": tool_name,
                    "canonical_tool": tool_name, "schema_hash": self.schema_hash,
                    "catalog_hash": self.catalog["catalog_hash"], "arguments_hash": canonical_hash(arguments)}])
        claim = self.executions.claim(**self.common, execution_ref=issued["executions"][0]["execution_ref"],
                                      tool_name=tool_name, arguments=arguments)
        try:
            with self.executions.bind(claim):
                callback = {"remember_memory": self.daily.remember, "revise_memory": self.revisions.revise,
                            "advance_plan": self.revisions.advance}[tool_name]
                return callback(**arguments), claim
        finally:
            self.executions.finish(claim)

    def test_three_modules_real_claim_binding_and_completed_claim_cannot_replay(self):
        before = self.fixture.service.module_one_status()["state"]
        for module, target in self.refs.items():
            args = {"target_ref": target, "changes": {"summary": "Synthetic corrected summary"}}
            result, claim = self.execute("revise_memory", args)
            self.assertTrue(result["revised"], result)
            self.assertNotIn(claim.execution_ref, json.dumps(result))
            with self.executions.bind(claim):
                rejected = self.revisions.revise(**args)
            self.assertFalse(rejected["revised"])
            with self.assertRaises(ExecutionBindingError):
                self.executions.claim(**self.common, execution_ref=claim.execution_ref, tool_name="revise_memory", arguments=args)
        self.assertEqual(before, self.fixture.service.module_one_status()["state"])
        self.assertEqual(0, self.fixture.proof_count("candidate_full_review", self.wake))
        self.assertEqual(0, self.fixture.proof_count("candidate_objection_full_review", self.wake))

    def test_finished_claim_is_rejected_inside_ordinary_write_transaction(self):
        args = {"target_ref": self.refs["learning_memory"], "changes": {"summary": "Synthetic corrected"}}
        result, claim = self.execute("revise_memory", args)
        _, item_id, version = parse_revision_target(result["ref"])
        with self.executions.bind(claim), self.assertRaisesRegex(ExecutionBindingError, "execution_claim_not_current"):
            self.services[1].store.revise_ordinary(**self.common, wake_id=self.wake["wake_id"], wake_seq=1,
                expected_row_version=self.services[1].status()["row_version"], learning_id=item_id,
                expected_item_version=version, changes={"summary": "forbidden stale execution"}, reason="actual")

    def test_real_execution_bound_advance_preserves_plan_version_and_checks_observed_event(self):
        with closing(sqlite3.connect(self.database)) as connection:
            seq = connection.execute("SELECT MAX(event_seq) FROM planning_events").fetchone()[0]
        args = {"target_ref": self.refs["planning_memory"], "expected_event_seq": seq,
                "event_type": "pause", "note": "Synthetic actual pause"}
        result, _ = self.execute("advance_plan", args)
        self.assertEqual("event_recorded", result["decision"], result)
        self.assertEqual(self.refs["planning_memory"], result["ref"])
        rejected, _ = self.execute("advance_plan", {**args, "event_type": "resume"})
        self.assertEqual("plan_event_seq_conflict", rejected["reason_codes"][0])

    def test_unbound_request_does_not_guess_real_current_wake(self):
        result = self.revisions.revise(self.refs["learning_memory"], {"summary": "not authorized"})
        self.assertEqual("execution_binding_required", result["reason_codes"][0])


if __name__ == "__main__":
    unittest.main()
