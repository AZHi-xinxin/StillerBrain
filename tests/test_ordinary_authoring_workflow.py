"""Ordinary cross-request authoring: synthetic fixtures and native MCP dispatch.

No real memory, upstream, server listener, or private configuration is used.
Legacy previews/receipts must stay wake-bound after additive migration.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from runtime.authoring import (
    AUTHORING_SCHEMA_VERSIONS, AuthoringError, AuthoringRewriteStore,
    claim_rewrite_receipt,
)
from runtime.ordinary_access import authenticated_ordinary_operation, current_ordinary_access
from tests import test_authoring as authoring_fixtures


class OrdinaryAuthoringWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = authoring_fixtures.AuthoringRewriteReceiptTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.enterContext(patch("socket.create_connection", side_effect=AssertionError("offline synthetic test")))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("offline synthetic test")))
        self.owner, self.model = self.fixture.owner, self.fixture.model
        self.store, self.database = self.fixture.store, self.fixture.database

    def operation(self, scope="shared_person_authoring", **identity):
        return authenticated_ordinary_operation(owner_id=identity.get("owner_id", self.owner),
                     model_id=identity.get("model_id", self.model), scope=scope)

    def preview(self, **overrides):
        with self.operation() as access:
            return self.fixture._preview(wake_id=access["wake_id"], **overrides)

    def confirm(self, preview, **overrides):
        with self.operation() as access:
            return self.fixture._confirm(preview, **{"wake_id": access["wake_id"],
                       "expected_module": self.fixture.module, **overrides})

    def remember(self, preview, receipt=None, **overrides):
        with self.operation("emotional_memory") as access:
            final = preview["suggested_fields"]
            args = dict(owner_id=self.owner, model_id=self.model, wake_id=access["wake_id"],
                expected_row_version=0, memory_type="shared_event", original_text=final["/original_text"],
                summary=final["/summary"], primary_emotion="calm", reason="Synthetic explicit adoption.",
                rewrite_receipt=receipt)
            return self.fixture.emotional.remember(**{**args, **overrides})

    def rows(self, table):
        assert table in {"authoring_rewrite_previews", "authoring_rewrite_receipts"}
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)]

    def test_three_operations_produce_one_canonical_memory_and_receipt_replay_is_idempotent(self):
        preview = self.preview()
        confirmed = self.confirm(preview)
        receipt = confirmed["rewrite_receipt"]
        stored = self.remember(preview, receipt)
        replay = self.remember(preview, receipt)
        self.assertEqual(stored["memory"]["memory_id"], replay["memory"]["memory_id"])
        self.assertEqual(1, self.fixture.emotional.status(owner_id=self.owner, model_id=self.model)["counts"]["active_memories"])
        p, r = self.rows("authoring_rewrite_previews")[0], self.rows("authoring_rewrite_receipts")[0]
        self.assertEqual("ordinary_authenticated", p["context_mode"])
        self.assertEqual("ordinary_authenticated", r["context_mode"])
        self.assertNotEqual(p["wake_id"], r["wake_id"])
        self.assertEqual("consumed", r["status"])
        self.assertIsNone(current_ordinary_access(owner_id=self.owner, model_id=self.model))
        with self.assertRaisesRegex(Exception, "rewrite_receipt_replay_conflict"):
            self.remember(preview, receipt, reason="Different canonical write request")

    def test_failed_canonical_transaction_keeps_receipt_ready(self):
        preview = self.preview()
        receipt = self.confirm(preview)["rewrite_receipt"]
        with self.assertRaisesRegex(Exception, "emotion_row_version_conflict"):
            self.remember(preview, receipt, expected_row_version=999)
        self.assertEqual("ready", self.rows("authoring_rewrite_receipts")[0]["status"])
        self.assertEqual(0, self.fixture.emotional.status(owner_id=self.owner, model_id=self.model)["counts"]["active_memories"])
        self.remember(preview, receipt)
        self.assertEqual("consumed", self.rows("authoring_rewrite_receipts")[0]["status"])

    def test_two_cross_operation_consumers_create_at_most_one_memory(self):
        preview = self.preview()
        receipt = self.confirm(preview)["rewrite_receipt"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.remember(preview, receipt), range(2)))
        self.assertEqual(results[0]["memory"]["memory_id"], results[1]["memory"]["memory_id"])
        self.assertEqual(1, self.fixture.emotional.status(owner_id=self.owner, model_id=self.model)["counts"]["active_memories"])

    def test_exposed_ordinary_suggestion_cannot_omit_receipt_in_later_operation(self):
        preview = self.preview()
        with self.assertRaisesRegex(Exception, "rewrite_receipt_required_for_exposed_suggestion"):
            self.remember(preview)
        self.confirm(preview)
        with self.assertRaisesRegex(Exception, "rewrite_receipt_required_for_exposed_suggestion"):
            self.remember(preview)
        self.assertEqual(0, self.fixture.emotional.status(owner_id=self.owner, model_id=self.model)["counts"]["active_memories"])

    def test_confirmation_keeps_hash_owner_module_explicit_choice_and_cas(self):
        preview = self.preview()
        changed = {**preview["suggested_fields"], "/summary": "Changed independently."}
        cases = (
            ({"owner_id": "another-owner"}, "rewrite_preview_not_found"),
            ({"model_id": "another-model"}, "rewrite_preview_not_found"),
            ({"expected_module": "learning_memory_module_three"}, "rewrite_module_mismatch"),
            ({"expected_source_draft_hash": "0" * 64}, "rewrite_source_stale"),
            ({"expected_suggestion_hash": "0" * 64}, "rewrite_suggestion_stale"),
            ({"expected_validation_context_hash": "0" * 64}, "validation_context_stale"),
            ({"alias_collision_scope_version": 2}, "validation_context_stale"),
            ({"final_fields_hash": "0" * 64}, "final_fields_hash_mismatch"),
            ({"final_fields": changed, "final_fields_hash": self.fixture._hash(changed)}, "final_fields_must_match_preview"),
            ({"ai_confirmation": False}, "ai_confirmation_required"),
            ({"expected_row_version": 999}, "authoring_version_conflict"),
        )
        for overrides, code in cases:
            before = self.fixture._database_snapshot()
            with self.subTest(code=code), self.assertRaisesRegex(AuthoringError, code):
                self.confirm(preview, **overrides)
            self.assertEqual(before, self.fixture._database_snapshot())
        self.assertEqual("confirmed", self.confirm(preview)["decision"])

    def test_ordinary_preview_needs_authenticated_shared_authoring_scope_to_confirm_later(self):
        preview = self.preview()
        for scope in (None, "learning_memory", "emotional_memory"):
            with ExitStack() as stack:
                if scope:
                    stack.enter_context(self.operation(scope))
                with self.assertRaisesRegex(AuthoringError, "rewrite_preview_wrong_wake"):
                    self.fixture._confirm(preview, wake_id="different-operation")

    def test_ordinary_receipt_cross_operation_requires_correct_module_scope_identity_and_exact_final(self):
        preview = self.preview()
        receipt = self.confirm(preview)["rewrite_receipt"]
        args = dict(receipt_token=receipt, owner_id=self.owner, model_id=self.model,
                    wake_id="different-operation", module=self.fixture.module,
                    final_fields=preview["suggested_fields"], request_payload={"synthetic": "request"})
        cases = (
            (None, {}, "rewrite_receipt_binding_mismatch"),
            ("shared_person_authoring", {}, "rewrite_receipt_binding_mismatch"),
            ("learning_memory", {}, "rewrite_receipt_binding_mismatch"),
            ("emotional_memory", {"owner_id": "other"}, "rewrite_receipt_binding_mismatch"),
            ("emotional_memory", {"model_id": "other"}, "rewrite_receipt_binding_mismatch"),
            ("emotional_memory", {"module": "learning_memory_module_three"}, "rewrite_receipt_binding_mismatch"),
            ("emotional_memory", {"final_fields": {"/summary": "different"}}, "rewrite_receipt_final_mismatch"),
        )
        for scope, overrides, code in cases:
            with self.subTest(scope=scope, code=code), ExitStack() as stack:
                if scope:
                    stack.enter_context(self.operation(scope))
                connection = stack.enter_context(closing(sqlite3.connect(self.database)))
                connection.row_factory = sqlite3.Row
                with self.assertRaisesRegex(AuthoringError, code):
                    claim_rewrite_receipt(connection, **{**args, **overrides})
        self.remember(preview, receipt)

    def test_three_target_modules_allow_only_their_ordinary_claim_scope(self):
        modules = (
            ("emotional_memory_module_two", "emotional_memory", "/original_text", "/summary"),
            ("learning_memory_module_three", "learning_memory", "/title", "/summary"),
            ("tool_guidance_module", "tool_guidance", "/display_label", "/purpose"),
        )
        for module, scope, first, second in modules:
            self.fixture.module = module
            with patch.object(self.store, "preview", side_effect=lambda **kwargs: kwargs):
                args = self.fixture._preview()
            args["draft_fields"] = {first: "她回来了，我很安心。", second: "她回来了。"}
            args["referent_bindings"][0]["field_path"] = first
            args["rewrite_targets"][0]["field_path"] = first
            args["expected_row_version"] = self.store.status(owner_id=self.owner, model_id=self.model)["row_version"]
            with self.operation() as access:
                preview = self.store.preview(**{**args, "wake_id": access["wake_id"]})
            confirmed = self.confirm(preview)
            with self.operation(scope) as access, closing(sqlite3.connect(self.database)) as connection:
                connection.row_factory = sqlite3.Row
                claim = claim_rewrite_receipt(connection, receipt_token=confirmed["rewrite_receipt"],
                       owner_id=self.owner, model_id=self.model, wake_id=access["wake_id"], module=module,
                       final_fields=preview["suggested_fields"], request_payload={"synthetic": module})
            self.assertFalse(claim["replayed"])

    def test_additive_migration_preserves_legacy_wake_bound_previews_and_receipts(self):
        preview = self.fixture._preview()
        receipt = self.fixture._confirm(preview)["rewrite_receipt"]
        old_rows = {table: self.rows(table) for table in
                    ("authoring_rewrite_previews", "authoring_rewrite_receipts")}
        with closing(sqlite3.connect(self.database)) as connection:
            for table in ("authoring_rewrite_previews", "authoring_rewrite_receipts"):
                connection.execute("ALTER TABLE " + table + " DROP COLUMN context_mode")
            connection.commit()
        self.store = self.fixture.store = AuthoringRewriteStore(self.database,
                            receipt_secret="authoring-test-secret-that-is-over-32-bytes")
        for table in ("authoring_rewrite_previews", "authoring_rewrite_receipts"):
            self.assertEqual("wake_bound", self.rows(table)[0]["context_mode"])
            self.assertEqual(old_rows[table], self.rows(table))
        with self.assertRaisesRegex(AuthoringError, "rewrite_preview_wrong_wake"):
            self.confirm(preview)
        with self.assertRaisesRegex(Exception, "rewrite_receipt_binding_mismatch"):
            self.remember(preview, receipt)
        # The original matching legacy wake still works after migration.
        self.remember(preview, receipt, wake_id=self.fixture.wake)


async def native_probe():
    root = Path(os.environ["STBRAIN_DB_PATH"]).parent.resolve()
    allowed = {root / name for name in ("main.db", "ideas.db", "vault.db")}
    real_connect = sqlite3.connect
    def checked_connect(path, *args, **kwargs):
        assert Path(path).resolve() in allowed, "only synthetic three databases are permitted"
        return real_connect(path, *args, **kwargs)
    with ExitStack() as stack:
        stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("offline synthetic probe")))
        stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("offline synthetic probe")))
        stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("no nested process")))
        stack.enter_context(patch("sqlite3.connect", side_effect=checked_connect))
        from mcp_server import server
        from tests.test_onboarding import ModuleOneOnboardingTests
        fixture = authoring_fixtures.AuthoringRewriteReceiptTests()
        fixture.module, fixture.owner, fixture.model, fixture.wake = "emotional_memory_module_two", server.OWNER_ID, server.MODEL_ID, "synthetic-unused"
        fixture.store = server.authoring_store
        async def call(name, arguments):
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert json.loads(blocks[0].text) == result
            assert current_ordinary_access(owner_id=fixture.owner, model_id=fixture.model) is None
            return result
        with patch.object(fixture.store, "preview", side_effect=lambda **kwargs: kwargs):
            args = fixture._preview()
        for key in ("owner_id", "model_id", "wake_id", "expected_row_version"):
            args.pop(key)
        denied = await call("preview_person_reference_rewrite", args)
        assert denied.get("reason_code") == "module_one_required" and denied.get("state_changed") is False
        with closing(sqlite3.connect(root / "main.db")) as connection:
            assert connection.execute("SELECT COUNT(*) FROM authoring_rewrite_previews").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM brain_wake_sessions").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM self_model_revisions").fetchone()[0] == 0
        # Complete the real synthetic module-one flow before public authoring
        # writes; the ordinary three-request chain itself needs no new wake.
        onboarding_fixture = ModuleOneOnboardingTests()
        onboarding_fixture.database, onboarding_fixture.store = root / "main.db", server.onboarding
        onboarding_fixture.owner, onboarding_fixture.model = server.OWNER_ID, server.MODEL_ID
        onboarding_fixture.bootstrap_live()
        assert server.onboarding.state(owner_id=server.OWNER_ID, model_id=server.MODEL_ID)["module_one_unlocked"] is True
        with closing(sqlite3.connect(root / "main.db")) as connection:
            bootstrap_wakes = connection.execute("SELECT COUNT(*) FROM brain_wake_sessions").fetchone()[0]
            original_core = connection.execute("SELECT * FROM self_model_revisions ORDER BY revision_id").fetchall()
        assert bootstrap_wakes == 3
        preview = await call("preview_person_reference_rewrite", args)
        assert preview["decision"] == "preview_only", preview
        with patch.object(fixture.store, "confirm", side_effect=lambda **kwargs: kwargs):
            args = fixture._confirm(preview)
        for key in ("owner_id", "model_id", "wake_id", "expected_row_version"):
            args.pop(key)
        args["module"] = fixture.module
        wrong = await call("confirm_person_reference_rewrite", {**args, "module": "learning_memory_module_three"})
        assert wrong["decision"] == "reject" and wrong["reason_codes"] == ["rewrite_module_mismatch"], wrong
        confirmation = await call("confirm_person_reference_rewrite", args)
        assert confirmation["decision"] == "confirmed", confirmation
        final = preview["suggested_fields"]
        memory_args = {"memory_type": "shared_event", "original_text": final["/original_text"],
                       "summary": final["/summary"], "primary_emotion": "calm", "reason": "Synthetic exact adoption"}
        missing = await call("remember_emotional_memory", memory_args)
        assert missing["decision"] == "reject" and missing["state_changed"] is False, missing
        stored = await call("remember_emotional_memory", {**memory_args, "rewrite_receipt": confirmation["rewrite_receipt"]})
        assert stored["decision"] == "stored", stored
        with closing(sqlite3.connect(root / "main.db")) as connection:
            assert connection.execute("SELECT COUNT(*) FROM brain_wake_sessions").fetchone()[0] == bootstrap_wakes
            assert connection.execute("SELECT * FROM self_model_revisions ORDER BY revision_id").fetchall() == original_core
            assert connection.execute("SELECT COUNT(*) FROM emotion_memories").fetchone()[0] == 1
            assert connection.execute("SELECT context_mode,status FROM authoring_rewrite_receipts").fetchone() == ("ordinary_authenticated", "consumed")
        return {"decision": "PASS", "native_three_request_chain": True,
                "module_one_bootstrapped": True, "unactivated_authoring_rejected": True,
                "ordinary_operations_created_wakes": 0, "core_revision_unchanged": True,
                "missing_receipt_rejected": True, "module_mismatch_rejected": True, "real_model_calls": 0}


class NativeOrdinaryAuthoringTests(unittest.TestCase):
    def test_registered_authoring_after_activation_without_another_wake(self):
        allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATH", "PATHEXT", "TEMP", "TMP", "OS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="ordinary-authoring-synthetic-") as scratch:
            root = Path(scratch)
            env.update(STBRAIN_MCP_TOKEN="synthetic-token-00000000000000000000000",
                       STBRAIN_WAKE_SECRET="synthetic-wake-0000000000000000000000", STBRAIN_OWNER_ID="synthetic-owner",
                       STBRAIN_MODEL_ID="synthetic-model", STBRAIN_DB_PATH=str(root / "main.db"),
                       STBRAIN_LEARNING_IDEA_DB_PATH=str(root / "ideas.db"), STBRAIN_HALLUCINATION_VAULT_DB_PATH=str(root / "vault.db"),
                       STBRAIN_REQUIRE_EXECUTION_BINDING="1", STBRAIN_EXECUTION_EPOCH="synthetic-authoring-epoch",
                       STBRAIN_ACCESS_PROFILE="simple-memory-v1", PYTHONIOENCODING="utf-8",
                       PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, "-B", "-m", "tests.test_ordinary_authoring_workflow", "--probe"],
                       cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertTrue(proof["module_one_bootstrapped"])
        self.assertTrue(proof["unactivated_authoring_rejected"])
        self.assertTrue(proof["core_revision_unchanged"])
        self.assertEqual(0, proof["ordinary_operations_created_wakes"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(native_probe())))
    else:
        unittest.main()
