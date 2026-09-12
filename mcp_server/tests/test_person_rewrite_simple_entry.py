"""Synthetic preview/confirm receipts through the ordinary unified writer.

Only isolated temporary SQLite databases are opened. No real model, listener,
configuration, memory, or participant registry is used; aliases in these
fixtures are explicit synthetic author declarations, not host authentication.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
import hashlib
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
from mcp_server.daily_memory_service import DailyMemoryAccessService
from mcp_server.emotional_service import EmotionalMemoryAccessService
from mcp_server.learning_service import LearningMemoryAccessService
from mcp_server.planning_service import PlanningMemoryAccessService
from mcp_server.tests import test_compact_open as compact_fixtures
from runtime.authoring import (
    ALIAS_COMPARISON_PROFILE_VERSION, AUTHORING_SCHEMA_VERSIONS,
    MENTION_PARSER_RULE_VERSION, REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
    AuthoringRewriteStore,
)
from runtime.emotional_memory import EmotionalMemoryStore
from runtime.learning_memory import LearningMemoryStore
from runtime.ordinary_access import authenticated_ordinary_operation
from runtime.planning_memory import PlanningMemoryStore


MODULES = {
    "emotional_memory": "emotional_memory_module_two",
    "learning_memory": "learning_memory_module_three",
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _context(module):
    return {
        "conversation_mode": "one_to_one",
        "authenticated_participant_entity_ids": ["synthetic-person"],
        "alias_collision_scope": "synthetic-conversation",
        "alias_collision_scope_version": 1, "protected_spans": [],
        "module_schema_version": AUTHORING_SCHEMA_VERSIONS[MODULES[module]],
        "rewrite_eligible_allowlist_version": REWRITE_ELIGIBLE_ALLOWLIST_VERSION,
        "mention_parser_rule_version": MENTION_PARSER_RULE_VERSION,
        "alias_comparison_profile_version": ALIAS_COMPARISON_PROFILE_VERSION,
    }


def _preview_fields(module):
    field = "/original_text" if module == "emotional_memory" else "/current_understanding"
    draft = {field: "我和她完成了合成测试。", "/summary": "合成测试记录。"}
    if module == "learning_memory":
        # The unified writer has no preceding-context field; its value is "".
        draft.update({"/title": "合成经验", "/preceding_context_summary": ""})
    binding = {"field_path": field, "surface_form": "她", "occurrence_index": 0,
               "entity_ref": "synthetic-person"}
    return {
        "module": MODULES[module], "draft_version": 0, "draft_fields": draft,
        "referent_bindings": [{**binding, "resolution_status": "resolved", "confidence": 100}],
        "rewrite_targets": [{**binding, "target_surface_form": "测试者", "mention_kind": "pronoun",
                             "target_alias_ref": "synthetic-alias", "target_alias_version": 1,
                             "unique_in_scope": True}],
        **_context(module),
    }


def _confirmation_fields(module, preview):
    final = preview["suggested_fields"]
    return {
        "module": MODULES[module], "preview_id": preview["preview_id"],
        "expected_source_draft_hash": preview["source_draft_hash"],
        "expected_suggestion_hash": preview["suggestion_hash"],
        "expected_validation_context_hash": preview["validation_context_hash"],
        "final_fields": final, "final_fields_hash": _hash(final), "ai_confirmation": True,
        **_context(module),
    }


def _memory_fields(module, preview, receipt=None):
    final = preview["suggested_fields"]
    fields = {"module": module, "content": final.get("/original_text", final.get("/current_understanding")),
              "summary": final["/summary"]}
    if module == "learning_memory":
        fields["title"] = final["/title"]
    if receipt is not None:
        fields["rewrite_receipt"] = receipt
    return fields


class PersonRewriteSimpleEntryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = compact_fixtures.CompactOpenStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("offline synthetic test")))
        self.database = self.fixture.database.resolve()
        self.ideas = self.database.parent / "synthetic-ideas.db"
        real_connect = sqlite3.connect

        def connect(path, *args, **kwargs):
            if Path(path).resolve() not in {self.database, self.ideas}:
                raise AssertionError("only this test's synthetic databases may be opened")
            return real_connect(path, *args, **kwargs)

        self.enterContext(patch("sqlite3.connect", side_effect=connect))
        self.fixture.bootstrap_live()
        self.owner, self.model = self.fixture.service.owner_id, self.fixture.service.model_id
        self.host = self.fixture.onboarding
        common = {"onboarding": self.host, "owner_id": self.owner, "model_id": self.model}
        self.rewrites = AuthoringRewriteStore(self.database,
                            receipt_secret="synthetic-authoring-receipt-secret-32-bytes")
        self.authoring = AuthoringRewriteAccessService(self.rewrites, **common)
        self.emotional = EmotionalMemoryAccessService(EmotionalMemoryStore(self.database), **common)
        self.learning = LearningMemoryAccessService(
            LearningMemoryStore(self.database, idea_database=self.ideas), **common)
        self.planning = PlanningMemoryAccessService(PlanningMemoryStore(self.database), **common)
        self.daily = DailyMemoryAccessService(self.host, self.emotional, self.learning,
                                             self.planning, self.owner, self.model)

    def operation(self, scope, owner=None):
        return authenticated_ordinary_operation(owner_id=owner or self.owner, model_id=self.model, scope=scope)

    def preview_and_confirm(self, module, owner=None):
        authoring = self.authoring if owner is None else AuthoringRewriteAccessService(
            self.rewrites, onboarding=self.host, owner_id=owner, model_id=self.model)
        with self.operation("shared_person_authoring", owner) as access:
            preview = authoring.preview(write_context_ref=access["write_context_ref"],
                        expected_authoring_version=authoring.status()["row_version"],
                        **_preview_fields(module))
        self.assertEqual("preview_only", preview["decision"], preview)
        with self.operation("shared_person_authoring", owner) as access:
            confirmed = authoring.confirm(write_context_ref=access["write_context_ref"],
                        expected_authoring_version=authoring.status()["row_version"],
                        **_confirmation_fields(module, preview))
        self.assertEqual("confirmed", confirmed["decision"], confirmed)
        return preview, confirmed["rewrite_receipt"]

    def remember(self, module, preview, receipt=None, **changes):
        with self.operation(module):
            return self.daily.remember(**{**_memory_fields(module, preview, receipt), **changes})

    def rows(self, table):
        self.assertIn(table, {"emotion_memories", "learning_items", "planning_versions",
                              "authoring_rewrite_previews", "authoring_rewrite_receipts"})
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)]

    def test_confirmed_preview_stores_via_unified_entry_for_both_modules(self):
        for module in MODULES:
            with self.subTest(module=module):
                preview, receipt = self.preview_and_confirm(module)
                result = self.remember(module, preview, receipt)
                self.assertTrue(result["stored"], result)
                self.assertNotIn(receipt, json.dumps(result))
        self.assertEqual("我和测试者完成了合成测试。", self.rows("emotion_memories")[0]["original_text"])
        learning = json.loads(self.rows("learning_items")[0]["current_json"])
        self.assertEqual("我和测试者完成了合成测试。", learning["current_understanding"])
        self.assertEqual("", learning["preceding_context_summary"])
        self.assertEqual(["consumed", "consumed"], [row["status"] for row in self.rows("authoring_rewrite_receipts")])

    def test_same_receipt_never_creates_second_memory_and_changed_request_is_rejected(self):
        for module, table in (("emotional_memory", "emotion_memories"), ("learning_memory", "learning_items")):
            with self.subTest(module=module):
                preview, receipt = self.preview_and_confirm(module)
                stored = self.remember(module, preview, receipt)
                replay = self.remember(module, preview, receipt)
                self.assertTrue(replay["stored"], replay)
                self.assertEqual(stored["ref"], replay["ref"])
                self.assertEqual(1, len(self.rows(table)))
                rejected = self.remember(module, preview, receipt, reason="A different canonical request")
                self.assertEqual(["rewrite_receipt_replay_conflict"], rejected["reason_codes"])
                self.assertEqual(1, len(self.rows(table)))

    def test_missing_or_changed_final_fields_leave_receipt_unconsumed(self):
        for module in MODULES:
            with self.subTest(module=module):
                preview, receipt = self.preview_and_confirm(module)
                missing = self.remember(module, preview)
                self.assertEqual(["rewrite_receipt_required_for_exposed_suggestion"], missing["reason_codes"])
                changed = self.remember(module, preview, receipt, content="作者后来修改的另一份正文。")
                self.assertEqual(["rewrite_receipt_final_mismatch"], changed["reason_codes"])
                self.assertFalse(changed["state_changed"])
                self.assertEqual("ready", self.rows("authoring_rewrite_receipts")[-1]["status"])
        self.assertEqual([], self.rows("emotion_memories"))
        self.assertEqual([], self.rows("learning_items"))

    def test_receipt_from_another_author_is_rejected_without_consumption(self):
        for module in MODULES:
            with self.subTest(module=module):
                preview, receipt = self.preview_and_confirm(module, owner="synthetic-other-author")
                denied = self.remember(module, preview, receipt)
                self.assertEqual(["rewrite_receipt_binding_mismatch"], denied["reason_codes"])
                self.assertFalse(denied["state_changed"])
        self.assertEqual(["ready", "ready"], [row["status"] for row in self.rows("authoring_rewrite_receipts")])
        self.assertEqual([], self.rows("emotion_memories"))
        self.assertEqual([], self.rows("learning_items"))

    def test_default_path_preserves_author_voice_and_does_not_enable_next_save(self):
        original = "  她说：‘我和她在一起。’\n这是一段小说。  "
        for module in ("emotional_memory", "learning_memory", "planning_memory"):
            with self.operation(module):
                result = self.daily.remember(module, original)
            self.assertTrue(result["stored"], result)
        self.assertEqual(original, self.rows("emotion_memories")[0]["original_text"])
        self.assertEqual(original, json.loads(self.rows("learning_items")[0]["current_json"])["current_understanding"])
        self.assertEqual(original, json.loads(self.rows("planning_versions")[0]["content_json"])["original_text"])
        self.assertEqual([], self.rows("authoring_rewrite_previews"))
        preview, receipt = self.preview_and_confirm("emotional_memory")
        self.assertTrue(self.remember("emotional_memory", preview, receipt)["stored"])
        with self.operation("emotional_memory"):
            result = self.daily.remember("emotional_memory", "我和她继续讨论另一件事。")
        self.assertTrue(result["stored"], result)
        self.assertIn("我和她继续讨论另一件事。", [row["original_text"] for row in self.rows("emotion_memories")])
        self.assertEqual(1, len(self.rows("authoring_rewrite_previews")))

    def test_planning_receipt_is_not_silently_ignored_and_invalid_receipts_are_clear(self):
        preview, receipt = self.preview_and_confirm("emotional_memory")
        with self.operation("planning_memory"):
            result = self.daily.remember("planning_memory", "独立计划。", rewrite_receipt=receipt)
        self.assertEqual(["rewrite_receipt_module_unsupported"], result["reason_codes"])
        self.assertIn("规划脑暂未接入", result["next_step"])
        self.assertEqual([], self.rows("planning_versions"))
        self.assertEqual("ready", self.rows("authoring_rewrite_receipts")[0]["status"])
        for invalid in ("", "  ", False, {}):
            with self.operation("emotional_memory"):
                result = self.daily.remember("emotional_memory", "内容。", rewrite_receipt=invalid)
            self.assertEqual(["rewrite_receipt_invalid"], result["reason_codes"])
        self.assertTrue(self.remember("emotional_memory", preview, receipt)["stored"])


async def native_probe():
    root = Path(os.environ["STBRAIN_DB_PATH"]).parent.resolve()
    permitted = {root / filename for filename in ("main.db", "ideas.db", "vault.db")}
    real_connect = sqlite3.connect

    def connect(path, *args, **kwargs):
        assert Path(path).resolve() in permitted, "only synthetic probe databases"
        return real_connect(path, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("offline probe")))
        stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("offline probe")))
        stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("no nested process")))
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

        schema = server.mcp._tool_manager.get_tool("remember_memory").parameters
        assert "rewrite_receipt" in schema["properties"] and "rewrite_receipt" not in schema["required"]
        for module in MODULES:
            preview = await call("preview_person_reference_rewrite", _preview_fields(module))
            assert preview["decision"] == "preview_only", preview
            confirmation = await call("confirm_person_reference_rewrite", _confirmation_fields(module, preview))
            assert confirmation["decision"] == "confirmed", confirmation
            arguments = _memory_fields(module, preview, confirmation["rewrite_receipt"])
            stored = await call("remember_memory", arguments)
            assert stored["stored"] is True, stored
            replay = await call("remember_memory", arguments)
            assert replay["stored"] is True and replay["ref"] == stored["ref"], replay
        with closing(sqlite3.connect(root / "main.db")) as connection:
            assert connection.execute("SELECT COUNT(*) FROM emotion_memories").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM learning_items").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM authoring_rewrite_receipts WHERE status='consumed'").fetchone()[0] == 2
        return {"decision": "PASS", "native_unified_receipt_chain": True,
                "modules": list(MODULES), "canonical_memories": 2, "real_model_calls": 0}


class NativePersonRewriteSimpleEntryTests(unittest.TestCase):
    def test_registered_mcp_schema_and_both_unified_writer_chains(self):
        retained = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATH", "PATHEXT", "TEMP", "TMP", "OS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in retained}
        with tempfile.TemporaryDirectory(prefix="person-rewrite-simple-native-") as temporary:
            root = Path(temporary)
            env.update(STBRAIN_MCP_TOKEN="synthetic-token-00000000000000000000000",
                       STBRAIN_WAKE_SECRET="synthetic-wake-0000000000000000000000",
                       STBRAIN_OWNER_ID="synthetic-owner", STBRAIN_MODEL_ID="synthetic-model",
                       STBRAIN_DB_PATH=str(root / "main.db"), STBRAIN_LEARNING_IDEA_DB_PATH=str(root / "ideas.db"),
                       STBRAIN_HALLUCINATION_VAULT_DB_PATH=str(root / "vault.db"),
                       STBRAIN_REQUIRE_EXECUTION_BINDING="1", STBRAIN_EXECUTION_EPOCH="synthetic-authoring-epoch",
                       STBRAIN_ACCESS_PROFILE="simple-memory-v1", PYTHONIOENCODING="utf-8",
                       PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
            completed = subprocess.run([sys.executable, "-B", "-m",
                "mcp_server.tests.test_person_rewrite_simple_entry", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding="utf-8", timeout=60)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertTrue(proof["native_unified_receipt_chain"])
        self.assertEqual(2, proof["canonical_memories"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(native_probe())))
    else:
        unittest.main()
