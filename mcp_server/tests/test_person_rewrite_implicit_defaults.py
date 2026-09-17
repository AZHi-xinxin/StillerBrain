"""Synthetic regressions for omitted learning preceding-context fields.

Every database is a fresh temporary fixture. No installed configuration,
memory, participant registry, model, network, or listening service is used.
Only the learning writer's absent/empty context is equivalent: the author's
other fields, receipt ownership, module, and replay payload remain exact.
"""
from __future__ import annotations

import asyncio
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

from mcp_server.authoring_service import AuthoringRewriteAccessService
from mcp_server.tests import test_person_rewrite_simple_entry as fixtures
from runtime.ordinary_access import authenticated_ordinary_operation


_ABSENT = object()
_OS_ENV = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATH", "PATHEXT", "TEMP", "TMP", "OS"}


def clean_environment():
    return {key: value for key, value in os.environ.items() if key.upper() in _OS_ENV}


def preview_fields(preceding_context=_ABSENT):
    draft = {
        "/title": "Synthetic learning note",
        "/summary": "An isolated authoring regression.",
        "/current_understanding": "I tested the synthetic example with her.",
    }
    if preceding_context is not _ABSENT:
        draft["/preceding_context_summary"] = preceding_context
    return {
        "module": "learning_memory_module_three",
        "draft_fields": draft,
        "rewrite_targets": [{
            "field_path": "/current_understanding", "surface_form": "her",
            "entity_ref": "synthetic-person", "target_surface_form": "SyntheticPerson",
        }],
    }


class ImplicitDefaultReceiptTests(unittest.TestCase):
    operation = fixtures.PersonRewriteSimpleEntryTests.operation
    rows = fixtures.PersonRewriteSimpleEntryTests.rows
    remember = fixtures.PersonRewriteSimpleEntryTests.remember

    def setUp(self):
        self.enterContext(patch.dict(os.environ, clean_environment(), clear=True))
        fixtures.PersonRewriteSimpleEntryTests.setUp(self)

    def preview(self, fields=None, *, owner=None, model=None):
        owner = self.owner if owner is None else owner
        model = self.model if model is None else model
        service = self.authoring if (owner, model) == (self.owner, self.model) else AuthoringRewriteAccessService(
            self.rewrites, onboarding=self.host, owner_id=owner, model_id=model)
        with authenticated_ordinary_operation(owner_id=owner, model_id=model, scope="shared_person_authoring") as access:
            result = service.preview(write_context_ref=access["write_context_ref"], **(fields or preview_fields()))
        self.assertEqual("preview_only", result["decision"], result)
        return result

    def confirm(self, preview, *, owner=None, model=None):
        owner = self.owner if owner is None else owner
        model = self.model if model is None else model
        service = self.authoring if (owner, model) == (self.owner, self.model) else AuthoringRewriteAccessService(
            self.rewrites, onboarding=self.host, owner_id=owner, model_id=model)
        with authenticated_ordinary_operation(owner_id=owner, model_id=model, scope="shared_person_authoring") as access:
            result = service.confirm(write_context_ref=access["write_context_ref"],
                                     preview_id=preview["preview_id"], ai_confirmation=True)
        self.assertEqual("confirmed", result["decision"], result)
        self.assertEqual(preview["suggested_fields"], result["final_fields"])
        return result["rewrite_receipt"]

    def snapshot(self):
        return {table: self.rows(table) for table in (
            "authoring_rewrite_previews", "authoring_rewrite_receipts",
            "emotion_memories", "learning_items", "planning_versions")}

    def assert_learning_saved(self, result, preview):
        self.assertTrue(result["stored"], result)
        row = json.loads(self.rows("learning_items")[0]["current_json"])
        for path, field in (("/title", "title"), ("/summary", "summary"),
                            ("/current_understanding", "current_understanding")):
            self.assertEqual(preview["suggested_fields"][path], row[field])
        self.assertEqual("", row["preceding_context_summary"])
        self.assertEqual("consumed", self.rows("authoring_rewrite_receipts")[0]["status"])

    def test_three_field_preview_minimal_confirm_and_unified_remember(self):
        preview = self.preview()
        self.assertEqual(3, len(preview["suggested_fields"]))
        self.assertNotIn("/preceding_context_summary", preview["suggested_fields"])
        receipt = self.confirm(preview)
        self.assertEqual([], self.rows("learning_items"))
        self.assert_learning_saved(self.remember("learning_memory", preview, receipt), preview)

    def test_explicit_empty_preceding_context_remains_compatible(self):
        preview = self.preview(preview_fields(""))
        receipt = self.confirm(preview)
        self.assertEqual("", preview["suggested_fields"]["/preceding_context_summary"])
        self.assert_learning_saved(self.remember("learning_memory", preview, receipt), preview)

    def test_available_three_field_suggestion_requires_receipt(self):
        preview = self.preview()
        before = self.snapshot()
        denied = self.remember("learning_memory", preview)
        self.assertEqual(["rewrite_receipt_required_for_exposed_suggestion"], denied["reason_codes"])
        self.assertFalse(denied["state_changed"])
        self.assertEqual(before, self.snapshot())

    def test_confirmed_three_field_suggestion_requires_receipt_without_consuming_it(self):
        preview = self.preview()
        self.confirm(preview)
        before = self.snapshot()
        denied = self.remember("learning_memory", preview)
        self.assertEqual(["rewrite_receipt_required_for_exposed_suggestion"], denied["reason_codes"])
        self.assertFalse(denied["state_changed"])
        self.assertEqual(before, self.snapshot())

    def test_changed_body_summary_or_title_reject_without_consuming_receipt(self):
        preview = self.preview()
        receipt = self.confirm(preview)
        before = self.snapshot()
        for field in ("content", "summary", "title"):
            with self.subTest(field=field):
                denied = self.remember("learning_memory", preview, receipt, **{field: "Changed synthetic author text."})
                self.assertEqual(["rewrite_receipt_final_mismatch"], denied["reason_codes"])
                self.assertFalse(denied["state_changed"])
                self.assertEqual(before, self.snapshot())
        self.assert_learning_saved(self.remember("learning_memory", preview, receipt), preview)

    def test_nonempty_preceding_context_cannot_be_discarded_by_unified_writer(self):
        preview = self.preview(preview_fields("The author supplied specific synthetic prior context."))
        receipt = self.confirm(preview)
        before = self.snapshot()
        denied = self.remember("learning_memory", preview, receipt)
        self.assertEqual(["rewrite_receipt_final_mismatch"], denied["reason_codes"])
        self.assertFalse(denied["state_changed"])
        self.assertEqual(before, self.snapshot())

    def test_only_preceding_context_is_implicit_not_other_missing_fields(self):
        for missing in ("/title", "/summary"):
            with self.subTest(field=missing):
                args = preview_fields()
                del args["draft_fields"][missing]
                preview = self.preview(args)
                receipt = self.confirm(preview)
                before = self.snapshot()
                final = preview["suggested_fields"]
                with self.operation("learning_memory"):
                    denied = self.daily.remember("learning_memory", final["/current_understanding"],
                        title=final.get("/title", "Synthetic learning note"),
                        summary=final.get("/summary", "An isolated authoring regression."), rewrite_receipt=receipt)
                self.assertEqual(["rewrite_receipt_final_mismatch"], denied["reason_codes"])
                self.assertFalse(denied["state_changed"])
                self.assertEqual(before, self.snapshot())

    def test_cross_owner_and_cross_model_receipts_stay_bound(self):
        for owner, model in (("synthetic-other-owner", self.model), (self.owner, "synthetic-other-model")):
            with self.subTest(owner=owner, model=model):
                preview = self.preview(owner=owner, model=model)
                receipt = self.confirm(preview, owner=owner, model=model)
                before = self.snapshot()
                denied = self.remember("learning_memory", preview, receipt)
                self.assertEqual(["rewrite_receipt_binding_mismatch"], denied["reason_codes"])
                self.assertFalse(denied["state_changed"])
                self.assertEqual(before, self.snapshot())

    def test_learning_receipt_cannot_be_used_for_emotional_memory(self):
        preview = self.preview()
        receipt = self.confirm(preview)
        before = self.snapshot()
        denied = self.remember("emotional_memory", preview, receipt)
        self.assertEqual(["rewrite_receipt_binding_mismatch"], denied["reason_codes"])
        self.assertFalse(denied["state_changed"])
        self.assertEqual(before, self.snapshot())

    def test_replay_returns_same_memory_and_changed_request_cannot_write_again(self):
        preview = self.preview()
        receipt = self.confirm(preview)
        stored = self.remember("learning_memory", preview, receipt)
        self.assert_learning_saved(stored, preview)
        before = self.snapshot()
        replay = self.remember("learning_memory", preview, receipt)
        self.assertTrue(replay["stored"], replay)
        self.assertEqual(stored["ref"], replay["ref"])
        self.assertEqual(before, self.snapshot())
        denied = self.remember("learning_memory", preview, receipt, reason="Different canonical request.")
        self.assertEqual(["rewrite_receipt_replay_conflict"], denied["reason_codes"])
        self.assertEqual(before, self.snapshot())

    def test_not_using_rewrite_preserves_author_voice_and_does_not_enable_automatic_rewrite(self):
        original = "  She wrote: I spoke with her.\nThis is a synthetic fictional passage.  "
        for module in ("emotional_memory", "learning_memory", "planning_memory"):
            with self.operation(module):
                stored = self.daily.remember(module, original)
            self.assertTrue(stored["stored"], stored)
        self.assertEqual(original, self.rows("emotion_memories")[0]["original_text"])
        self.assertEqual(original, json.loads(self.rows("learning_items")[0]["current_json"])["current_understanding"])
        self.assertEqual(original, json.loads(self.rows("planning_versions")[0]["content_json"])["original_text"])
        self.assertEqual([], self.rows("authoring_rewrite_previews"))
        self.assertEqual([], self.rows("authoring_rewrite_receipts"))


async def native_probe():
    root = Path(os.environ["STBRAIN_DB_PATH"]).parent.resolve()
    permitted = {root / filename for filename in ("main.db", "ideas.db", "vault.db")}
    real_connect = sqlite3.connect

    def connect(path, *args, **kwargs):
        assert Path(path).resolve() in permitted, "only synthetic probe databases"
        return real_connect(path, *args, **kwargs)

    with ExitStack() as stack:
        for target in ("socket.create_connection", "socket.socket.connect", "subprocess.Popen"):
            stack.enter_context(patch(target, side_effect=AssertionError("offline synthetic probe")))
        stack.enter_context(patch("sqlite3.connect", side_effect=connect))
        from mcp_server import server
        from tests import test_onboarding as onboarding_fixtures
        fixture = onboarding_fixtures.ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / "main.db", server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()

        async def call(name, arguments):
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert json.loads(blocks[0].text) == result
            return result

        preview = await call("preview_person_reference_rewrite", preview_fields())
        assert preview["decision"] == "preview_only", preview
        assert len(preview["suggested_fields"]) == 3, preview
        confirmed = await call("confirm_person_reference_rewrite", {
            "preview_id": preview["preview_id"], "ai_confirmation": True})
        assert confirmed["decision"] == "confirmed", confirmed
        args = fixtures._memory_fields("learning_memory", preview, confirmed["rewrite_receipt"])
        stored = await call("remember_memory", args)
        assert stored["stored"] is True, stored
        replay = await call("remember_memory", args)
        assert replay["stored"] is True and replay["ref"] == stored["ref"], replay
        with closing(sqlite3.connect(root / "main.db")) as connection:
            assert connection.execute("SELECT COUNT(*) FROM learning_items").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM emotion_memories").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM authoring_rewrite_receipts WHERE status='consumed'").fetchone()[0] == 1
        return {"decision": "PASS", "minimal_learning_native_chain": True,
                "canonical_memories": 1, "real_model_calls": 0, "external_network_requests": 0}


class NativeImplicitDefaultTests(unittest.TestCase):
    def test_three_field_chain_through_registered_mcp_dispatch(self):
        env = clean_environment()
        with tempfile.TemporaryDirectory(prefix="implicit-default-native-") as temporary:
            root = Path(temporary)
            env.update(STBRAIN_MCP_TOKEN="synthetic-token-00000000000000000000000",
                STBRAIN_WAKE_SECRET="synthetic-wake-0000000000000000000000",
                STBRAIN_OWNER_ID="synthetic-owner", STBRAIN_MODEL_ID="synthetic-model",
                STBRAIN_DB_PATH=str(root / "main.db"), STBRAIN_LEARNING_IDEA_DB_PATH=str(root / "ideas.db"),
                STBRAIN_HALLUCINATION_VAULT_DB_PATH=str(root / "vault.db"),
                STBRAIN_REQUIRE_EXECUTION_BINDING="1", STBRAIN_EXECUTION_EPOCH="synthetic-authoring-epoch",
                STBRAIN_ACCESS_PROFILE="simple-memory-v1", PYTHONIOENCODING="utf-8",
                PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", __name__, "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding="utf-8", timeout=90,
                creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("PASS", json.loads(result.stdout.strip().splitlines()[-1])["decision"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(native_probe())))
    else:
        unittest.main()
