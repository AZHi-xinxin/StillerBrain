"""Real registered MCP/native/compact paths with synthetic temp data only."""
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


async def probe():
    from mcp_server import server
    from runtime.execution_binding import ExecutionStore, canonical_hash
    from runtime.atlas_metadata import AtlasMetadataReader, AtlasScope
    from tests.test_memory_relations import seed_endpoints
    from tests.test_onboarding import ModuleOneOnboardingTests
    checks = []
    database = Path(os.environ["STBRAIN_DB_PATH"])
    scope = {"owner_id": server.OWNER_ID, "model_id": server.MODEL_ID}
    seed_endpoints(database)

    def check(condition, label):
        if not condition: raise AssertionError(label)
        checks.append(label)

    async def call(name, args):
        blocks, result = await server.mcp.call_tool(name, args)
        check(json.loads(blocks[0].text) == result, "structured-text:" + name)
        return result

    def non_relation_dump():
        with closing(sqlite3.connect(database)) as db:
            return [line for line in db.iterdump() if "memory_relation_" not in line]

    tools = server.mcp._tool_manager.list_tools()
    check(len(tools) == 52, "all legacy and work tools remain plus three relation tools")
    for name in ("attach_memory_relation", "detach_memory_relation", "read_memory_relations"):
        tool = server.mcp._tool_manager.get_tool(name)
        check(tool is not None and "execution_ref" in tool.parameters["properties"], "claim-aware:" + name)
    required = server.mcp._tool_manager.get_tool("attach_memory_relation").parameters["required"]
    check(set(required) == {"from_ref", "to_ref", "type"}, "minimal relation schema")
    args = {"from_ref": "emotion://e1@1", "to_ref": "learning://l1@1", "type": "related_to"}
    denied = await call("attach_memory_relation", args)
    check(denied["decision"] == "reject" and denied["reason_code"] == "module_one_required", "actual activation gate")
    empty = await call("read_memory_relations", {"target_ref": args["from_ref"]})
    check(empty["relations"] == [], "explicit readonly before activation")
    fixture = ModuleOneOnboardingTests()
    fixture.database, fixture.store, fixture.owner, fixture.model = database, server.onboarding, server.OWNER_ID, server.MODEL_ID
    fixture.bootstrap_live()
    before = non_relation_dump()
    for secret in (server.MCP_TOKEN, server.WAKE_SECRET):
        rejected = await call("attach_memory_relation", {**args, "type": "custom", "label": secret})
        check(rejected["decision"] == "reject" and secret not in json.dumps(rejected), "configured secret never persists/echoes")
    for change in ({"owner_id": "foreign"}, {"model_id": "foreign"}, {"to_ref": "work://synthetic@1"},
                   {"type": "invented"}, {"type": "custom"}, {"type": 3}, {"execution_ref": "invalid-synthetic"}):
        invalid = await call("attach_memory_relation", {**args, **change})
        check(invalid.get("decision") == "reject", "invalid public field rejected:" + next(iter(change)))
    first = await call("attach_memory_relation", {**args, "request_id": "a" * 32})
    replay = await call("attach_memory_relation", {**args, "request_id": "a" * 32})
    check(first["edge_ref"] == replay["edge_ref"] and not replay["state_changed"], "native direct idempotency")
    read = await call("read_memory_relations", {"target_ref": args["from_ref"]})
    check(read["relations"][0]["target_title"] == "Synthetic learning title", "one hop target title")
    check("PRIVATE_" not in json.dumps(read), "no body/summary projection")
    graph = json.loads(AtlasMetadataReader(AtlasScope(database, **scope, id_key=b"A" * 32)).read())
    check(len(graph["edges"]) == 1, "real relation appears as anonymous edge")
    check("Synthetic learning title" not in json.dumps(graph), "atlas excludes titles")
    detached = await call("detach_memory_relation", {"edge_ref": first["edge_ref"]})
    replay = await call("attach_memory_relation", {**args, "request_id": "a" * 32})
    check(not replay["active"] and replay["edge_ref"] == detached["edge_ref"], "old replay cannot undo detach")
    check(non_relation_dump() == before, "relation operations do not rewrite existing business tables")
    directory = await call("stbrain_tools", {"category": "relations"})
    check("attach_memory_relation" in json.dumps(directory), "compact relation discovery")
    compact = await call("stbrain_manage", {"action": "attach_memory_relation", "arguments": {
        "from_ref": "learning://l1@1", "to_ref": "plan://p1@1", "type": "causes"}})
    check(compact["decision"] == "attached", "compact direct relation writes")
    entries = [{"canonical_name": t.name, "schema_hash": canonical_hash(t.parameters)} for t in tools]
    advertised = {"contract": "advertised-tools/1", "catalog_complete": True, "catalog_hash": canonical_hash(entries), "entries": entries}
    wake = server.onboarding.issue_wake(**scope, host_id="synthetic-host", thread_id="synthetic-thread", source_kind="human_message", source_event_id="synthetic-relation-context")
    with patch.object(server.memory_relation_service, "read", side_effect=AssertionError("automatic relation read forbidden")), \
         patch.object(server.work_memory_service, "recall", side_effect=AssertionError("automatic work read forbidden")):
        prepared = server.onboarding.build_pre_generation_context(**scope, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
            source_digest="synthetic-source", host_contract_digest="synthetic-host", advertised_tools=advertised)
    check(prepared.get("decision") == "context_prepared", "ordinary context does not recursively read relations or work")
    server.onboarding.confirm_context_injected(**scope, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"], context_hash=prepared["context_hash"])
    registry = ExecutionStore(database, deployment_epoch=os.environ["STBRAIN_EXECUTION_EPOCH"], capability_secret=server.WAKE_SECRET)

    async def bound(name, arguments, suffix, compact=False):
        public = "stbrain_manage" if compact else name
        issued = registry.issue_batch(**scope, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
            batch_id="synthetic-relations-batch-" + suffix, revision=1, calls=[{"call_id": "synthetic-call-" + suffix,
                "canonical_tool": name, "advertised_name": public, "schema_hash": canonical_hash(server.mcp._tool_manager.get_tool(public).parameters),
                "catalog_hash": advertised["catalog_hash"], "arguments_hash": canonical_hash(arguments)}])
        lease = issued["executions"][0]["execution_ref"]
        payload = {"action": name, "arguments": arguments} if compact else arguments
        result = await call(public, {**payload, "execution_ref": lease})
        replay = await call(public, {**payload, "execution_ref": lease})
        check(replay.get("decision") == "reject", "claimed lease cannot replay:" + suffix)
        return result

    created = await bound("attach_memory_relation", {**args, "type": "custom", "label": "synthetic authored label"}, "native")
    check(created["decision"] == "attached", "actual native gateway bound relation")
    read = await bound("read_memory_relations", {"target_ref": args["from_ref"]}, "read", compact=True)
    check(read["relations"][0]["edge_ref"] == created["edge_ref"], "actual compact gateway one-hop read")
    removed = await bound("detach_memory_relation", {"edge_ref": created["edge_ref"]}, "detach", compact=True)
    check(removed["decision"] == "detached", "actual compact gateway detach")
    check("PRIVATE_" not in json.dumps(read), "gateway output still no body")
    return {"decision": "PASS", "checks": checks, "check_count": len(checks), "network_attempts": 0,
            "real_memory_accessed": False, "model_called": False}


async def no_network_probe():
    attempts = []
    def deny(*args, **kwargs):
        attempts.append(True); raise AssertionError("network forbidden")
    with ExitStack() as guard:
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto", "socket.create_connection", "socket.getaddrinfo"):
            guard.enter_context(patch(name, side_effect=deny))
        result = await probe()
    if attempts: raise AssertionError("network attempt swallowed")
    return result


class MemoryRelationTransportTests(unittest.TestCase):
    def test_synthetic_real_mcp_compact_claims_and_no_auto_recall(self):
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PATHEXT", "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="relations-transport-synthetic-") as scratch:
            env.update({"STBRAIN_ACCESS_PROFILE": "simple-memory-v1", "STBRAIN_MCP_TOKEN": "synthetic-token-000000000000000000000000",
                "STBRAIN_WAKE_SECRET": "synthetic-wake-00000000000000000000000", "STBRAIN_OWNER_ID": "synthetic-owner", "STBRAIN_MODEL_ID": "synthetic-model",
                "STBRAIN_DB_PATH": str(Path(scratch) / "main.db"), "STBRAIN_LEARNING_IDEA_DB_PATH": str(Path(scratch) / "ideas.db"),
                "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(Path(scratch) / "vault.db"), "STBRAIN_REQUIRE_EXECUTION_BINDING": "1",
                "STBRAIN_EXECUTION_EPOCH": "synthetic-relations-epoch", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
            process = subprocess.run([sys.executable, "-B", "-m", "mcp_server.tests.test_memory_relation_transport", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(0, process.returncode, process.stderr)
        proof = json.loads(process.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"]); self.assertGreaterEqual(proof["check_count"], 45)
        self.assertEqual(0, proof["network_attempts"])
        print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]: print(json.dumps(asyncio.run(no_network_probe()), ensure_ascii=False))
    else: unittest.main()
