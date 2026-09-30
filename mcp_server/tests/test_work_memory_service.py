"""Actual temporary onboarding/ordinary authorization and execution binding."""
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from mcp_server.work_memory_service import WorkMemoryAccessService
from runtime.execution_binding import ExecutionBindingError, ExecutionStore, canonical_hash
from runtime.ordinary_access import authenticated_ordinary_operation
from runtime.work_memory import WorkMemoryStore
from tests import test_onboarding as onboarding_fixtures


class WorkMemoryServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = onboarding_fixtures.ModuleOneOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.onboarding = self.fixture.store
        self.database = self.fixture.database.resolve()
        self.common = {"owner_id": self.fixture.owner, "model_id": self.fixture.model}
        self.store = WorkMemoryStore(self.database)
        self.service = WorkMemoryAccessService(self.store, onboarding=self.onboarding, **self.common)
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("process forbidden")))

    def ordinary(self, *, scope="learning_memory", claim=None, **changes):
        return authenticated_ordinary_operation(**{**self.common, **changes}, scope=scope, claim=claim)

    def activate(self):
        self.fixture.bootstrap_live()  # Entirely synthetic module-one activation.

    def assert_reject(self, result, code):
        self.assertEqual("reject", result["decision"])
        self.assertEqual(code, result["reason_code"])
        self.assertFalse(result["state_changed"])
        self.assertFalse(result["stored"])

    def work_count(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("SELECT COUNT(*) FROM work_memory_items").fetchone()[0]

    def test_unactivated_remains_readonly_even_with_ordinary_context(self):
        with self.ordinary():
            self.assert_reject(self.service.remember("body", "tag"), "module_one_required")
            result = self.service.recall()
        self.assertEqual("recalled", result["decision"])
        self.assertEqual(0, self.work_count())

    def test_lifecycle_roundtrip_uses_same_ordinary_authorization(self):
        self.activate()
        with self.ordinary():
            first = self.service.remember("synthetic body", "synthetic lifecycle")
            retired = self.service.revise(first["target_ref"], lifecycle="retired")
            self.assertEqual("retired", retired["lifecycle"])
            self.assertEqual(0, self.service.recall()["count"])
            self.assert_reject(self.service.recall(target_ref=first["target_ref"]), "work_memory_not_found")
            old = self.service.recall(target_ref=first["target_ref"], include_retired=True)["results"][0]
            self.assertEqual(retired["target_ref"], old["current_target_ref"])
            self.assert_reject(self.service.revise(first["target_ref"], lifecycle="active"), "work_version_conflict")
            restored = self.service.revise(old["current_target_ref"], lifecycle="active")
            self.assertEqual("active", restored["lifecycle"])
            self.assertEqual(1, self.service.recall()["count"])

    def test_lifecycle_never_opens_new_permission_or_unbound_write(self):
        with self.ordinary():
            self.assert_reject(self.service.revise("work://" + "a" * 32 + "@1", lifecycle="retired"), "module_one_required")
        self.activate()
        with self.ordinary():
            first = self.service.remember("synthetic", "test")
        self.assert_reject(self.service.revise(first["target_ref"], lifecycle="retired"), "execution_binding_required")
        with self.ordinary(scope="emotional_memory"):
            self.assert_reject(self.service.revise(first["target_ref"], lifecycle="retired"), "execution_binding_required")
        with self.ordinary():
            self.assertEqual("active", self.service.recall()["results"][0]["lifecycle"])

    def test_invalid_lifecycle_rejected_before_binding(self):
        with patch.object(self.service, "_binding", side_effect=AssertionError("must not bind")):
            self.assert_reject(self.service.revise("work://" + "a" * 32 + "@1", lifecycle=True), "invalid_work_lifecycle")
        with self.ordinary():
            self.assert_reject(self.service.recall(include_retired="true"), "invalid_work_query")

    def test_lifecycle_does_not_bypass_protected_content_checks(self):
        self.activate()
        with self.ordinary():
            first = self.service.remember("synthetic", "test")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE work_memory_versions SET content='password=synthetic-secret'")
            db.commit()
        with self.ordinary():
            self.assert_reject(self.service.revise(first["target_ref"], lifecycle="retired"), "credential_or_secret_detected")

    def test_plain_call_without_bound_context_cannot_create(self):
        self.activate()
        self.assert_reject(self.service.remember("body", "tag"), "execution_binding_required")
        self.assertEqual(0, self.work_count())

    def test_authenticated_direct_roundtrip_edit_and_original_version(self):
        self.activate()
        original = "  Work original\r\n精确原文🙂  "
        with self.ordinary():
            first = self.service.remember(original, "名字 tag")
        self.assertTrue(first["stored"])
        with self.ordinary():
            edited = self.service.revise(first["target_ref"], tag="new tag")
            old = self.service.recall(target_ref=first["target_ref"])
            current = self.service.recall(query="new")
        self.assertEqual(2, edited["version"])
        self.assertEqual(original, old["results"][0]["content"])
        self.assertEqual(original, current["results"][0]["content"])

    def test_direct_retry_key_returns_same_result_without_duplication(self):
        self.activate()
        with self.ordinary():
            first = self.service.remember("body", "tag", request_id="a" * 32)
        with self.ordinary():
            replay = self.service.remember("body", "tag", request_id="a" * 32)
            changed = self.service.remember("changed", "tag", request_id="a" * 32)
        self.assertEqual(first["target_ref"], replay["target_ref"])
        self.assertFalse(replay["state_changed"])
        self.assert_reject(changed, "request_conflict")
        self.assertEqual(1, self.work_count())

    def test_wrong_ordinary_identity_scope_or_reference_cannot_write(self):
        self.activate()
        for context in (self.ordinary(owner_id="foreign"), self.ordinary(model_id="foreign"), self.ordinary(scope="emotional_memory")):
            with context:
                self.assert_reject(self.service.remember("body", "tag"), "execution_binding_required")
        with self.ordinary():
            self.assert_reject(self.service.remember("body", "tag", write_context_ref="wrong"), "write_context_binding_mismatch")
        self.assertEqual(0, self.work_count())

    def test_internal_ordinary_reference_is_accepted_but_not_returned(self):
        self.activate()
        with self.ordinary() as access:
            result = self.service.remember("body", "tag", write_context_ref=access["write_context_ref"])
        self.assertTrue(result["stored"])
        self.assertNotIn(access["write_context_ref"], json.dumps(result))

    def test_invalid_fields_reject_before_opening_context(self):
        with patch.object(self.onboarding, "open_brain_context", side_effect=AssertionError("should not open")):
            self.assert_reject(self.service.remember("", "tag"), "invalid_content")
            self.assert_reject(self.service.remember("body", ""), "invalid_tag")
            self.assert_reject(self.service.remember("body", "tag", request_id="wrong"), "invalid_request_id")
            self.assert_reject(self.service.revise("wrong", content="body"), "invalid_work_ref")

    def test_known_secret_value_rejected_without_echo_or_persistence(self):
        self.activate()
        secret = self.fixture.wake("synthetic-work-secret")[0]["wake_capability"]
        with self.ordinary():
            result = self.service.remember("mention " + secret, "tag")
        self.assert_reject(result, "credential_or_secret_detected")
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(0, self.work_count())

    def test_known_secret_in_existing_body_cannot_be_copied_by_tag_only_edit(self):
        self.activate()
        secret = self.fixture.wake("synthetic-work-existing-secret")[0]["wake_capability"]
        raw = self.store.remember(**self.common, content="ordinary body", tag="tag")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE work_memory_versions SET content=?", (secret,))
            db.commit()
        with self.ordinary():
            self.assert_reject(self.service.revise(raw["target_ref"], tag="changed"), "credential_or_secret_detected")
            self.assert_reject(self.service.recall(target_ref=raw["target_ref"]), "credential_or_secret_detected")

    def configured_service(self):
        # Deliberately lack credential prefixes/labels: syntax alone misses them.
        self.configured_secret = "0123456789abcdef0123456789abcdef"
        self.service = WorkMemoryAccessService(self.store, onboarding=self.onboarding, **self.common,
                                               protected_values=(self.configured_secret,))
        self.activate()

    def test_configured_secret_substrings_rejected_in_each_write_field_and_request_id(self):
        self.configured_service()
        with self.ordinary():
            for fields in ({"content": "prefix " + self.configured_secret, "tag": "tag"},
                           {"content": "body", "tag": self.configured_secret + " suffix"},
                           {"content": "body", "tag": "tag", "request_id": self.configured_secret}):
                result = self.service.remember(**fields)
                self.assert_reject(result, "credential_or_secret_detected")
                self.assertNotIn(self.configured_secret, json.dumps(result))
            first = self.service.remember("body", "tag")
            for fields in ({"content": self.configured_secret}, {"tag": self.configured_secret}):
                self.assert_reject(self.service.revise(first["target_ref"], **fields), "credential_or_secret_detected")
        self.assertEqual(1, self.work_count())

    def test_configured_secret_current_and_historical_reads_are_blocked(self):
        self.configured_service()
        first = self.store.remember(**self.common, content=self.configured_secret, tag="old")
        second = self.store.revise(**self.common, target_ref=first["target_ref"], content="clean", tag=self.configured_secret)
        with self.ordinary():
            for fields in ({"target_ref": first["target_ref"]}, {"target_ref": second["target_ref"]}, {}):
                result = self.service.recall(**fields)
                self.assert_reject(result, "credential_or_secret_detected")
                self.assertNotIn(self.configured_secret, json.dumps(result))

    def test_configured_secret_retained_field_cannot_be_copied_by_partial_edit(self):
        self.configured_service()
        body = self.store.remember(**self.common, content=self.configured_secret, tag="tag")
        tag = self.store.remember(**self.common, content="body", tag=self.configured_secret)
        with self.ordinary():
            self.assert_reject(self.service.revise(body["target_ref"], tag="changed"), "credential_or_secret_detected")
            self.assert_reject(self.service.revise(tag["target_ref"], content="changed"), "credential_or_secret_detected")
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(2, db.execute("SELECT COUNT(*) FROM work_memory_versions").fetchone()[0])

    def test_configured_secret_query_and_reference_reject_before_store_lookup(self):
        self.configured_service()
        with self.ordinary(), patch.object(self.store, "recall", side_effect=AssertionError("should not query")):
            self.assert_reject(self.service.recall(query=self.configured_secret), "credential_or_secret_detected")
            self.assert_reject(self.service.recall(target_ref="work://" + self.configured_secret + "@1"), "credential_or_secret_detected")

    def test_nested_result_keys_values_and_cycles_fail_closed(self):
        self.configured_service()
        for value in ({self.configured_secret: "not returned"}, {"records": [{"content": self.configured_secret}]},
                      {"text": "orb_atlas_" + "A" * 43}):
            with self.assertRaisesRegex(ValueError, "credential_or_secret_detected"):
                self.service._result(value)
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaisesRegex(ValueError, "credential_or_secret_detected"):
            self.service._protected(cyclic)

    def test_atlas_credentials_never_persist_or_escape_from_old_records(self):
        self.activate()
        token = "orb_atlas_" + "A" * 43
        with self.ordinary():
            for fields in ({"content": token, "tag": "tag"}, {"content": "body", "tag": token}):
                self.assert_reject(self.service.remember(**fields), "credential_or_secret_detected")
            self.assert_reject(self.service.recall(query=token), "credential_or_secret_detected")
        first = self.store.remember(**self.common, content="ordinary body", tag="tag")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE work_memory_versions SET content=?", (token,))
            db.commit()
        with self.ordinary():
            self.assert_reject(self.service.recall(target_ref=first["target_ref"]), "credential_or_secret_detected")
            self.assert_reject(self.service.revise(first["target_ref"], tag="changed"), "credential_or_secret_detected")

    def test_permission_is_rechecked_inside_actual_write_transaction(self):
        self.activate()
        original = self.store.remember
        def race(**kwargs):
            with closing(sqlite3.connect(self.database)) as db:
                db.execute("UPDATE brain_module_unlocks SET unlocked=0 WHERE owner_id=? AND model_id=?", tuple(self.common.values()))
                db.commit()
            return original(**kwargs)
        with self.ordinary(), patch.object(self.store, "remember", side_effect=race):
            self.assert_reject(self.service.remember("body", "tag"), "module_one_required")
        self.assertEqual(0, self.work_count())

    def test_unknown_binding_exception_is_fixed_code_not_private_text(self):
        self.activate()
        with self.ordinary(), patch.object(self.onboarding, "current_open_write_context", side_effect=ExecutionBindingError("DO_NOT_ECHO")):
            result = self.service.remember("body", "tag")
        self.assert_reject(result, "work_memory_operation_rejected")
        self.assertNotIn("DO_NOT_ECHO", json.dumps(result))

    def test_cross_identity_explicit_reference_is_not_found(self):
        self.activate()
        foreign = self.store.remember(owner_id="other", model_id=self.common["model_id"], content="foreign", tag="foreign")
        with self.ordinary():
            self.assert_reject(self.service.recall(target_ref=foreign["target_ref"]), "work_memory_not_found")
            self.assert_reject(self.service.revise(foreign["target_ref"], content="overwritten"), "work_memory_not_found")

    def test_original_business_tables_are_unchanged_by_work_write_and_query(self):
        self.activate()
        def business():
            with closing(sqlite3.connect(self.database)) as db:
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                          if not row[0].startswith("work_memory_")]
                return {name: db.execute('SELECT * FROM "' + name + '"').fetchall() for name in tables}
        before = business()
        with self.ordinary():
            first = self.service.remember("distinctive original", "distinctive tag")
            self.service.revise(first["target_ref"], content="updated")
            self.service.recall(query="distinctive")
        self.assertEqual(before, business())

    def test_work_content_and_tag_never_enter_daily_projection_or_atlas(self):
        from runtime.atlas_metadata import AtlasMetadataReader, AtlasScope
        from runtime.emotional_memory import EmotionalMemoryStore
        from runtime.learning_memory import LearningMemoryStore
        from runtime.planning_memory import PlanningMemoryStore
        self.onboarding.emotional_store = EmotionalMemoryStore(self.database)
        self.onboarding.learning_store = LearningMemoryStore(
            self.database, idea_database=self.database.with_name("synthetic-work-ideas.sqlite"))
        self.onboarding.planning_store = PlanningMemoryStore(self.database)
        self.activate()
        content, tag = "UNIQUE_WORK_ORIGINAL_MUST_STAY_EXPLICIT", "UNIQUE_WORK_TAG_MUST_STAY_EXPLICIT"
        with self.ordinary():
            self.assertTrue(self.service.remember(content, tag)["stored"])
        wake = self.onboarding.issue_wake(**self.common, host_id="synthetic-host", thread_id="synthetic-thread",
                                          source_kind="human_message", source_event_id="synthetic-work-no-recall")
        prepared = self.onboarding.build_pre_generation_context(
            **self.common, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
            source_digest="synthetic-source", host_contract_digest="synthetic-contract",
            source_frame={"query_text": tag, "thread_id": "synthetic-thread", "lineage_stable": True,
                          "prior_assistant_present": True, "first_user_turn": False,
                          "source_event_id": "synthetic-work-no-recall", "capture_items": []})
        projection = json.dumps(prepared, ensure_ascii=False)
        self.assertNotIn(content, projection)
        self.assertNotIn(tag, projection)
        graph = json.loads(AtlasMetadataReader(AtlasScope(self.database, self.common["owner_id"], self.common["model_id"], b"k" * 32)).read())
        self.assertEqual([], graph["stars"])
        self.assertEqual([], graph["edges"])

    def prepare_claim(self, tool, arguments):
        self.executions = ExecutionStore(self.database, deployment_epoch="synthetic-work-epoch",
                                        capability_secret=b"synthetic-execution-secret-over-32-bytes")
        schema_hash = canonical_hash({"type": "object", "synthetic": True})
        entries = [{"canonical_name": tool, "schema_hash": schema_hash}]
        catalog = {"contract": "advertised-tools/1", "catalog_complete": True,
                   "catalog_hash": canonical_hash(entries), "entries": entries}
        wake = self.onboarding.issue_wake(**self.common, host_id="synthetic-host", thread_id="synthetic-thread",
                                          source_kind="human_message", source_event_id="synthetic-work")
        prepared = self.onboarding.build_pre_generation_context(**self.common, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], source_digest="synthetic-source",
            host_contract_digest="synthetic-contract", advertised_tools=catalog)
        self.onboarding.confirm_context_injected(**self.common, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], context_hash=prepared["context_hash"])
        issued = self.executions.issue_batch(**self.common, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
            batch_id="synthetic-work-batch", revision=1, calls=[{"call_id": "synthetic-work-call", "canonical_tool": tool,
                "advertised_name": tool, "schema_hash": schema_hash, "catalog_hash": catalog["catalog_hash"],
                "arguments_hash": canonical_hash(arguments)}])
        return self.executions.claim(**self.common, execution_ref=issued["executions"][0]["execution_ref"],
                                      tool_name=tool, arguments=arguments)

    def test_real_gateway_claim_creates_idempotently_without_storing_raw_lease(self):
        self.activate()
        arguments = {"content": "gateway body", "tag": "gateway tag"}
        claim = self.prepare_claim("remember_work_memory", arguments)
        with self.executions.bind(claim), self.ordinary(claim=claim):
            first = self.service.remember(**arguments)
            replay = self.service.remember(**arguments)
        self.assertTrue(first["stored"])
        self.assertEqual(first["target_ref"], replay["target_ref"])
        self.assertFalse(replay["state_changed"])
        with closing(sqlite3.connect(self.database)) as db:
            serialized = json.dumps(db.execute("SELECT * FROM work_memory_versions").fetchall())
        for secret in (claim.execution_ref, claim.claim_id, claim.batch_id, claim.wake_id):
            self.assertNotIn(secret, serialized)
        self.executions.finish(claim)
        with self.executions.bind(claim), self.ordinary(claim=claim):
            result = self.service.remember(**arguments)
        self.assertFalse(result["stored"])

    def test_gateway_claim_cannot_supply_direct_request_id_or_wrong_identity(self):
        self.activate()
        arguments = {"content": "body", "tag": "tag"}
        claim = self.prepare_claim("remember_work_memory", arguments)
        with self.executions.bind(claim), self.ordinary(claim=claim):
            self.assert_reject(self.service.remember(**arguments, request_id="a" * 32), "request_id_conflicts_with_bound_execution")
        for changed in (replace(claim, owner_id="foreign"), replace(claim, model_id="foreign"),
                        replace(claim, tool_name="recall_work_memory")):
            with self.executions.bind(changed):
                self.assert_reject(self.service.remember(**arguments), "execution_owner_mismatch")
        self.assertEqual(0, self.work_count())


if __name__ == "__main__":
    unittest.main()
