"""Actual synthetic onboarding/claims; no private memories, model or network."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import patch

from mcp_server.memory_relation_service import MemoryRelationAccessService
from runtime.execution_binding import ExecutionStore, ExecutionBindingError, canonical_hash
from runtime.memory_relations import MemoryRelationStore, MemoryRelationError
from runtime.ordinary_access import authenticated_ordinary_operation
from tests import test_onboarding as fixtures
from tests.test_atlas_metadata import create_runtime_database
from tests.test_memory_relations import seed_endpoints, OWNER


class MemoryRelationServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ModuleOneOnboardingTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.fixture.owner, self.fixture.model = OWNER.values()
        self.database, self.onboarding = self.fixture.database, self.fixture.store
        create_runtime_database(self.database); seed_endpoints(self.database)
        self.store = MemoryRelationStore(self.database)
        self.service = MemoryRelationAccessService(self.store, onboarding=self.onboarding, **OWNER)
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("process forbidden")))

    def ordinary(self, *, claim=None, **changes):
        return authenticated_ordinary_operation(**{**OWNER, "scope": "memory_relations", **changes}, claim=claim)

    def attach(self, **changes):
        return self.service.attach(**{"from_ref": "emotion://e1@1", "to_ref": "learning://l1@1", "type": "related_to", **changes})

    def reject(self, result, reason):
        self.assertEqual("reject", result["decision"])
        self.assertEqual(reason, result["reason_code"])
        self.assertFalse(result["state_changed"])

    def count(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute("SELECT count(*) FROM memory_relation_versions").fetchone()[0]

    def test_activation_required_but_authenticated_read_stays_readonly(self):
        with self.ordinary():
            self.reject(self.attach(), "module_one_required")
            self.assertEqual([], self.service.read("emotion://e1@1")["relations"])
        self.assertEqual(0, self.count())

    def test_no_plain_write_without_bound_or_ordinary_context(self):
        self.fixture.bootstrap_live()
        self.reject(self.attach(), "execution_binding_required")
        self.assertEqual(0, self.count())

    def test_ordinary_roundtrip_and_detached_old_request_never_resurrects(self):
        self.fixture.bootstrap_live()
        with self.ordinary():
            first = self.attach(request_id="a" * 32)
            duplicate = self.attach(request_id="a" * 32)
            self.assertEqual(first["edge_ref"], duplicate["edge_ref"])
            self.assertFalse(duplicate["state_changed"])
            removed = self.service.detach(first["edge_ref"], request_id="b" * 32)
            replay = self.attach(request_id="a" * 32)
            self.assertEqual(removed["edge_ref"], replay["edge_ref"])
            self.assertFalse(replay["active"])
            self.assertEqual([], self.service.read("emotion://e1@1")["relations"])
        self.assertEqual(2, self.count())

    def test_wrong_ordinary_identity_scope_and_explicit_context_rejected(self):
        self.fixture.bootstrap_live()
        for change in ({"owner_id": "foreign"}, {"model_id": "foreign"}, {"scope": "emotional_memory"}):
            with self.ordinary(**change): self.reject(self.attach(), "execution_binding_required")
        with self.ordinary(): self.reject(self.attach(write_context_ref="incorrect"), "write_context_binding_mismatch")
        self.assertEqual(0, self.count())

    def test_cross_owner_and_model_endpoints_unavailable_without_disclosure(self):
        self.fixture.bootstrap_live()
        for field in ("owner_id", "model_id"):
            with closing(sqlite3.connect(self.database)) as db:
                db.execute("UPDATE learning_items SET " + field + "='foreign'"); db.commit()
            with self.ordinary(): self.reject(self.attach(), "relation_endpoint_unavailable")
            with closing(sqlite3.connect(self.database)) as db:
                db.execute("UPDATE learning_items SET " + field + "=?", (OWNER[field],)); db.commit()
        self.assertEqual(0, self.count())

    def test_known_configured_and_atlas_secrets_rejected_everywhere(self):
        self.fixture.bootstrap_live()
        secret = "0123456789abcdef0123456789abcdef"
        self.service = MemoryRelationAccessService(self.store, onboarding=self.onboarding, **OWNER, protected_values=(secret,))
        with self.ordinary():
            for value in (secret, "orb_atlas_" + "A" * 43):
                for args in ({"type": "custom", "label": value}, {"type": "custom", "label": "safe", "reverse_label": value},
                             {"request_id": value}, {"from_ref": "emotion://" + value + "@1"}):
                    result = self.attach(**args)
                    self.reject(result, "credential_or_secret_detected")
                    self.assertNotIn(value, json.dumps(result))
            self.reject(self.service.read("emotion://" + secret + "@1"), "credential_or_secret_detected")
        self.assertEqual(0, self.count())

    def test_wake_capability_detected_and_never_echoed(self):
        self.fixture.bootstrap_live()
        wake, _ = self.fixture.wake("synthetic-relations-secret")
        secret = wake["wake_capability"]
        with self.ordinary():
            result = self.attach(type="custom", label=secret)
        self.reject(result, "credential_or_secret_detected")
        self.assertNotIn(secret, json.dumps(result)); self.assertEqual(0, self.count())

    def test_circular_nonfinite_and_nested_protected_result_fail_closed(self):
        self.fixture.bootstrap_live()
        secret = "opaque-synthetic-secret-at-least-32chars"
        self.service = MemoryRelationAccessService(self.store, onboarding=self.onboarding, **OWNER, protected_values=(secret,))
        cycle = []; cycle.append(cycle)
        for value in (cycle, float("nan"), {secret: "never return"}, {"items": [{"title": secret}]}):
            with self.assertRaisesRegex(MemoryRelationError, "credential_or_secret_detected"):
                self.service._protected(value)

    def test_private_secret_in_existing_title_or_label_is_blocked_on_read(self):
        self.fixture.bootstrap_live()
        secret = "synthetic-private-secret-existing-12345"
        self.service = MemoryRelationAccessService(self.store, onboarding=self.onboarding, **OWNER, protected_values=(secret,))
        with self.ordinary(): first = self.attach(type="custom", label="safe")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE memory_relation_items SET label=?", (secret,)); db.commit()
        with self.ordinary(): self.reject(self.service.read("emotion://e1@1"), "credential_or_secret_detected")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE memory_relation_items SET label='safe'")
            db.execute("UPDATE learning_items SET current_json=json_set(current_json,'$.title',?)", (secret,)); db.commit()
        with self.ordinary(): self.reject(self.service.read("emotion://e1@1"), "credential_or_secret_detected")
        self.assertEqual(1, self.count())

    def test_activation_rechecked_inside_actual_write_transaction(self):
        self.fixture.bootstrap_live()
        actual = self.store.attach
        def race(**kwargs):
            with closing(sqlite3.connect(self.database)) as db:
                db.execute("UPDATE brain_module_unlocks SET unlocked=0"); db.commit()
            return actual(**kwargs)
        with self.ordinary(), patch.object(self.store, "attach", side_effect=race):
            self.reject(self.attach(), "module_one_required")
        self.assertEqual(0, self.count())

    def open_direct(self, scopes):
        grant = self.onboarding.issue_direct_grant(**OWNER, actor_id="synthetic-human", client_principal="synthetic-client",
            request_id="synthetic-relation-direct", requested_scopes=scopes)
        return self.onboarding.open_brain_context(**OWNER, direct_grant_ref=grant["grant_ref"], direct_client_principal="synthetic-client")

    def test_legacy_cross_module_write_requires_every_endpoint_scope_and_relation_scope(self):
        self.fixture.bootstrap_live()
        opened = self.open_direct(["memory_relations", "emotional_memory"])
        self.assertTrue(opened["write_context_available"])
        self.reject(self.attach(write_context_ref=opened["write_context_ref"]), "relation_scope_not_authorized")
        self.assertEqual(0, self.count())

    def test_legacy_old_grant_does_not_automatically_gain_relation_permission(self):
        self.fixture.bootstrap_live()
        opened = self.open_direct(["emotional_memory", "learning_memory"])
        self.reject(self.attach(write_context_ref=opened["write_context_ref"]), "relation_scope_not_authorized")
        self.assertEqual(0, self.count())

    def test_legacy_exact_new_scopes_allow_explicit_write_without_key_replacement(self):
        self.fixture.bootstrap_live()
        opened = self.open_direct(["memory_relations", "emotional_memory", "learning_memory"])
        first = self.attach(write_context_ref=opened["write_context_ref"])
        self.assertEqual("attached", first["decision"])
        self.assertEqual("detached", self.service.detach(first["edge_ref"], write_context_ref=opened["write_context_ref"])["decision"])

    def test_legacy_revoke_or_wake_close_between_binding_and_attach_is_rejected(self):
        self.fixture.bootstrap_live()
        opened = self.open_direct(["memory_relations", "emotional_memory", "learning_memory"])
        original = self.store.attach
        for sql in ("UPDATE brain_direct_grants SET status='revoked'",
                    "UPDATE brain_wake_sessions SET status='closed' WHERE status='current'",
                    "UPDATE brain_direct_grants SET expires_at='2000-01-01T00:00:00Z'"):
            with closing(sqlite3.connect(self.database)) as db:
                original_grants = db.execute("SELECT grant_id,status,expires_at FROM brain_direct_grants").fetchall()
                original_wakes = db.execute("SELECT wake_id,status FROM brain_wake_sessions").fetchall()
            def race(**kwargs):
                with closing(sqlite3.connect(self.database)) as db:
                    db.execute(sql); db.commit()
                return original(**kwargs)
            with patch.object(self.store, "attach", side_effect=race):
                self.reject(self.attach(write_context_ref=opened["write_context_ref"]), "relation_scope_not_authorized")
            self.assertEqual(0, self.count())
            with closing(sqlite3.connect(self.database)) as db:
                for identity, status, expires in original_grants:
                    db.execute("UPDATE brain_direct_grants SET status=?,expires_at=? WHERE grant_id=?", (status, expires, identity))
                for identity, status in original_wakes:
                    db.execute("UPDATE brain_wake_sessions SET status=? WHERE wake_id=?", (status, identity))
                db.commit()

    def test_legacy_revocation_between_binding_and_detach_keeps_relation_active(self):
        self.fixture.bootstrap_live()
        opened = self.open_direct(["memory_relations", "emotional_memory", "learning_memory"])
        first = self.attach(write_context_ref=opened["write_context_ref"])
        original = self.store.detach
        def race(**kwargs):
            with closing(sqlite3.connect(self.database)) as db:
                db.execute("UPDATE brain_direct_grants SET status='revoked'"); db.commit()
            return original(**kwargs)
        with patch.object(self.store, "detach", side_effect=race):
            self.reject(self.service.detach(first["edge_ref"], write_context_ref=opened["write_context_ref"]), "relation_scope_not_authorized")
        self.assertEqual(1, self.count())
        with self.ordinary(): self.assertEqual(1, len(self.service.read("emotion://e1@1")["relations"]))

    def prepare_claim(self, tool, arguments):
        self.executions = ExecutionStore(self.database, deployment_epoch="synthetic-relation-epoch",
            capability_secret=b"synthetic-execution-secret-over-32-bytes")
        schema_hash = canonical_hash({"type": "object", "synthetic": True})
        entries = [{"canonical_name": tool, "schema_hash": schema_hash}]
        catalog = {"contract": "advertised-tools/1", "catalog_complete": True, "catalog_hash": canonical_hash(entries), "entries": entries}
        wake = self.onboarding.issue_wake(**OWNER, host_id="synthetic-host", thread_id="synthetic-thread",
            source_kind="human_message", source_event_id="synthetic-relations")
        prepared = self.onboarding.build_pre_generation_context(**OWNER, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
            source_digest="synthetic-source", host_contract_digest="synthetic-contract", advertised_tools=catalog)
        self.onboarding.confirm_context_injected(**OWNER, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"], context_hash=prepared["context_hash"])
        issued = self.executions.issue_batch(**OWNER, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
            batch_id="synthetic-relation-batch", revision=1, calls=[{"call_id": "synthetic-relation-call", "canonical_tool": tool,
                "advertised_name": tool, "schema_hash": schema_hash, "catalog_hash": catalog["catalog_hash"], "arguments_hash": canonical_hash(arguments)}])
        return self.executions.claim(**OWNER, execution_ref=issued["executions"][0]["execution_ref"], tool_name=tool, arguments=arguments)

    def test_actual_gateway_claim_pins_identity_and_is_idempotent_until_closed(self):
        self.fixture.bootstrap_live()
        args = {"from_ref": "emotion://e1@1", "to_ref": "plan://p1@1", "type": "causes"}
        claim = self.prepare_claim("attach_memory_relation", args)
        with self.executions.bind(claim), self.ordinary(claim=claim):
            first = self.attach(**args); replay = self.attach(**args)
        self.assertEqual("attached", first["decision"]); self.assertEqual(first["edge_ref"], replay["edge_ref"])
        self.assertFalse(replay["state_changed"])
        self.executions.finish(claim)
        with self.executions.bind(claim), self.ordinary(claim=claim):
            self.reject(self.attach(**args), "execution_claim_not_current")

    def test_wrong_claim_owner_model_tool_database_and_supplied_request_fail(self):
        self.fixture.bootstrap_live()
        args = {"from_ref": "emotion://e1@1", "to_ref": "learning://l1@1", "type": "related_to"}
        claim = self.prepare_claim("attach_memory_relation", args)
        for changed in (replace(claim, owner_id="foreign"), replace(claim, model_id="foreign"),
                        replace(claim, tool_name="read_memory_relations")):
            with self.executions.bind(changed): result = self.attach()
            self.assertIn(result["reason_code"], {"execution_owner_mismatch", "execution_claim_not_current"})
            self.assertEqual("reject", result["decision"])
            self.assertFalse(result["state_changed"])
        with self.assertRaisesRegex(ExecutionBindingError, "execution_claim_invalid"):
            with self.executions.bind(replace(claim, database=str(self.database.with_name("foreign.db")))):
                self.fail("foreign database cannot acquire bound context")
        with self.executions.bind(claim), self.ordinary(claim=claim):
            self.reject(self.attach(request_id="a" * 32), "request_id_conflicts_with_bound_execution")
        self.assertEqual(0, self.count())


if __name__ == "__main__": unittest.main()
