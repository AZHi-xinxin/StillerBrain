"""Real FastMCP argument/result conversion with no frontend sandbox tool.

The child process gets only new synthetic DB paths and test credentials. There
is no network/model call, private database, user memory, or tool-output file.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


async def synthetic_probe() -> dict:
    from mcp_server import server
    from mcp_server.tests.test_compact_open import CompactOpenStateTests
    from mcp_server.tests.test_planning_service import calm, plan_content

    # asyncio's own local event loop is already created before the fixture
    # disables sockets. No network is allowed during any tool call below.
    fixture = CompactOpenStateTests()
    fixture.setUp()
    try:
        fixture.service = server.service
        fixture.onboarding = server.service.onboarding
        fixture.database = Path(os.environ["STBRAIN_DB_PATH"])
        fixture.bootstrap_live()
        fixture.wake("native-compact-tool-round")
        names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
        assert "workspace_shell" not in names
        assert not any("sandbox" in name for name in names)

        async def call(name: str, arguments: dict) -> dict:
            result = await server.mcp.call_tool(name, arguments)
            assert isinstance(result, tuple) and len(result) == 2
            text_blocks, structured = result
            assert isinstance(structured, dict)
            assert len(text_blocks) == 1 and text_blocks[0].type == "text"
            # A frontend without structuredContent still receives one complete
            # JSON result. Parse that exact object, never a recursive key search.
            plain = json.loads(text_blocks[0].text)
            assert plain == structured
            return structured

        opened = await call("stbrain_open", {})
        assert opened["view"] == "summary"
        assert opened["contract_version"] == "public-tools/20"
        assert opened["write_context_available"] is True
        assert opened["continuation"] is None
        assert opened["review_material_presented"] is False
        ref = opened["write_context_ref"]
        assert isinstance(ref, str) and ref and ref != "$.write_context_ref"
        for name in ("emotional_memory", "learning_memory", "tool_guidance", "planning_memory",
                     "self_governance_profile", "injection_control", "hallucination_vault",
                     "shared_person_authoring"):
            assert name in opened
        serialized = json.dumps(opened, ensure_ascii=False)
        size = len(serialized.encode("utf-8"))
        assert size < 8192, "summary exceeds the isolated small-client budget"
        for forbidden in ('"wake_capability"', '"wake_id"', '"grant_ref"', '"context_hash"',
                          "$.write_context_ref", '"pending_changes"', '"current_action_contract"'):
            assert forbidden not in serialized

        arguments = {
            **plan_content("离线无沙箱协议验收"),
            "write_context_ref": ref,
            "expected_planning_version": opened["planning_memory"]["planning_row_version"],
            "reason": "我选择保存这条纯合成的协议测试计划。",
            "calm_check": calm(), "ai_confirmation": True,
            "idempotency_key": "synthetic-native-compact-create",
        }
        created = await call("remember_planning_memory", arguments)
        assert created["decision"] == "stored"
        assert created["state_changed"] is True
        assert created["active_plan_changed"] is True
        assert created["candidate_created"] is False
        assert created["review_performed"] is False
        assert created["review_requires_later_wake"] is False

        replay = await call("remember_planning_memory", arguments)
        assert replay["idempotent_replay"] is True and replay["state_changed"] is False

        # CAS remains a separate error rather than being mislabeled as no open.
        conflict = await call("remember_planning_memory", {
            **arguments, "idempotency_key": "synthetic-stale-version",
        })
        assert conflict["decision"] == "reject"
        assert conflict["reason_codes"] == ["planning_row_version_conflict"]
        assert conflict["state_changed"] is False

        await call("stbrain_open", {"view": "manual", "module": "planning_memory"})
        summary_again = await call("stbrain_open", {})
        assert summary_again["write_context_ref"] == ref
        assert summary_again["review_material_presented"] is False
        return {
            "decision": "PASS", "tool_count": len(names), "sandbox_tools": 0,
            "summary_utf8_bytes": size, "all_modules_summarized": True,
            "structured_and_text_equal": True, "native_open_to_active_plan": True,
            "active_plan_changed": True, "version_conflict_preserved": True,
            "idempotency_replay_preserved": True,
            "private_database_accessed": False,
        }
    finally:
        fixture.doCleanups()


class CompactOpenNativeTransportTests(unittest.TestCase):
    def test_native_mcp_open_to_planning_without_sandbox(self) -> None:
        try:
            import mcp  # noqa: F401
        except ImportError:
            self.skipTest("FastMCP dependency not installed in this interpreter")
        environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith(("STBRAIN_", "OMBRE_", "OPENAI_", "DEEPSEEK_", "E32_"))
            and key.upper() not in {"PYTHONPATH", "PYTHONSTARTUP"}
        }
        with tempfile.TemporaryDirectory(prefix="compact-native-synthetic-") as scratch:
            environment.update({
                "STBRAIN_MCP_TOKEN": "synthetic-native-mcp-token-at-least-thirty-two-characters",
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "0",
                "STBRAIN_WAKE_SECRET": "synthetic-native-wake-secret-at-least-thirty-two-characters",
                "STBRAIN_OWNER_ID": "owner:compact-native-synthetic",
                "STBRAIN_MODEL_ID": "model:compact-native-synthetic",
                "STBRAIN_DB_PATH": str(Path(scratch) / "brain.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(Path(scratch) / "ideas.db"),
                "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(Path(scratch) / "vault.db"),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            })
            result = subprocess.run(
                [sys.executable, "-B", "-m", "mcp_server.tests.test_compact_open_transport", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=environment,
                capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertEqual(0, proof["sandbox_tools"])
        self.assertLess(proof["summary_utf8_bytes"], 8192)


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(synthetic_probe())))
    else:
        unittest.main()
