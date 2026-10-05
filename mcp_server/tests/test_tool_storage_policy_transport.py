"""Tool-card authoring through the real offline MCP SDK dispatcher.

Each child owns three synthetic databases beside the repository (D: here).
This exercises native and compact dispatch, not HTTP authentication or a model.
No production store, external executor, network, or child service is available.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
import copy
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


MODES = ("direct_create", "compact_create", "direct_revise", "compact_revise",
         "defaults", "boundaries")


class ToolStoragePolicyTransportTests(unittest.TestCase):
    def run_probe(self, mode):
        repository = Path(__file__).resolve().parents[2]
        allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATH",
                   "PATHEXT", "OS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="tool-storage-synthetic-",
                                         dir=repository.parent) as directory:
            root = Path(directory).resolve()
            env.update({
                "TEMP": str(root), "TMP": str(root),
                "STBRAIN_ACCESS_PROFILE": "simple-memory-v1",
                "STBRAIN_MCP_TOKEN": "synthetic-storage-mcp-" + "m" * 40,
                "STBRAIN_WAKE_SECRET": "synthetic-storage-wake-" + "w" * 40,
                "STBRAIN_OWNER_ID": "synthetic-storage-owner",
                "STBRAIN_MODEL_ID": "synthetic-storage-model",
                "STBRAIN_DB_PATH": str(root / "main.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(root / "ideas.db"),
                "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(root / "vault.db"),
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "1",
                "STBRAIN_EXECUTION_EPOCH": "synthetic-storage-epoch",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                "PYTHONIOENCODING": "utf-8",
            })
            completed = subprocess.run(
                [sys.executable, "-B", "-m", "mcp_server.tests.test_tool_storage_policy_transport",
                 "--probe", mode],
                cwd=repository, env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=90,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertEqual(mode, proof["mode"])
        self.assertGreater(proof["registered_calls"], 0)
        self.assertEqual(0, proof["network_or_process_attempts"])
        self.assertTrue(proof["only_synthetic_databases"])
        self.assertTrue(proof["identity_and_execution_records_unchanged"])
        self.assertTrue(proof["rejections_preserved_all_three_databases"])

    def test_direct_authoring_accepts_independent_risk_and_confirmation(self):
        self.run_probe("direct_create")

    def test_compact_authoring_accepts_independent_risk_and_confirmation(self):
        self.run_probe("compact_create")

    def test_unified_revision_preserves_original_text_and_versions(self):
        self.run_probe("direct_revise")

    def test_compact_unified_revision_preserves_original_text_and_versions(self):
        self.run_probe("compact_revise")

    def test_native_and_compact_defaults_are_medium_and_none(self):
        self.run_probe("defaults")

    def test_activation_secrets_owner_and_execution_binding_still_reject(self):
        self.run_probe("boundaries")


async def probe(mode):
    assert mode in MODES
    root = Path(os.environ["STBRAIN_DB_PATH"]).resolve().parent
    databases = {root / name for name in ("main.db", "ideas.db", "vault.db")}
    real_connect = sqlite3.connect
    attempts = []

    def only_synthetic(path, *args, **kwargs):
        from tests.sqlite_fixture_guard import is_synthetic_sqlite_path
        assert is_synthetic_sqlite_path(path, databases, uri=kwargs.get("uri")), "outside_synthetic_database"
        return real_connect(path, *args, **kwargs)

    def no_network_or_process(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("network_or_child_service_forbidden")

    with ExitStack() as guards:
        for target in ("socket.create_connection", "socket.socket.connect",
                       "socket.socket.connect_ex", "socket.socket.sendto",
                       "socket.socket.bind", "socket.getaddrinfo", "subprocess.Popen"):
            guards.enter_context(patch(target, side_effect=no_network_or_process))
        guards.enter_context(patch("sqlite3.connect", side_effect=only_synthetic))
        from mcp_server import server
        from tests.test_onboarding import ModuleOneOnboardingTests

        calls = 0
        rejections = 0

        def rows(sql, parameters=()):
            with closing(sqlite3.connect(root / "main.db")) as connection:
                return connection.execute(sql, parameters).fetchall()

        def snapshot():
            result = {}
            for path in sorted(databases):
                with closing(sqlite3.connect(path)) as connection:
                    result[path.name] = hashlib.sha256(
                        "\n".join(connection.iterdump()).encode()).hexdigest()
            return result

        async def call(name, arguments, compact=False):
            nonlocal calls
            original = copy.deepcopy(arguments)
            actual_name = "stbrain_manage" if compact else name
            actual_args = {"action": name, "arguments": arguments} if compact else arguments
            blocks, result = await server.mcp.call_tool(actual_name, actual_args)
            assert json.loads(blocks[0].text) == result, "mcp_text_structured_mismatch"
            assert arguments == original, "caller_arguments_changed"
            calls += 1
            return result

        async def reject(name, arguments, reason, compact=False):
            nonlocal rejections
            before = snapshot()
            result = await call(name, arguments, compact)
            assert result.get("decision") == "reject", "rejection_missing"
            assert result.get("state_changed") is False, "rejection_claimed_change"
            assert reason in result.get("reason_codes", [result.get("reason_code")]), result
            assert before == snapshot(), "rejection_changed_database"
            serialized = json.dumps(result, ensure_ascii=False)
            assert "SYNTHETIC_PRIVATE_" not in serialized, "rejected_text_echoed"
            assert os.environ["STBRAIN_MCP_TOKEN"] not in serialized, "credential_echoed"
            assert os.environ["STBRAIN_WAKE_SECRET"] not in serialized, "secret_echoed"
            rejections += 1
            return result

        def fields(**changes):
            return {"tool_name": "合成外发删除上传工具_" + str(calls),
                    "purpose": "保存工具说明，不执行外发、删除或上传。\n合成原文逐字读回。",
                    "call_notes": "这些是作者说明；实际调用仍由当前工具授权。",
                    "capability_class": "real_world_action", **changes}

        async def read_card(card_id):
            result = await call("recall_tool_guidance", {"card_id": card_id, "view": "card"})
            assert result["execution_performed"] is False
            assert len(result["results"]) == 1
            card = result["results"][0]
            assert card["permission_authority"] == "none"
            return card

        async def create(compact=False, **changes):
            arguments = fields(**changes)
            result = await call("remember_tool_guidance", arguments, compact)
            assert result.get("decision") == "stored", result
            assert result["execution_performed"] is False
            card = await read_card(result["card"]["card_id"])
            assert card["version"] == 1
            for key in ("purpose", "call_notes", "capability_class"):
                assert card["content"][key] == arguments[key], "author_content_changed"
            assert card["content"]["risk_level"] == arguments.get("risk_level", "medium")
            assert card["content"]["confirmation_policy"] == arguments.get("confirmation_policy", "none")
            return card

        if mode == "boundaries":
            for compact in (False, True):
                await reject("remember_tool_guidance", fields(risk_level="low", confirmation_policy="none"),
                             "module_one_required", compact)

        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / "main.db", server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        protected_tables = ("self_model_revisions", "brain_wake_sessions", "brain_execution_batches",
                            "brain_execution_calls", "tool_experiences")
        protected = {table: rows('SELECT * FROM "' + table + '" ORDER BY rowid')
                     for table in protected_tables}

        if mode in ("direct_create", "compact_create"):
            for risk in ("low", "high", "critical"):
                for confirmation in ("none", "contextual"):
                    await create(mode == "compact_create", risk_level=risk, confirmation_policy=confirmation)

        elif mode == "defaults":
            schema = server.mcp._tool_manager.get_tool("remember_tool_guidance").parameters
            assert schema["properties"]["risk_level"]["default"] == "medium"
            assert schema["properties"]["confirmation_policy"]["default"] == "none"
            for compact in (False, True):
                await create(compact)

        elif mode in ("direct_revise", "compact_revise"):
            card = await create(risk_level="critical", confirmation_policy="explicit_each_time")
            card_id = card["card_id"]
            original = rows("SELECT * FROM tool_card_versions WHERE card_id=? ORDER BY version", (card_id,))
            current_ref = f"tool-card://{card_id}@1"
            expected_versions = [card["content"]]
            for risk in ("low", "high"):
                changes = {"risk_level": risk, "confirmation_policy": "none",
                           "purpose": "SYNTHETIC_PRIVATE_" + risk + "：作者的新说明。\n不替目标工具授权。"}
                result = await call("revise_memory", {"target_ref": current_ref, "changes": changes},
                                    mode == "compact_revise")
                assert result.get("revised") is True, result
                assert result["execution_performed"] is False
                assert changes["purpose"] not in json.dumps(result, ensure_ascii=False)
                current_ref = result["ref"]
                current = await read_card(card_id)
                for key, value in changes.items():
                    assert current["content"][key] == value, "revision_changed_author_text"
                assert current["content"]["call_notes"] == card["content"]["call_notes"]
                expected_versions.append(current["content"])
            history = await call("recall_tool_guidance", {"card_id": card_id, "view": "history"})
            assert [item["content"] for item in reversed(history["versions"])] == expected_versions
            assert rows("SELECT * FROM tool_card_versions WHERE card_id=? ORDER BY version", (card_id,))[:1] == original
            await reject("revise_memory", {"target_ref": f"tool-card://{card_id}@1",
                         "changes": {"risk_level": "low"}}, "tool_card_version_conflict")

        elif mode == "boundaries":
            card = await create(risk_level="low", confirmation_policy="none")
            ref = f"tool-card://{card['card_id']}@1"
            for compact in (False, True):
                await reject("remember_tool_guidance", fields(purpose="Authorization: Bearer " + os.environ["STBRAIN_MCP_TOKEN"]),
                             "credential_or_secret_detected", compact)
                await reject("remember_tool_guidance", fields(owner_id="synthetic-other-owner"),
                             "compact_action_arguments_invalid" if compact else "invalid_tool_arguments", compact)
                await reject("remember_tool_guidance", fields(execution_ref="stexec_" + "f" * 43),
                             "compact_nested_execution_ref_forbidden" if compact else "execution_binding_invalid_or_finished", compact)
                await reject("revise_memory", {"target_ref": ref,
                             "changes": {"risk_level": "high", "confirmation_policy": "none",
                                         "purpose": "secret: " + os.environ["STBRAIN_WAKE_SECRET"]}},
                             "credential_or_secret_detected", compact)
                await reject("revise_memory", {"target_ref": ref,
                             "changes": {"owner_id": "synthetic-other-owner"}},
                             "compact_action_arguments_invalid" if compact else "ordinary_revision_requires_advanced", compact)
                await reject("revise_memory", {"target_ref": ref, "changes": {"risk_level": "high"},
                             "execution_ref": "stexec_" + "f" * 43},
                             "compact_nested_execution_ref_forbidden" if compact else "execution_binding_invalid_or_finished", compact)
            # A separate synthetic author's card cannot be revised or leaked by
            # the authenticated MCP identity; no server identity is changed.
            store = server.tool_guidance_service.store
            foreign = store.remember(owner_id="synthetic-foreign-owner", model_id=server.MODEL_ID,
                wake_id="synthetic-foreign-fixture", expected_row_version=0,
                reason="Synthetic isolation fixture", tool_name="SyntheticForeignTool",
                purpose="SYNTHETIC_PRIVATE_FOREIGN_CARD")["card"]
            await reject("revise_memory", {"target_ref": f"tool-card://{foreign['card_id']}@1",
                         "changes": {"risk_level": "low", "confirmation_policy": "none"}},
                         "tool_card_not_found")
            before_read = snapshot()
            result = await call("recall_tool_guidance", {"card_id": foreign["card_id"], "view": "card"})
            assert "SYNTHETIC_PRIVATE_FOREIGN_CARD" not in json.dumps(result)
            assert not result.get("results"), "foreign_card_leaked"
            assert snapshot() == before_read
            assert (await read_card(card["card_id"]))["content"] == card["content"]
            # Exact configured values must be protected even without a label;
            # this differs from the pattern-based credential cases above.
            for compact in (False, True):
                for variable in ("STBRAIN_MCP_TOKEN", "STBRAIN_WAKE_SECRET"):
                    secret = os.environ[variable]
                    for changes in ({"purpose": secret},
                                    {"keywords": ["synthetic-safe-word", secret]},
                                    {"additional_source_refs": [secret]}):
                        await reject("remember_tool_guidance", fields(**changes),
                                     "credential_or_secret_detected", compact)
                        await reject("revise_memory", {"target_ref": ref, "changes": changes},
                                     "credential_or_secret_detected", compact)

        assert {table: rows('SELECT * FROM "' + table + '" ORDER BY rowid')
                for table in protected_tables} == protected, "identity_or_execution_records_changed"
        assert (server.OWNER_ID, server.MODEL_ID) == (
            "synthetic-storage-owner", "synthetic-storage-model")
        assert rows("SELECT COUNT(*) FROM brain_execution_calls")[0][0] == 0
        assert rows("SELECT COUNT(*) FROM tool_experiences")[0][0] == 0
        assert not attempts
        return {"decision": "PASS", "mode": mode, "registered_calls": calls,
                "rejection_checks": rejections, "network_or_process_attempts": len(attempts),
                "only_synthetic_databases": True, "identity_and_execution_records_unchanged": True,
                "rejections_preserved_all_three_databases": True}


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        print(json.dumps(asyncio.run(probe(sys.argv[2])), ensure_ascii=False))
    else:
        unittest.main()
