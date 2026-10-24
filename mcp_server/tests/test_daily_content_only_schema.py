"""Actual registered entry signatures with synthetic facades; never a live brain.

Authorization and persistence are tested separately. Here the registered SDK
argument models and function bodies must agree with published content-only
schemas, without relying on a discovery-only schema rewrite.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class DailyContentOnlySchemaTests(unittest.TestCase):
    def test_real_registered_schema_and_entry_defaults_without_live_stores(self):
        system = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATH", "PATHEXT", "TEMP", "TMP", "OS"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in system}
        with tempfile.TemporaryDirectory(prefix="synthetic-content-only-schema-") as directory:
            root = Path(directory).resolve()
            environment.update({
                "STBRAIN_ACCESS_PROFILE": "simple-memory-v1",
                "STBRAIN_MCP_TOKEN": "synthetic-content-only-token-" + "x" * 40,
                "STBRAIN_WAKE_SECRET": "synthetic-content-only-wake-" + "y" * 40,
                "STBRAIN_OWNER_ID": "synthetic-content-only-owner",
                "STBRAIN_MODEL_ID": "synthetic-content-only-model",
                "STBRAIN_DB_PATH": str(root / "main.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(root / "ideas.db"),
                "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(root / "vault.db"),
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "1",
                "STBRAIN_EXECUTION_EPOCH": "synthetic-content-only-epoch",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONIOENCODING": "utf-8",
            })
            result = subprocess.run(
                [sys.executable, "-B", "-m", "mcp_server.tests.test_daily_content_only_schema", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=environment, capture_output=True,
                text=True, encoding="utf-8", timeout=75,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertEqual(13, proof["accepted_signature_cases"])
        self.assertEqual(10, proof["rejected_signature_cases"])
        self.assertEqual(0, proof["network_attempts"])
        self.assertEqual(62, proof["registered_tools"])
        self.assertEqual(61, proof["manager_listed_tools"])


async def probe():
    root = Path(os.environ["STBRAIN_DB_PATH"]).resolve().parent
    databases = {root / name for name in ("main.db", "ideas.db", "vault.db")}
    real_connect = sqlite3.connect
    network_attempts = []

    def only_synthetic(path, *args, **kwargs):
        from tests.sqlite_fixture_guard import is_synthetic_sqlite_path
        assert is_synthetic_sqlite_path(path, databases, uri=kwargs.get("uri")), "outside_synthetic_database"
        return real_connect(path, *args, **kwargs)

    def no_network(*args, **kwargs):
        network_attempts.append(True)
        raise AssertionError("network_or_child_service_forbidden")

    with ExitStack() as guards:
        for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                       "socket.socket.sendto", "socket.socket.bind", "socket.getaddrinfo", "subprocess.Popen"):
            guards.enter_context(patch(target, side_effect=no_network))
        guards.enter_context(patch("sqlite3.connect", side_effect=only_synthetic))
        from mcp_server import server
        from mcp_server.compact_catalog_schema import project_compact_tool
        from mcp_server.public_contract import PUBLIC_TOOL_NAMES
        from mcp import types
        from jsonschema import Draft202012Validator
        from pydantic import ValidationError

        seen = []

        def facade(**arguments):
            seen.append(deepcopy(arguments))
            return {"synthetic_signature_probe": True}

        guards.enter_context(patch.object(server.daily_service, "remember", side_effect=facade))
        guards.enter_context(patch.object(server.work_memory_service, "remember", side_effect=facade))
        manager = server.mcp._tool_manager
        assert set(manager._tools) == set(PUBLIC_TOOL_NAMES) | {
            "authorize_self_model", "stbrain_tools", "stbrain_manage", "stage_memory_image_upload",
        }, "registered_tool_set_changed"
        assert {tool.name for tool in manager.list_tools()} == set(manager._tools) - {"revise_tool_guidance"}
        daily = manager.get_tool("remember_memory")
        work = manager.get_tool("remember_work_memory")
        for tool in (daily, work):
            assert tool.parameters["required"] == ["content"]
            assert tool.parameters["additionalProperties"] is False
            compact = project_compact_tool(types.Tool(
                name=tool.name, description=tool.description, inputSchema=tool.parameters,
            ))
            assert compact.inputSchema["required"] == ["content"]
        assert daily.parameters["properties"]["module"]["default"] == "auto"
        assert daily.parameters["properties"]["importance"]["default"] == 50
        assert daily.parameters["properties"]["emotion"]["default"] == "other"
        assert work.parameters["properties"]["tag"]["default"] == "随手记"

        accepted = [
            (daily, {"content": "  Synthetic original\n "}),
            (daily, {"content": "Synthetic", "module": None, "kind": None, "emotion": None,
                     "importance": None, "keywords": None}),
            (daily, {"content": "Synthetic", "module": "", "kind": "", "emotion": "",
                     "importance": "", "keywords": ""}),
            (daily, {"content": "Synthetic", "kind": "moment"}),
            (daily, {"content": "Synthetic", "module": "learning", "importance": "55", "keywords": "经验，课堂"}),
            (daily, {"content": "Synthetic", "importance": 55.0, "keywords": ["经验", "课堂"]}),
            (daily, {"content": "Synthetic", "kind": "作者自定分类", "emotion": "作者感受"}),
            (daily, {"content": "Synthetic", "module": "learning_memory", "rewrite_receipt": "synthetic-receipt"}),
            (work, {"content": "  Synthetic scratchpad\n "}),
            (work, {"content": "Synthetic", "tag": "explicit-tag"}),
            # Literal strings must reach the receipt-aware bounded facade.
            # FastMCP's default pre-parser would turn these into arrays/null.
            (daily, {"content": "Synthetic", "keywords": '["原始,短语"]'}),
            (daily, {"content": "Synthetic", "kind": "null", "emotion": "null"}),
            (daily, {"content": "Synthetic", "keywords": '[' + ' ' * 32768 + ']'}),
        ]
        for tool, arguments in accepted:
            original = deepcopy(arguments)
            Draft202012Validator(tool.parameters).validate(arguments)
            # Direct registered run intentionally isolates signature+dispatch
            # from outer auth wrappers; it is not an auth or write-success test.
            result = await tool.run(arguments)
            assert result == {"synthetic_signature_probe": True}
            assert arguments == original
            for key, value in original.items():
                assert seen[-1][key] == value, "entry_rewrote_author_arguments"
        assert seen[0]["module"] == "auto"
        assert seen[0]["importance"] == 50 and seen[0]["emotion"] == "other"
        assert seen[0]["content"] == "  Synthetic original\n "
        assert seen[7]["importance"] == 50 and seen[7]["emotion"] == "other", "receipt_defaults_changed"
        assert seen[8]["tag"] == "随手记"

        invalid = [
            (daily, {}), (daily, {"content": 7}), (daily, {"content": "Synthetic", "importance": True}),
            (daily, {"content": "Synthetic", "module": True}),
            (daily, {"content": "Synthetic", "kind": 7}),
            (daily, {"content": "Synthetic", "emotion": False}),
            (daily, {"content": "Synthetic", "keywords": [7]}),
            (daily, {"content": "Synthetic", "private_unknown_key": "PRIVATE_VALUE"}),
            (work, {}), (work, {"content": "Synthetic", "tag": ""}),
        ]
        before = len(seen)
        for tool, arguments in invalid:
            try:
                tool.fn_metadata.arg_model.model_validate(arguments)
            except ValidationError as error:
                assert "PRIVATE_VALUE" not in str(error), "validation_echoed_private_input"
            else:
                raise AssertionError("invalid_signature_was_accepted")
        assert len(seen) == before
        assert not network_attempts
        return {"decision": "PASS", "accepted_signature_cases": len(accepted),
                "rejected_signature_cases": len(invalid), "network_attempts": 0,
                "registered_tools": len(manager._tools), "manager_listed_tools": len(manager.list_tools()),
                "live_instance_used": False,
                "authorization_or_persistence_tested": False}


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(probe()), ensure_ascii=False))
    else:
        unittest.main()
