"""Unified tool-card revision adapter; synthetic databases and native MCP only."""
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

from mcp_server.daily_revision_service import DailyRevisionAccessService, parse_revision_target
from mcp_server.tool_guidance_service import ToolGuidanceAccessService
from mcp_server.tests import test_compact_open as compact_fixtures
from runtime.ordinary_access import authenticated_ordinary_operation
from runtime.tool_guidance import ToolGuidanceStore


class UnifiedToolRevisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = compact_fixtures.CompactOpenStateTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("offline synthetic test")))
        self.database = self.fixture.database.resolve()
        connect = sqlite3.connect

        def only_synthetic(path, *args, **kwargs):
            if Path(path).resolve() != self.database:
                raise AssertionError("only this synthetic database may be opened")
            return connect(path, *args, **kwargs)

        self.enterContext(patch("sqlite3.connect", side_effect=only_synthetic))
        self.fixture.bootstrap_live()
        self.host = self.fixture.onboarding
        self.owner, self.model = self.fixture.service.owner_id, self.fixture.service.model_id
        self.store = ToolGuidanceStore(self.database)
        self.tools = ToolGuidanceAccessService(self.store, onboarding=self.host,
                                              owner_id=self.owner, model_id=self.model)
        self.revisions = DailyRevisionAccessService(self.host, None, None, None,
                               self.owner, self.model, tool_guidance_service=self.tools)
        with self.operation() as access:
            result = self.tools.remember(write_context_ref=access["write_context_ref"],
                expected_tool_row_version=self.tools.status()["row_version"],
                tool_name="合成家庭服务", purpose="在回家场景查看已知设备。",
                reminder="回家了，可以看看设备状态。", keywords=["回家"], scenario_tags=["回家了"],
                source_ref="synthetic:original-source", expires_at="2099-01-01T00:00:00Z",
                reason="Synthetic initial tool card")
        self.assertEqual("stored", result["decision"], result)
        self.card_id = result["card"]["card_id"]
        self.ref = f"tool-card://{self.card_id}@1"

    def operation(self, owner=None):
        return authenticated_ordinary_operation(owner_id=owner or self.owner,
                                                 model_id=self.model, scope="tool_guidance")

    def revise(self, changes, target_ref=None, **kwargs):
        with self.operation():
            return self.revisions.revise(target_ref or self.ref, changes, **kwargs)

    def rows(self, table):
        self.assertIn(table, {"tool_cards", "tool_card_versions", "tool_audit_events", "tool_guidance_candidates"})
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)]

    def card(self):
        with self.operation():
            result = self.tools.recall(card_id=self.card_id, view="card")
        return result["results"][0]

    def test_actual_reference_scope_and_identity_parser(self):
        self.assertEqual(("tool_guidance", self.card_id, 1), parse_revision_target(self.ref))
        for ref in (self.ref.replace("tool-card:", "tool:"), self.ref.replace("toolcard_", "learn_"),
                    self.ref.replace("@1", "@latest"), self.ref.replace("@1", ""), self.ref.replace("@1", "@0")):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                parse_revision_target(ref)

    def test_author_fields_use_existing_transaction_and_preserve_history(self):
        before = self.rows("tool_card_versions")[0]
        result = self.revise({"purpose": "补充后的真实作者原文。", "reminder": "到家后可以看看设备。",
                              "scenario_tags": ["回家啦", "到家了"], "keywords": ["家庭", "设备"],
                              "confidence": 100, "source_type": "ai_firsthand",
                              "additional_source_refs": ["synthetic:additional-source"],
                              "referent_bindings": []})
        self.assertTrue(result["revised"], result)
        self.assertEqual("tool_guidance", result["module"])
        self.assertEqual(2, result["version"])
        self.assertEqual(self.ref, result["rollback_ref"])
        self.assertFalse(result["execution_performed"])
        self.assertNotIn("补充后的真实作者原文", json.dumps(result, ensure_ascii=False))
        self.assertEqual(before, self.rows("tool_card_versions")[0])
        content = self.card()["content"]
        self.assertEqual("补充后的真实作者原文。", content["purpose"])
        self.assertEqual(100, content["claimed_confidence"])
        self.assertEqual(["回家啦", "到家了"], content["scenario_tags"])
        self.assertEqual(["synthetic:additional-source"], content["additional_source_refs"])
        self.assertEqual([], self.rows("tool_guidance_candidates"))

    def test_three_direct_null_clears_and_legacy_clear_fields(self):
        first = self.revise({"expires_at": None, "reminder": None, "source_ref": None})
        self.assertTrue(first["revised"], first)
        content = self.card()["content"]
        self.assertIsNone(content["expires_at"])
        self.assertIsNone(content["source_ref"])
        self.assertIsNone(content.get("reminder"))
        second = self.revise({"reminder": "另一句提醒。"}, first["ref"])
        third = self.revise({"clear_fields": ["reminder", "reminder"]}, second["ref"])
        self.assertTrue(third["revised"], third)
        self.assertIsNone(self.card()["content"].get("reminder"))
        previous_purpose = self.card()["content"]["purpose"]
        fourth = self.revise({"purpose": None, "display_label": "新显示名"}, third["ref"])
        self.assertTrue(fourth["revised"], fourth)
        self.assertEqual(previous_purpose, self.card()["content"]["purpose"])

    def test_clear_conflict_and_invalid_control_fields_leave_card_unchanged(self):
        before = self.rows("tool_card_versions")
        for changes, code in (
            ({"reminder": "保留这句", "clear_fields": ["reminder"]}, "set_and_clear_conflict"),
            ({"clear_fields": ["purpose"]}, "invalid_clear_fields"),
            ({"clear_fields": "reminder"}, "invalid_clear_fields"),
            ({"owner_id": "another-author"}, "ordinary_revision_requires_advanced"),
            ({"lifecycle": "retired"}, "ordinary_revision_requires_advanced"),
        ):
            with self.subTest(code=code):
                result = self.revise(changes)
                self.assertFalse(result["revised"])
                self.assertEqual([code], result["reason_codes"])
                self.assertEqual(before, self.rows("tool_card_versions"))

    def test_stale_target_and_cross_owner_keep_exact_scope_and_version(self):
        changed = self.revise({"purpose": "新版说明。"})
        self.assertTrue(changed["revised"], changed)
        stale = self.revise({"purpose": "旧版覆盖尝试。"})
        self.assertEqual(["tool_card_version_conflict"], stale["reason_codes"])
        other_tools = ToolGuidanceAccessService(self.store, onboarding=self.host,
                                owner_id="synthetic-other-owner", model_id=self.model)
        other = DailyRevisionAccessService(self.host, None, None, None,
                            "synthetic-other-owner", self.model, tool_guidance_service=other_tools)
        with self.operation(owner="synthetic-other-owner"):
            denied = other.revise(changed["ref"], {"purpose": "Foreign author update"})
        self.assertEqual(["tool_card_not_found"], denied["reason_codes"])
        self.assertEqual("新版说明。", self.card()["content"]["purpose"])
        self.assertEqual(2, len(self.rows("tool_card_versions")))

    def test_retire_restore_typos_and_source_addition_keep_existing_controls(self):
        retired = self.revise({"intent": "retire"})
        self.assertTrue(retired["revised"], retired)
        self.assertEqual("retired", self.card()["lifecycle"])
        restored = self.revise({"intent": "restore", "target_version": 1}, retired["ref"])
        self.assertTrue(restored["revised"], restored)
        self.assertEqual("active", self.card()["lifecycle"])
        typo = self.revise({"edit_class": "typo", "field_name": "purpose",
                           "before_text": "已知设备", "after_text": "已连接设备"}, restored["ref"])
        self.assertTrue(typo["revised"], typo)
        addition = self.revise({"edit_class": "source_addition", "source_ref": "synthetic:new-source"}, typo["ref"])
        self.assertTrue(addition["revised"], addition)
        self.assertIn("synthetic:new-source", self.card()["content"]["additional_source_refs"])
        self.assertEqual(5, len(self.rows("tool_card_versions")))

    def test_risk_floor_and_confidence_error_hints_are_preserved(self):
        risk = self.revise({"risk_level": "low"})
        self.assertEqual(["risk_below_runtime_floor"], risk["reason_codes"])
        self.assertEqual("high", risk["repair_guidance"]["real_world_action_minimum_risk_level"])
        confidence = self.revise({"confidence": 101})
        self.assertEqual(["invalid_confidence"], confidence["reason_codes"])
        self.assertEqual(100, confidence["repair_guidance"]["maximum"])
        self.assertEqual(1, len(self.rows("tool_card_versions")))

    def test_receipt_mismatch_is_not_announced_as_saved_and_backend_body_is_redacted(self):
        with patch.object(self.tools, "_write", return_value={
            "decision": "version_appended", "card": {"card_id": self.card_id, "version": 2,
            "content": {"private": "private-backend-sentinel"}}, "rollback_ref": self.ref,
            "tool_row_version": 999, "state_changed": True, "active_version_changed": True,
            "execution_performed": False}):
            result = self.revise({"purpose": "Synthetic change"})
        self.assertEqual("not_confirmed_revised", result["decision"])
        self.assertFalse(result["revised"])
        self.assertNotIn("private-backend-sentinel", json.dumps(result))
        self.assertEqual(1, len(self.rows("tool_card_versions")))


async def native_probe():
    root = Path(os.environ["STBRAIN_DB_PATH"]).parent.resolve()
    allowed = {root / name for name in ("main.db", "ideas.db", "vault.db")}
    connect = sqlite3.connect

    def only_synthetic(path, *args, **kwargs):
        assert Path(path).resolve() in allowed, "only synthetic probe databases"
        return connect(path, *args, **kwargs)

    with ExitStack() as stack:
        for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.bind",
                       "socket.getaddrinfo", "subprocess.Popen"):
            stack.enter_context(patch(target, side_effect=AssertionError("offline synthetic MCP probe")))
        stack.enter_context(patch("sqlite3.connect", side_effect=only_synthetic))
        from mcp_server import server
        from tests import test_onboarding as onboarding_fixtures

        async def call(name, args):
            blocks, result = await server.mcp.call_tool(name, args)
            assert json.loads(blocks[0].text) == result
            return result

        listed = await server.mcp.list_tools()
        names = {tool.name for tool in listed}
        assert "revise_memory" in names and "revise_tool_guidance" not in names, names
        assert server.mcp._tool_manager.get_tool("revise_tool_guidance") is not None
        fake_ref = "tool-card://toolcard_" + "0" * 32 + "@1"
        denied = await call("revise_memory", {"target_ref": fake_ref, "changes": {"purpose": "Synthetic"}})
        assert denied.get("reason_code") == "module_one_required", denied
        old_denied = await call("revise_tool_guidance", {"card_id": "toolcard_" + "0" * 32,
                            "expected_card_version": 1, "purpose": "Synthetic"})
        assert old_denied.get("reason_code") == "module_one_required", old_denied
        fixture = onboarding_fixtures.ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / "main.db", server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        stored = await call("remember_tool_guidance", {"tool_name": "合成家庭服务",
                            "purpose": "查看家庭设备。", "reminder": "回家可以看看设备。",
                            "scenario_tags": ["回家了"], "reason": "Synthetic native initial card"})
        assert stored["decision"] == "stored", stored
        card_id = stored["card"]["card_id"]
        ref = f"tool-card://{card_id}@1"
        revised = await call("revise_memory", {"target_ref": ref,
                             "changes": {"purpose": "直接更新后的说明。", "confidence": 100}})
        assert revised.get("revised") is True and revised["version"] == 2, revised
        stale = await call("revise_memory", {"target_ref": ref, "changes": {"purpose": "Stale attempt"}})
        assert stale["reason_codes"] == ["tool_card_version_conflict"], stale
        cleared = await call("revise_memory", {"target_ref": revised["ref"], "changes": {"reminder": None}})
        assert cleared.get("revised") is True and cleared["version"] == 3, cleared
        legacy = await call("revise_tool_guidance", {"card_id": card_id, "expected_card_version": 3,
                           "purpose": "旧入口兼容修改。", "reason": "Synthetic compatible legacy call"})
        assert legacy["decision"] == "version_appended" and legacy["card"]["version"] == 4, legacy
        assert legacy["execution_performed"] is False
        history = await call("recall_tool_guidance", {"view": "history", "card_id": card_id})
        assert [item["version"] for item in history["versions"]] == [4, 3, 2, 1], history
        assert history["versions"][-1]["content"]["purpose"] == "查看家庭设备。"
        with closing(sqlite3.connect(root / "main.db")) as connection:
            assert connection.execute("SELECT COUNT(*) FROM tool_card_versions").fetchone()[0] == 4
            assert connection.execute("SELECT COUNT(*) FROM tool_guidance_candidates").fetchone()[0] == 0
        return {"decision": "PASS", "simple_directory_compact": True, "legacy_dispatch_preserved": True,
                "initial_module_one_gate_preserved": True, "native_unified_tool_revision": True,
                "history_versions": 4, "real_model_calls": 0}


class NativeUnifiedToolRevisionTests(unittest.TestCase):
    def test_native_unified_revision_and_compact_catalog_preserve_legacy_permissions(self):
        allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATH", "PATHEXT", "TEMP", "TMP", "OS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="unified-tool-revision-native-") as directory:
            root = Path(directory)
            env.update(STBRAIN_MCP_TOKEN="synthetic-token-00000000000000000000000",
                       STBRAIN_WAKE_SECRET="synthetic-wake-0000000000000000000000",
                       STBRAIN_OWNER_ID="synthetic-owner", STBRAIN_MODEL_ID="synthetic-model",
                       STBRAIN_DB_PATH=str(root / "main.db"), STBRAIN_LEARNING_IDEA_DB_PATH=str(root / "ideas.db"),
                       STBRAIN_HALLUCINATION_VAULT_DB_PATH=str(root / "vault.db"),
                       STBRAIN_REQUIRE_EXECUTION_BINDING="1", STBRAIN_EXECUTION_EPOCH="synthetic-tool-revision-epoch",
                       STBRAIN_ACCESS_PROFILE="simple-memory-v1", PYTHONIOENCODING="utf-8",
                       PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
            completed = subprocess.run([sys.executable, "-B", "-m", "mcp_server.tests.test_unified_tool_revision", "--probe"],
                    cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                    text=True, encoding="utf-8", timeout=60)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertTrue(proof["native_unified_tool_revision"])
        self.assertTrue(proof["initial_module_one_gate_preserved"])
        self.assertTrue(proof["legacy_dispatch_preserved"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(native_probe())))
    else:
        unittest.main()
