"""Read every tool-experience outcome without changing memory or old views.

Fixtures, identities, and receipts are synthetic. SQLite is allowlisted to fresh
temporary files; sockets, listeners, models, and nested processes are forbidden.
The native probe exercises real MCP registration and compact guarded dispatch.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from mcp_server.tests.test_tool_guidance_service import FakeOnboarding
from mcp_server.tool_guidance_service import ToolGuidanceAccessService
from runtime.tool_guidance import EXPERIENCE_OUTCOMES, ToolGuidanceStore


_OS_ENV = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATH", "PATHEXT", "TEMP", "TMP", "OS"}


def clean_environment():
    return {key: value for key, value in os.environ.items() if key.upper() in _OS_ENV}


def sqlite_guard(permitted):
    real_connect = sqlite3.connect

    def connect(path, *args, **kwargs):
        value = str(path)
        if value.startswith("file:"):
            value = unquote(urlsplit(value).path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", value):
                value = value[1:]
        assert Path(value).resolve() in permitted, "only this probe's synthetic databases"
        return real_connect(path, *args, **kwargs)

    return connect


def block_external(stack):
    enter = stack.enterContext if isinstance(stack, unittest.TestCase) else stack.enter_context
    for name in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                 "socket.socket.sendto", "socket.socket.bind", "socket.getaddrinfo", "subprocess.Popen"):
        enter(patch(name, side_effect=AssertionError("offline synthetic readback")))


def database_snapshot(paths, *, omit_execution_transport=False):
    snapshot = {}
    for path in paths:
        if not path.exists():
            continue
        with closing(sqlite3.connect(path)) as connection:
            lines = list(connection.iterdump())
        if omit_execution_transport:
            lines = [line for line in lines if not line.startswith((
                'INSERT INTO "brain_execution_calls"', 'INSERT INTO "brain_execution_batches"'))]
        snapshot[path.name] = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return snapshot


class ToolExperienceReadbackTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, clean_environment(), clear=True))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="experience-readback-synthetic-"))).resolve()
        self.database = self.directory / "main.db"
        block_external(self)
        self.enterContext(patch("sqlite3.connect", side_effect=sqlite_guard({self.database})))
        self.store = ToolGuidanceStore(self.database)
        self.onboarding = FakeOnboarding()
        self.onboarding.allowed = True
        self.onboarding.add_wake("synthetic-ref", "synthetic-wake", 1)
        self.onboarding.contexts["synthetic-ref"]["context_mode"] = "ordinary_authenticated"
        self.owner, self.model = "synthetic-owner", "synthetic-model"
        self.service = self.service_for(self.owner, self.model)
        self.card = self.create_card(self.service)
        self.sequence = 0

    def service_for(self, owner, model):
        return ToolGuidanceAccessService(self.store, onboarding=self.onboarding,
            owner_id=owner, model_id=model, catalog_provider=None)

    def create_card(self, service, name="SyntheticReadbackTool"):
        result = service.remember(write_context_ref="synthetic-ref",
            expected_tool_row_version=service.status()["row_version"],
            tool_name=name, purpose="Read a synthetic example for an isolated test.",
            reminder="Consider the synthetic example.", scenario_tags=["SyntheticReadbackScene"],
            reason="Isolated readback fixture.")
        self.assertEqual("stored", result["decision"], result)
        return result["card"]["card_id"]

    def record(self, outcome="success", *, service=None, card=None, **changes):
        service = self.service if service is None else service
        self.sequence += 1
        when = datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.sequence)
        arguments = {"card_id": self.card if card is None else card, "outcome": outcome,
            "reason_code": "synthetic_observation", "attempt_summary": "Synthetic attempt " + str(self.sequence),
            "lesson": "Synthetic lesson " + str(self.sequence), "confidence": 100, **changes}
        with patch("runtime.tool_guidance._now_dt", return_value=when):
            result = service.record_experience(write_context_ref="synthetic-ref",
                expected_tool_row_version=service.status()["row_version"], **arguments)
        self.assertEqual("recorded", result["decision"], result)
        return result["experience"]

    def recall(self, **changes):
        return self.service.recall(**{"card_id": self.card, "view": "experiences", **changes})

    def snapshot(self):
        return database_snapshot([self.database])

    def test_success_partial_success_and_failure_are_read_back(self):
        expected = [self.record(outcome) for outcome in ("success", "partial_success", "timeout")]
        result = self.recall()
        self.assertEqual("precise_result", result["decision"], result)
        self.assertEqual("experiences", result["view"])
        self.assertEqual({row["experience_id"] for row in expected},
                         {row["experience_id"] for row in result["experiences"]})
        self.assertEqual(3, result["total"])
        self.assertFalse(result["truncated"])

    def test_every_allowed_outcome_has_an_exact_readback(self):
        expected = [self.record(outcome) for outcome in sorted(EXPERIENCE_OUTCOMES)]
        self.assertEqual(10, len(expected))
        for stored in expected:
            with self.subTest(outcome=stored["outcome"]):
                result = self.recall(query=stored["experience_id"])
                self.assertEqual(1, result["total"])
                self.assertFalse(result["truncated"])
                item = result["experiences"][0]
                for field in ("experience_id", "card_id", "card_version", "outcome", "reason_code",
                              "attempt_summary", "lesson", "confidence", "occurred_at",
                              "observed_schema_hash", "catalog_hash", "cooldown_until"):
                    self.assertEqual(stored[field], item[field], field)

    def test_latest_five_are_bounded_and_older_id_is_still_addressable(self):
        expected = [self.record() for _ in range(8)]
        result = self.recall()
        self.assertEqual(8, result["total"])
        self.assertTrue(result["truncated"])
        self.assertEqual([item["experience_id"] for item in reversed(expected[-5:])],
                         [item["experience_id"] for item in result["experiences"]])
        older = self.recall(query=expected[0]["experience_id"], limit=1)
        self.assertEqual([expected[0]["experience_id"]], [row["experience_id"] for row in older["experiences"]])
        self.assertEqual(1, older["total"])
        self.assertFalse(older["truncated"])
        narrowed = self.recall(limit=2)
        self.assertEqual(2, len(narrowed["experiences"]))
        self.assertEqual(8, narrowed["total"])

    def test_query_filters_attempt_lesson_reason_and_outcome_without_wildcards(self):
        stored = self.record("partial_success", attempt_summary="Synthetic Apricot attempt.",
            lesson="Synthetic Tangerine lesson.", reason_code="SyntheticCedarReason")
        self.record("timeout", attempt_summary="A different synthetic attempt.",
            lesson="A different synthetic lesson.", reason_code="other_reason")
        for query in ("APRICOT", "tangerine", "cedar", "partial_success"):
            with self.subTest(query=query):
                result = self.recall(query=query)
                self.assertEqual(1, result["total"])
                self.assertEqual(stored["experience_id"], result["experiences"][0]["experience_id"])
        for query in ("%", "' OR 1=1 --", "toolexp_" + "0" * 32, "not present"):
            result = self.recall(query=query)
            self.assertEqual([], result["experiences"])
            self.assertEqual(0, result["total"])
            self.assertFalse(result["truncated"])

    def test_self_reported_confidence_one_hundred_never_becomes_verified(self):
        stored = self.record(confidence=100)
        result = self.recall(query=stored["experience_id"])
        item = result["experiences"][0]
        self.assertEqual(100, item["confidence"])
        self.assertEqual("ai_reported", item["provenance"])
        self.assertIs(False, item["verified"])
        self.assertIsNone(item["evidence_ref"])
        self.assertEqual("historical_advice_only", result["guidance_authority"])
        self.assertEqual("none", result["permission_authority"])
        self.assertIs(False, result["execution_performed"])

    def test_read_and_filter_do_not_change_any_database_row_or_module_version(self):
        stored = self.record()
        before = self.snapshot()
        version = self.service.status()["row_version"]
        for args in ({}, {"limit": 1}, {"query": stored["experience_id"]}, {"query": "missing"}):
            result = self.recall(**args)
            self.assertEqual("precise_result", result["decision"])
            self.assertIs(False, result["state_changed"])
            self.assertEqual(before, self.snapshot())
            self.assertEqual(version, self.service.status()["row_version"])

    def test_wrong_card_owner_and_model_cannot_read_an_experience(self):
        owned = self.record(attempt_summary="Synthetic private-marker-own.")
        other_card = self.create_card(self.service, "SyntheticOtherCard")
        self.assertEqual([], self.recall(card_id=other_card, query=owned["experience_id"])["experiences"])
        for owner, model in (("synthetic-other-owner", self.model), (self.owner, "synthetic-other-model")):
            with self.subTest(owner=owner, model=model):
                service = self.service_for(owner, model)
                card = self.create_card(service)
                secret = self.record(service=service, card=card, attempt_summary="Synthetic other-identity-marker.")
                before = self.snapshot()
                denied = self.recall(card_id=card, query=secret["experience_id"])
                self.assertEqual(["tool_card_not_found"], denied["reason_codes"])
                self.assertNotIn(secret["experience_id"], json.dumps(denied))
                self.assertNotIn(secret["attempt_summary"], json.dumps(denied))
                empty = self.recall(query=secret["experience_id"])
                self.assertEqual([], empty["experiences"])
                reverse = service.recall(card_id=self.card, view="experiences")
                self.assertEqual(["tool_card_not_found"], reverse["reason_codes"])
                self.assertEqual(before, self.snapshot())

    def test_missing_card_and_out_of_range_limit_still_reject(self):
        self.record()
        before = self.snapshot()
        denied = self.recall(card_id=None)
        self.assertEqual(["card_id_required"], denied["reason_codes"])
        for limit in (0, 6, False, "5"):
            denied = self.recall(limit=limit)
            self.assertEqual(["invalid_limit"], denied["reason_codes"])
            self.assertEqual(before, self.snapshot())

    def test_legacy_failures_remains_failure_only_with_unchanged_shape(self):
        self.record("success")
        self.record("partial_success")
        failure = self.record("invalid_arguments")
        result = self.recall(view="failures")
        self.assertEqual([failure["experience_id"]], [row["experience_id"] for row in result["experiences"]])
        self.assertEqual({"experience_id", "outcome", "reason_code", "attempt_summary", "lesson",
                          "provenance", "verified", "occurred_at"}, set(result["experiences"][0]))
        self.assertNotIn("total", result)
        self.assertNotIn("truncated", result)

    def test_retired_and_nonadvertised_cards_keep_explicit_experience_readback(self):
        from tests.test_tool_guidance import catalog

        stored = self.record()
        # This complete synthetic catalog advertises HomeControl, not this card.
        self.service.catalog_provider = catalog()
        visible = self.recall(view="card")
        self.assertEqual("not_advertised", visible["results"][0]["availability"])
        before = self.snapshot()
        self.assertEqual(stored["experience_id"], self.recall()["experiences"][0]["experience_id"])
        self.assertEqual(before, self.snapshot())
        retired = self.service.revise(write_context_ref="synthetic-ref",
            expected_tool_row_version=self.service.status()["row_version"],
            card_id=self.card, expected_card_version=1, intent="retire",
            reason="Retire the synthetic fixture without removing its history.")
        self.assertEqual("retired", retired["card"]["lifecycle"], retired)
        before = self.snapshot()
        result = self.recall(query=stored["experience_id"])
        self.assertEqual("precise_result", result["decision"])
        self.assertEqual(stored["experience_id"], result["experiences"][0]["experience_id"])
        self.assertEqual(1, result["experiences"][0]["card_version"])
        self.assertEqual(before, self.snapshot())

    def test_card_history_and_automatic_recall_do_not_gain_experience_payloads(self):
        marker = "SyntheticExperiencePayloadOnly"
        stored = self.record(attempt_summary=marker, lesson=marker)
        for view in ("card", "history", "directory", "suggestions"):
            result = self.recall(view=view)
            self.assertNotIn("experiences", result)
            self.assertNotIn(marker, json.dumps(result))
            self.assertNotIn(stored["experience_id"], json.dumps(result))
        before = self.service.recall_for_injection(query="SyntheticReadbackScene")
        self.assertTrue(before["envelopes"])
        self.recall()
        after = self.service.recall_for_injection(query="SyntheticReadbackScene")
        self.assertEqual(before, after)
        self.assertNotIn(marker, json.dumps(after))
        self.assertNotIn(stored["experience_id"], json.dumps(after))

    def test_static_and_runtime_manual_explain_diagnostic_flag_and_parameter_shape(self):
        from mcp_server.usage_guide import module_usage_guide

        before = self.snapshot()
        manuals = [self.service.manual(), module_usage_guide("tool_guidance", simple=True)]
        for manual in manuals:
            with self.subTest(scope=manual.get("manual_scope", "runtime")):
                notes = " ".join(manual["read_notes"])
                for phrase in ("call_notes_available", "call_notes_current", "不是", "保存成功",
                               "最新版本", "false", "不删除原文", "平铺", "stbrain_manage", "arguments"):
                    self.assertIn(phrase, notes)
                self.assertIn("experiences", json.dumps(manual))
        self.assertEqual(before, self.snapshot())


async def native_probe():
    root = Path(os.environ["STBRAIN_DB_PATH"]).parent.resolve()
    databases = {root / name for name in ("main.db", "ideas.db", "vault.db")}
    with ExitStack() as guard:
        block_external(guard)
        guard.enter_context(patch("sqlite3.connect", side_effect=sqlite_guard(databases)))
        from mcp import types
        from jsonschema import Draft202012Validator
        from mcp_server import server
        from mcp_server.tests.test_tool_catalog_profile import EXPECTED_DAILY_NAMES, http_context
        from runtime.execution_binding import canonical_hash
        from tests.test_onboarding import ModuleOneOnboardingTests

        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / "main.db", server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()

        async def call(name, arguments):
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert json.loads(blocks[0].text) == result
            return result

        handler = server.mcp._mcp_server.request_handlers[types.ListToolsRequest]
        with http_context():
            full = (await handler(types.ListToolsRequest(method="tools/list"))).root.tools
        with http_context(query="tool_profile=daily"):
            daily = (await handler(types.ListToolsRequest(method="tools/list"))).root.tools
        assert len(full) == 44, len(full)
        assert len(daily) == 7 and {tool.name for tool in daily} == EXPECTED_DAILY_NAMES
        assert len(server.mcp._tool_manager.list_tools()) == 46
        for tool in daily:
            if tool.name in {"stbrain_tools", "stbrain_manage"}:
                assert "45" in tool.description and "常用" in tool.description and "分类" in tool.description
                assert tool.inputSchema == server.mcp._tool_manager.get_tool(tool.name).parameters
        full_schema = next(tool for tool in full if tool.name == "recall_tool_guidance").inputSchema
        assert "experiences" in full_schema["properties"]["view"]["enum"]
        discovery = await call("stbrain_tools", {"action": "recall_tool_guidance"})
        expected_schema = {**full_schema, "properties": {
            key: value for key, value in full_schema["properties"].items() if key != "execution_ref"}}
        assert discovery["arguments_schema"] == expected_schema
        assert "experiences" in discovery["arguments_schema"]["properties"]["view"]["enum"]

        saved = await call("remember_tool_guidance", {
            "tool_name": "SyntheticReadbackTool", "purpose": "Isolated experience readback."})
        assert saved["decision"] == "stored", saved
        card_id = saved["card"]["card_id"]
        records = []
        for outcome in ("success", "partial_success", "timeout"):
            result = await call("stbrain_manage", {"action": "record_tool_experience", "arguments": {
                "card_id": card_id, "outcome": outcome, "reason_code": "synthetic_result",
                "attempt_summary": "Synthetic native " + outcome, "confidence": 100}})
            assert result["decision"] == "recorded", result
            records.append(result["experience"])

        args = {"card_id": card_id, "view": "experiences", "limit": 5}
        Draft202012Validator(discovery["arguments_schema"]).validate(args)
        before = database_snapshot(databases)
        result = await call("stbrain_manage", {"action": "recall_tool_guidance", "arguments": args})
        assert result["decision"] == "precise_result", result
        assert result["total"] == 3 and result["truncated"] is False
        assert {item["experience_id"] for item in records} == {item["experience_id"] for item in result["experiences"]}
        assert all(item["confidence"] == 100 and item["verified"] is False
                   and item["provenance"] == "ai_reported" and item["evidence_ref"] is None
                   for item in result["experiences"])
        assert database_snapshot(databases) == before

        # A signed compact envelope must reach the same real guarded reader.
        common = {"owner_id": server.OWNER_ID, "model_id": server.MODEL_ID}
        manage = server.mcp._tool_manager.get_tool("stbrain_manage")
        entries = [{"canonical_name": "stbrain_manage", "schema_hash": canonical_hash(manage.parameters)}]
        catalog = {"contract": "advertised-tools/1", "catalog_complete": True,
                   "catalog_hash": canonical_hash(entries), "entries": entries}
        wake = server.onboarding.issue_wake(**common, host_id="synthetic-readback-host",
            thread_id="synthetic-readback-thread", source_kind="human_message", source_event_id="synthetic-readback-turn")
        context = server.onboarding.build_pre_generation_context(**common, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], source_digest="synthetic-source",
            host_contract_digest="synthetic-contract", advertised_tools=catalog)
        server.onboarding.confirm_context_injected(**common, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], context_hash=context["context_hash"])
        batch = {**common, "wake_id": wake["wake_id"], "wake_capability": wake["wake_capability"],
                 "batch_id": "synthetic-readback-batch", "revision": 1}
        issued = server._execution_store.issue_batch(**batch, calls=[{
            "call_id": "synthetic-readback-call", "canonical_tool": "recall_tool_guidance",
            "advertised_name": "stbrain_manage", "schema_hash": entries[0]["schema_hash"],
            "catalog_hash": catalog["catalog_hash"], "arguments_hash": canonical_hash(args)}])
        before = database_snapshot(databases, omit_execution_transport=True)
        bound = await call("stbrain_manage", {"action": "recall_tool_guidance", "arguments": args,
            "execution_ref": issued["executions"][0]["execution_ref"]})
        assert bound["decision"] == "precise_result", bound
        assert bound["experiences"] == result["experiences"]
        assert database_snapshot(databases, omit_execution_transport=True) == before
        assert server._execution_store.batch_status(**batch)["counts"]["completed"] == 1
        return {"decision": "PASS", "compact_direct_and_signed_readback": True,
                "full_tools": 44, "daily_tools": 7, "real_model_calls": 0,
                "external_network_requests": 0, "real_database_access": 0}


class NativeToolExperienceReadbackTests(unittest.TestCase):
    def test_discovery_compact_dispatch_signed_reader_and_fixed_catalog_sizes(self):
        env = clean_environment()
        with tempfile.TemporaryDirectory(prefix="experience-readback-native-") as temporary:
            root = Path(temporary)
            env.update(STBRAIN_MCP_TOKEN="synthetic-token-00000000000000000000000",
                STBRAIN_WAKE_SECRET="synthetic-wake-0000000000000000000000",
                STBRAIN_OWNER_ID="synthetic-owner", STBRAIN_MODEL_ID="synthetic-model",
                STBRAIN_DB_PATH=str(root / "main.db"), STBRAIN_LEARNING_IDEA_DB_PATH=str(root / "ideas.db"),
                STBRAIN_HALLUCINATION_VAULT_DB_PATH=str(root / "vault.db"),
                STBRAIN_REQUIRE_EXECUTION_BINDING="1", STBRAIN_EXECUTION_EPOCH="synthetic-readback-epoch",
                STBRAIN_ACCESS_PROFILE="simple-memory-v1", PYTHONIOENCODING="utf-8",
                PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-m", __name__, "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding="utf-8", timeout=90,
                creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("PASS", json.loads(result.stdout.strip().splitlines()[-1])["decision"])


class CompactToolboxNoticeTests(unittest.TestCase):
    def test_small_registry_uses_available_action_count_not_hardcoded_forty_five(self):
        from mcp.server.fastmcp import FastMCP
        from mcp_server.compact_tool_dispatch import install_compact_tool_dispatch

        self.enterContext(patch.dict(os.environ, clean_environment(), clear=True))
        block_external(self)
        for names in (("recall_tool_guidance",),
                      ("recall_tool_guidance", "remember_memory", "manage_injection_control")):
            with self.subTest(registered=len(names)):
                mcp = FastMCP("synthetic-toolbox-count")

                async def synthetic(value: str = "fixture") -> dict:
                    return {"synthetic": value}

                for name in names:
                    mcp.add_tool(synthetic, name=name)
                before = {tool.name: json.loads(json.dumps(tool.parameters))
                          for tool in mcp._tool_manager.list_tools()}
                install_compact_tool_dispatch(mcp)
                for name in ("stbrain_tools", "stbrain_manage"):
                    description = mcp._tool_manager.get_tool(name).description
                    self.assertIn(f"可按需访问 {len(names)} 项操作", description)
                    self.assertIn("包含常用能力", description)
                    self.assertIn("分类工具箱", description)
                    self.assertNotIn("45", description)
                for name, schema in before.items():
                    self.assertEqual(schema, mcp._tool_manager.get_tool(name).parameters)


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(native_probe())))
    else:
        unittest.main()
