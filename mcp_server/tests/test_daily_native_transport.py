"""Real registered server tools, synthetic authenticated host leases and stores.

No network, phone, real user memory or live configuration is accessed. This
tests MCP conversion/dispatch, not HTTP authorization or a real model.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


async def probe():
    from mcp_server import server
    from mcp_server.tests.test_compact_open import CompactOpenStateTests
    from runtime.execution_binding import ExecutionStore, canonical_hash
    fixture = CompactOpenStateTests()
    fixture.setUp()
    try:
        fixture.service, fixture.onboarding = server.service, server.onboarding
        fixture.database = Path(os.environ["STBRAIN_DB_PATH"])
        fixture.bootstrap_live()
        common = {"owner_id": server.OWNER_ID, "model_id": server.MODEL_ID}
        tools = server.mcp._tool_manager.list_tools()
        assert len(tools) == 44
        tool = server.mcp._tool_manager.get_tool("remember_memory")
        assert tool.parameters["required"] == ["module", "content"]
        assert "execution_ref" in tool.parameters["properties"]
        entries = [{"canonical_name": t.name, "schema_hash": canonical_hash(t.parameters)} for t in tools]
        catalog = {"contract": "advertised-tools/1", "catalog_complete": True,
                   "catalog_hash": canonical_hash(entries), "entries": entries}
        wake = server.onboarding.issue_wake(**common, host_id="synthetic-host", thread_id="synthetic-thread",
            source_kind="human_message", source_event_id="synthetic-daily-native")
        prepared = server.onboarding.build_pre_generation_context(**common, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], source_digest="synthetic", host_contract_digest="synthetic",
            advertised_tools=catalog)
        server.onboarding.confirm_context_injected(**common, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], context_hash=prepared["context_hash"])
        registry = ExecutionStore(fixture.database, deployment_epoch=os.environ["STBRAIN_EXECUTION_EPOCH"],
                                  capability_secret=server.WAKE_SECRET)
        async def call(name, args):
            blocks, result = await server.mcp.call_tool(name, args)
            assert json.loads(blocks[0].text) == result
            return result
        async def bound_call(name, args, label):
            current_tool = server.mcp._tool_manager.get_tool(name)
            issued = registry.issue_batch(**common, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
                batch_id="synthetic-" + label, revision=1, calls=[{
                    "call_id": "synthetic-" + label, "advertised_name": name, "canonical_tool": name,
                    "schema_hash": canonical_hash(current_tool.parameters), "catalog_hash": catalog["catalog_hash"],
                    "arguments_hash": canonical_hash(args)}])
            return await call(name, {**args, "execution_ref": issued["executions"][0]["execution_ref"]})
        help_result = await call("stbrain_help", {})
        assert help_result["state_changed"] is False
        missing = await call("remember_memory", {"module": "planning_memory", "content": "synthetic"})
        assert missing["decision"] == "reject"
        results = []
        for i, module in enumerate(("emotional_memory", "learning_memory", "planning_memory")):
            args = {"module": module, "content": "  Synthetic native memory\nkept exactly.  "}
            issued = registry.issue_batch(**common, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
                batch_id=f"synthetic-batch-{i}", revision=1, calls=[{
                    "call_id": f"synthetic-call-{i}", "advertised_name": "remember_memory", "canonical_tool": "remember_memory",
                    "schema_hash": canonical_hash(tool.parameters), "catalog_hash": catalog["catalog_hash"],
                    "arguments_hash": canonical_hash(args)}])
            args["execution_ref"] = issued["executions"][0]["execution_ref"]
            result = await call("remember_memory", args)
            assert result["decision"] == "stored", result
            assert result["count"] == 1 and result["state_changed"] is True
            assert "write_context_ref" not in result
            replay = await call("remember_memory", args)
            assert replay["decision"] == "reject"
            before = server.daily_service.services[module].status()["row_version"]
            unsafe_args = {"module": module, "content": "Do not persist " + args["execution_ref"]}
            secret_call = registry.issue_batch(**common, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
                batch_id=f"synthetic-secret-batch-{i}", revision=1, calls=[{
                    "call_id": f"synthetic-secret-call-{i}", "advertised_name": "remember_memory", "canonical_tool": "remember_memory",
                    "schema_hash": canonical_hash(tool.parameters), "catalog_hash": catalog["catalog_hash"],
                    "arguments_hash": canonical_hash(unsafe_args)}])
            unsafe_args["execution_ref"] = secret_call["executions"][0]["execution_ref"]
            unsafe_result = await call("remember_memory", unsafe_args)
            assert unsafe_result["decision"] == "reject"
            assert unsafe_result["state_changed"] is False
            assert before == server.daily_service.services[module].status()["row_version"]
            revised = await bound_call("revise_memory", {"target_ref": result["ref"],
                "changes": {"summary": "Synthetic amended summary"}}, f"revise-{i}")
            assert revised["decision"] == "revised", revised
            assert revised["version"] == 2
            stale = await bound_call("revise_memory", {"target_ref": result["ref"],
                "changes": {"summary": "Must not overwrite v2"}}, f"stale-revise-{i}")
            assert stale["decision"] == "reject" and stale["state_changed"] is False, stale
            forbidden = await bound_call("revise_memory", {"target_ref": revised["ref"],
                "changes": {"owner_id": "synthetic-other-owner"}}, f"forbidden-revise-{i}")
            assert forbidden["decision"] == "reject" and forbidden["state_changed"] is False, forbidden
            if module in {"emotional_memory", "learning_memory"}:
                # Confidence is now an author-editable field, not a technical
                # namespace/receipt override. It still has normal range checks.
                invalid_confidence = await bound_call("revise_memory", {"target_ref": revised["ref"],
                    "changes": {"confidence": 101}}, f"invalid-confidence-{i}")
                assert invalid_confidence["decision"] == "reject" and invalid_confidence["state_changed"] is False, invalid_confidence
                confidence = await bound_call("revise_memory", {"target_ref": revised["ref"],
                    "changes": {"confidence": 100}}, f"author-confidence-{i}")
                assert confidence["decision"] == "revised" and confidence["version"] == 3, confidence
            if module == "planning_memory":
                read = await call("recall_planning_memory", {"plan_ref": revised["ref"]})
                target = read["plans"][0]
                sequence = target["event_seq"]
                paused = await bound_call("advance_plan", {"target_ref": revised["ref"],
                    "expected_event_seq": sequence, "event_type": "pause", "note": "Synthetic pause"}, "pause-plan")
                assert paused["decision"] == "event_recorded", paused
                stale_event = await bound_call("advance_plan", {"target_ref": revised["ref"],
                    "expected_event_seq": sequence, "event_type": "resume", "note": "Stale resume"}, "stale-event")
                assert stale_event["decision"] == "reject" and stale_event["state_changed"] is False, stale_event
            results.append(module)
        return {"decision": "PASS", "modules": results, "tool_count": 44,
                "single_native_call_per_memory": True, "replays_rejected": True,
                "single_revision_per_module": True, "stale_targets_rejected": True,
                "plan_event_cas_verified": True,
                "real_mcp_conversion": True, "live_data_accessed": False, "network_calls": 0}
    finally:
        fixture.doCleanups()


class DailyNativeTests(unittest.TestCase):
    def test_registered_daily_tools_with_actual_execution_registry(self):
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("STBRAIN_", "OMBRE_", "OPENAI_", "DEEPSEEK_", "E32_"))
               and k.upper() not in {"PYTHONPATH", "PYTHONSTARTUP"}}
        with tempfile.TemporaryDirectory(prefix="daily-native-synthetic-") as scratch:
            env.update({"STBRAIN_MCP_TOKEN": "synthetic-token-000000000000000000000000",
                "STBRAIN_WAKE_SECRET": "synthetic-wake-00000000000000000000000",
                "STBRAIN_OWNER_ID": "synthetic-owner", "STBRAIN_MODEL_ID": "synthetic-model",
                "STBRAIN_DB_PATH": str(Path(scratch) / "main.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(Path(scratch) / "ideas.db"),
                "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(Path(scratch) / "vault.db"),
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "1", "STBRAIN_EXECUTION_EPOCH": "synthetic-epoch",
                "PYTHONIOENCODING": "utf-8"})
            result = subprocess.run([sys.executable, "-B", "-m", "mcp_server.tests.test_daily_native_transport", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env,
                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertEqual(3, len(proof["modules"]))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(probe())))
    else:
        unittest.main()
