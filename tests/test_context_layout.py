"""Synthetic, offline tests of authenticated snapshot layout v1.

No production database, user memories, provider requests or tokens are used.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
import json
import sqlite3
import unittest
from unittest import mock

from mcp_server.control_server import ControlApplication
from runtime.onboarding import (
    CONTEXT_LAYOUT_CONTRACT, ModuleOneOnboardingStore, OnboardingError,
    OPTIONAL_BRAIN_NOTICE, _canonical, _sha256,
)
from tests import test_onboarding as fixtures


class ContextLayoutTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ModuleOneOnboardingTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.database = self.fixture.database
        self.scope = {"owner_id": self.fixture.owner, "model_id": self.fixture.model}
        self.messages = [
            {"role": "system", "content": "Synthetic frontend instructions."},
            {"role": "user", "content": "Previous topic."},
            {"role": "assistant", "content": "Previous answer."},
            {"role": "user", "content": "Current synthetic question."},
        ]
        self.layout = {
            "contract": CONTEXT_LAYOUT_CONTRACT,
            "insertion_rule": "before-current-human",
            "human_message_index": 3,
            "initial_message_count": 4,
            "initial_messages_digest": _sha256(self.messages),
        }

    def issue(self, event="layout-test", thread="thread:layout"):
        return self.store.issue_wake(
            **self.scope, host_id="host:layout", thread_id=thread,
            source_kind="human_message", source_event_id=event,
        )

    def prepare(self, wake, **changes):
        arguments = {
            **self.scope,
            "wake_id": wake["wake_id"],
            "wake_capability": wake["wake_capability"],
            "source_digest": "synthetic-source-digest",
            "host_contract_digest": "synthetic-host-contract-digest",
            "context_layout": deepcopy(self.layout),
        }
        arguments.update(changes)
        return self.store.build_pre_generation_context(**arguments)

    def confirm(self, wake, context_hash):
        return self.store.confirm_context_injected(
            **self.scope, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], context_hash=context_hash,
        )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def metadata(self, wake):
        with self.connect() as connection:
            return json.loads(connection.execute(
                "SELECT context_layout_json FROM brain_context_snapshots WHERE wake_id=?",
                (wake["wake_id"],),
            ).fetchone()[0])

    def test_legacy_single_message_hash_and_metadata_unchanged(self):
        wake = self.issue()
        prepared = self.prepare(wake, context_layout=None)
        self.assertNotIn("context_bundle", prepared)
        self.assertEqual(_sha256(prepared["message"]), prepared["context_hash"])
        self.assertEqual({}, self.metadata(wake))
        self.assertEqual("context_reused", self.prepare(wake, context_layout=None)["decision"])
        self.assertEqual("injected", self.confirm(wake, prepared["context_hash"])["decision"])

    def test_factory_bundle_binds_exact_minimal_frame_and_full_host_scope(self):
        wake = self.issue()
        prepared = self.prepare(wake)
        bundle = prepared["context_bundle"]
        self.assertEqual({"role": "system", "content": OPTIONAL_BRAIN_NOTICE}, bundle["stable_message"])
        self.assertIsNone(bundle["dynamic_message"])
        self.assertEqual(self.layout, bundle["layout"])
        self.assertEqual({
            **self.scope, "host_id": "host:layout", "thread_id": "thread:layout",
            "wake_id": wake["wake_id"], "source_digest": "synthetic-source-digest",
            "host_contract_digest": "synthetic-host-contract-digest",
        }, bundle["binding"])
        self.assertEqual(_sha256(prepared["message"]), bundle["legacy_message_hash"])
        self.assertEqual(_sha256(bundle), prepared["context_hash"])
        self.assertNotEqual(bundle["legacy_message_hash"], prepared["context_hash"])
        self.assertEqual("context_not_injected", self.confirm(wake, bundle["legacy_message_hash"])["decision"])
        self.assertEqual("injected", self.confirm(wake, prepared["context_hash"])["decision"])
        self.assertNotIn("wake_id", bundle["stable_message"]["content"])

    def test_snapshot_restarts_and_same_wake_freeze(self):
        self.fixture.seed_tagless_named_learning()
        wake = self.issue()
        first = self.prepare(wake, source_frame={"query_text": "你还记得我们之前读的侍魔嘛？"})
        bundle = first["context_bundle"]
        self.assertIsNotNone(bundle["dynamic_message"])
        stable = json.loads(bundle["stable_message"]["content"])
        dynamic = json.loads(bundle["dynamic_message"]["content"])
        self.assertEqual({"boot_anchor", "active_identity_capsule", "facets"}, set(stable))
        self.assertIn("learning_memory", dynamic)
        self.assertEqual(json.loads(first["message"]["content"]), {**stable, **dynamic})
        self.assertNotIn("完整理解测试标记", bundle["dynamic_message"]["content"])
        self.confirm(wake, first["context_hash"])
        self.store = ModuleOneOnboardingStore(
            self.database, capability_secret=self.fixture.store.capability_secret,
            wake_ttl_seconds=300, edit_challenge_ttl_seconds=300,
        )
        reused = self.prepare(wake, source_frame={"query_text": "Different topic must not refresh this wake."})
        self.assertEqual("context_reused", reused["decision"])
        self.assertEqual(bundle, reused["context_bundle"])
        self.assertEqual(first["context_hash"], reused["context_hash"])
        self.assertEqual({"layout", "hard_suppressed"}, set(self.metadata(wake)))

    def test_layout_mutation_or_legacy_switch_cannot_reuse_snapshot(self):
        wake = self.issue()
        self.prepare(wake)
        for key, value in (
            ("human_message_index", 1), ("initial_message_count", 5),
            ("initial_messages_digest", "f" * 64),
        ):
            modified = {**self.layout, key: value}
            denied = self.prepare(wake, context_layout=modified)
            self.assertEqual(["context_snapshot_mismatch"], denied["reason_codes"])
            self.assertFalse(denied["may_generate"])
        self.assertFalse(self.prepare(wake, context_layout=None)["may_generate"])

    def test_existing_legacy_wake_never_upgrades_in_place(self):
        wake = self.issue()
        legacy = self.prepare(wake, context_layout=None)
        self.assertFalse(self.prepare(wake)["may_generate"])
        reused = self.prepare(wake, context_layout=None)
        self.assertEqual(legacy["context_hash"], reused["context_hash"])

    def test_invalid_descriptor_rejected_before_snapshot_insert(self):
        wake = self.issue()
        variants = [
            {}, [], {**self.layout, "unknown": "x"},
            {**self.layout, "contract": "stbrain-context-layout/unknown"},
            {**self.layout, "insertion_rule": "after-current-human"},
            {**self.layout, "human_message_index": True},
            {**self.layout, "human_message_index": -1},
            {**self.layout, "human_message_index": 4},
            {**self.layout, "initial_message_count": 100001},
            {**self.layout, "initial_messages_digest": "unverified"},
        ]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(OnboardingError):
                self.prepare(wake, context_layout=variant)
        self.assertEqual(0, self.fixture.count("brain_context_snapshots"))

    def test_unknown_or_malformed_persisted_layout_fails_closed(self):
        wake = self.issue()
        self.prepare(wake)
        variants = ["not-json", "[]", "null", '{"layout":{}}', _canonical({
            "layout": {**self.layout, "contract": "stbrain-context-layout/unknown"},
            "hard_suppressed": False,
        })]
        for variant in variants:
            with self.connect() as connection:
                connection.execute("UPDATE brain_context_snapshots SET context_layout_json=?", (variant,))
            denied = self.prepare(wake)
            self.assertEqual(["context_layout_metadata_invalid"], denied["reason_codes"])
            self.assertFalse(denied["may_generate"])

    def test_snapshot_material_tampering_is_not_rehashed_into_new_authority(self):
        wake = self.issue()
        self.prepare(wake)
        with self.connect() as connection:
            connection.execute("UPDATE brain_context_snapshots SET dynamic_json=?", ('{"forged":"synthetic"}',))
        denied = self.prepare(wake)
        self.assertEqual(["context_snapshot_hash_mismatch"], denied["reason_codes"])
        self.assertFalse(denied["may_generate"])

    def test_cross_owner_capability_and_superseded_wake_still_denied(self):
        wake = self.issue()
        prepared = self.prepare(wake)
        with self.assertRaises(OnboardingError):
            self.prepare(wake, owner_id="other:owner")
        self.assertEqual("wake_invalid", self.prepare(wake, wake_capability="invalid-capability")["decision"])
        later = self.issue(event="later", thread="thread:other")
        other = self.prepare(later)
        self.assertNotEqual(prepared["context_hash"], other["context_hash"])
        self.assertEqual("wake_superseded", self.prepare(wake)["decision"])

    def test_hard_and_soft_off_keep_minimal_frame_and_restore_exactly(self):
        self.fixture.bootstrap_live()
        for mode in ("hard_off", "soft_off"):
            with self.subTest(mode=mode):
                wake = self.issue(event=mode)
                with mock.patch.object(self.store.injection_control_store, "effective_mode", return_value=mode):
                    first = self.prepare(wake)
                self.assertEqual({"role": "system", "content": "{}"}, first["message"])
                self.assertEqual(first["message"], first["context_bundle"]["stable_message"])
                self.assertIsNone(first["context_bundle"]["dynamic_message"])
                reused = self.prepare(wake)
                self.assertEqual("context_reused", reused["decision"])
                self.assertEqual(first["context_hash"], reused["context_hash"])

    def test_disabled_bypass_does_not_create_bundle_or_snapshot(self):
        self.store.ensure_state(**self.scope)
        with self.connect() as connection:
            connection.execute("UPDATE brain_onboarding_state SET injection_policy='disabled'")
        wake = self.issue()
        result = self.prepare(wake)
        self.assertEqual("bypass", result["decision"])
        self.assertNotIn("context_bundle", result)
        self.assertEqual(0, self.fixture.count("brain_context_snapshots"))

    def test_legacy_suppressed_snapshot_restores_only_its_committed_empty_frame(self):
        self.fixture.bootstrap_live()
        wake = self.issue()
        with mock.patch.object(self.store.injection_control_store, "effective_mode", return_value="hard_off"):
            first = self.prepare(wake, context_layout=None)
        self.assertEqual("{}", first["message"]["content"])
        self.assertEqual({}, self.metadata(wake))
        reused = self.prepare(wake, context_layout=None)
        self.assertEqual("context_reused", reused["decision"])
        self.assertEqual(first["message"], reused["message"])
        self.assertEqual(first["context_hash"], reused["context_hash"])

    def test_isolated_legacy_still_excludes_legacy_identity_from_bundle(self):
        self.fixture.test_legacy_revision_is_preserved_and_not_injected()
        wake = self.issue()
        first = self.prepare(wake)
        self.assertEqual(OPTIONAL_BRAIN_NOTICE, first["context_bundle"]["stable_message"]["content"])
        self.assertIsNone(first["context_bundle"]["dynamic_message"])
        self.assertNotIn("active_identity_capsule", first["message"]["content"])
        self.assertEqual("context_reused", self.prepare(wake)["decision"])

    def test_edit_recovery_still_uses_active_identity_not_candidate(self):
        self.fixture.test_edit_draft_only_recovery_keeps_active_first_person_context()
        wake = self.issue()
        first = self.prepare(wake)
        content = json.loads(first["context_bundle"]["stable_message"]["content"])
        self.assertEqual(fixtures.model_content("v1")["active_identity_capsule"], content["active_identity_capsule"])
        self.assertNotIn("v2-draft", first["message"]["content"])
        self.assertEqual(first["context_hash"], self.prepare(wake)["context_hash"])

    def test_new_bundle_does_not_shortcut_candidate_review_wake_gate(self):
        self.fixture.bootstrap_to_wait()
        wake = self.issue()
        prepared = self.prepare(wake)
        self.assertEqual(OPTIONAL_BRAIN_NOTICE, prepared["message"]["content"])
        self.assertEqual("candidate_wait", self.store.state(**self.scope)["state"]["stage"])
        self.confirm(wake, prepared["context_hash"])
        self.assertEqual("candidate_wait", self.store.state(**self.scope)["state"]["stage"])
        opened = self.store.open_brain_context(**self.scope)
        self.assertEqual("candidate_review", opened["continuation"]["stage"])

    def test_split_retains_dynamic_only_and_rejects_collisions(self):
        wake = {**self.scope, "host_id": "host:layout", "thread_id": "thread:layout", "wake_id": "wake:synthetic"}
        dynamic = {"learning_memory": {"summary": "Synthetic memory, exact author's text."}}
        message = self.store._context_message(stable={}, dynamic=dynamic)
        arguments = dict(stable={}, dynamic=dynamic, message=message, layout=self.layout,
                         wake=wake, source_digest="source", host_contract_digest="host")
        bundle = self.store._context_bundle(**arguments)
        self.assertIsNone(bundle["stable_message"])
        self.assertEqual(message, bundle["dynamic_message"])
        with self.assertRaises(OnboardingError):
            self.store._context_bundle(**{**arguments, "stable": dynamic})

    def test_additive_migration_only_adds_dedicated_column_preserves_legacy_rows(self):
        wake = self.issue()
        prepared = self.prepare(wake, context_layout=None)
        with self.connect() as connection:
            connection.execute("ALTER TABLE brain_context_snapshots DROP COLUMN context_layout_json")
            columns_before = connection.execute("PRAGMA table_info(brain_context_snapshots)").fetchall()
            rows_before = connection.execute("SELECT * FROM brain_context_snapshots").fetchall()
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            counts_before = {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
        self.store = ModuleOneOnboardingStore(self.database, capability_secret=self.fixture.store.capability_secret)
        with self.connect() as connection:
            columns_after = connection.execute("PRAGMA table_info(brain_context_snapshots)").fetchall()
            self.assertEqual(columns_before, columns_after[:-1])
            self.assertEqual(("context_layout_json", "TEXT", 1, "'{}'", 0), columns_after[-1][1:])
            rows_after = connection.execute("SELECT * FROM brain_context_snapshots").fetchall()
            self.assertEqual(rows_before, [row[:-1] for row in rows_after])
            self.assertTrue(all(row[-1] == "{}" for row in rows_after))
            self.assertEqual(counts_before, {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables})
        self.assertEqual(prepared["context_hash"], self.prepare(wake, context_layout=None)["context_hash"])
        ModuleOneOnboardingStore(self.database, capability_secret=self.fixture.store.capability_secret)
        self.assertEqual({}, self.metadata(wake))

    def test_control_authenticates_descriptor_and_confirmation(self):
        host_token = "synthetic-host-token-" + "h" * 32
        app = ControlApplication(self.store, **self.scope, host_token=host_token,
                                 human_token="synthetic-human-token-" + "u" * 32,
                                 human_actor_id="human:synthetic")
        wake = self.issue()
        body = {
            "wake_id": wake["wake_id"], "wake_capability": wake["wake_capability"],
            "source_digest": "source", "host_contract_digest": "host",
            "context_layout": self.layout,
        }
        encoded = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        self.assertEqual(401, app.handle("POST", "/v1/host/context/prepare", headers, encoded)[0])
        headers["Authorization"] = "Bearer " + host_token
        status, prepared = app.handle("POST", "/v1/host/context/prepare", headers, encoded)
        self.assertEqual(200, status)
        self.assertEqual(self.layout, prepared["context_bundle"]["layout"])
        invalid = {**body, "context_layout": {**self.layout, "contract": "unknown"}}
        self.assertEqual(400, app.handle("POST", "/v1/host/context/prepare", headers, json.dumps(invalid).encode())[0])
        status, confirmed = app.handle("POST", "/v1/host/context/confirm", headers, json.dumps({
            "wake_id": wake["wake_id"], "wake_capability": wake["wake_capability"],
            "context_hash": prepared["context_hash"],
        }).encode())
        self.assertEqual(200, status)
        self.assertEqual("injected", confirmed["decision"])


if __name__ == "__main__":
    unittest.main()
