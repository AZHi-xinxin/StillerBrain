"""Real registered MCP dispatch; only synthetic data and no application network."""
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


async def probe():
    from mcp_server import server
    from runtime.execution_binding import ExecutionStore, canonical_hash
    from tests.test_onboarding import ModuleOneOnboardingTests

    checks = []
    database = Path(os.environ["STBRAIN_DB_PATH"])
    scope = {"owner_id": server.OWNER_ID, "model_id": server.MODEL_ID}

    def check(condition, label):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    async def call(name, args):
        blocks, result = await server.mcp.call_tool(name, args)
        check(json.loads(blocks[0].text) == result, "structured-text:" + name)
        return result

    def count():
        with closing(sqlite3.connect(database)) as db:
            return db.execute("SELECT count(*) FROM work_memory_versions").fetchone()[0]

    def non_work_dump():
        with closing(sqlite3.connect(database)) as db:
            return [line for line in db.iterdump() if "work_memory_" not in line]

    tools = server.mcp._tool_manager.list_tools()
    check(len(tools) == 52, "work and relation tools registered without replacing existing tools")
    required = server.mcp._tool_manager.get_tool("remember_work_memory").parameters.get("required", [])
    check(set(required) == {"content", "tag"}, "minimal public write schema")
    empty = await call("recall_work_memory", {})
    check(empty.get("decision") == "recalled" and empty["results"] == [], "authenticated empty read before activation")
    denied = await call("remember_work_memory", {"content": "Synthetic body", "tag": "synthetic"})
    check(denied.get("decision") == "reject" and denied.get("state_changed") is False and count() == 0,
          "unactivated ordinary write rejected")
    fixture = ModuleOneOnboardingTests()
    fixture.database, fixture.store = database, server.onboarding
    fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
    fixture.bootstrap_live()
    before = non_work_dump()
    for secret in (server.MCP_TOKEN, server.WAKE_SECRET):
        rejected = await call("remember_work_memory", {"content": "prefix " + secret + " suffix", "tag": "synthetic"})
        check(rejected.get("decision") == "reject" and count() == 0, "configured credential cannot enter work memory")
    original = "  SYNTHETIC_WORK_ONLY_SENTINEL\n原文必须逐字保留。  "
    tag = "synthetic-work-tag-%_"
    args = {"content": original, "tag": tag, "request_id": "1" * 32}
    created = await call("remember_work_memory", args)
    check(created.get("decision") == "stored" and created["version"] == 1, "ordinary explicit create")
    replay = await call("remember_work_memory", args)
    check(replay["target_ref"] == created["target_ref"] and replay["state_changed"] is False and count() == 1,
          "explicit uncertain retry does not duplicate")
    collision = await call("remember_work_memory", {**args, "content": "different synthetic body"})
    check(collision.get("decision") == "reject" and count() == 1, "idempotency collision rejected")
    for extra in ({"owner_id": "other"}, {"execution_ref": "invalid-synthetic-ref"},
                  {"content": 12}, {"tag": ""}):
        bad = await call("remember_work_memory", {**args, **extra})
        check(bad.get("decision") == "reject" and count() == 1, "invalid public input cannot persist:" + next(iter(extra)))
    explicit = await call("recall_work_memory", {"query": "%_"})
    check(explicit["results"][0]["content"] == original, "literal tag lookup returns exact original")
    revised = await call("revise_work_memory", {"target_ref": created["target_ref"], "tag": "synthetic-new-tag"})
    check(revised.get("decision") == "revised" and revised["version"] == 2, "tag-only edit appends version")
    stale = await call("revise_work_memory", {"target_ref": created["target_ref"], "content": "no overwrite"})
    check(stale.get("decision") == "reject" and count() == 2, "stale edit rejected")
    old = await call("recall_work_memory", {"target_ref": created["target_ref"]})
    check(old["results"][0]["content"] == original and old["results"][0]["tag"] == tag, "old version retained")
    current = await call("recall_work_memory", {"target_ref": revised["target_ref"]})
    check(current["results"][0]["content"] == original, "tag change never rewrites body")
    check(non_work_dump() == before, "direct work operations leave every legacy table unchanged")
    directory = await call("stbrain_tools", {"category": "work"})
    check("remember_work_memory" in json.dumps(directory), "compact discovery advertises work category")
    compact = await call("stbrain_manage", {"action": "recall_work_memory", "arguments": {"query": "synthetic-new-tag"}})
    check(compact["results"][0]["content"] == original, "compact explicit lookup reaches work store")

    entries = [{"canonical_name": tool.name, "schema_hash": canonical_hash(tool.parameters)} for tool in tools]
    advertised = {"contract": "advertised-tools/1", "catalog_complete": True,
                  "catalog_hash": canonical_hash(entries), "entries": entries}
    wake = server.onboarding.issue_wake(**scope, host_id="synthetic-host", thread_id="synthetic-thread",
        source_kind="human_message", source_event_id="synthetic-work-context")
    # Automatic recall is forbidden even when a tag is an exact current query.
    with patch.object(server.work_memory_service, "recall", side_effect=AssertionError("automatic_work_read")):
        prepared = server.onboarding.build_pre_generation_context(**scope, wake_id=wake["wake_id"],
            wake_capability=wake["wake_capability"], source_digest="synthetic-source", host_contract_digest="synthetic-host",
            advertised_tools=advertised, source_frame={"query_text": "synthetic-new-tag", "lineage_stable": False,
                                                       "prior_assistant_present": False, "capture_items": []})
    check(prepared.get("decision") == "context_prepared", "ordinary gateway context still prepares")
    with closing(sqlite3.connect(database)) as db:
        context = db.execute("SELECT stable_json,dynamic_json FROM brain_context_snapshots WHERE wake_id=?",
                             (wake["wake_id"],)).fetchone()
    check("SYNTHETIC_WORK_ONLY_SENTINEL" not in json.dumps([prepared, context]), "work body never automatically injected")
    check(created["target_ref"] not in json.dumps([prepared, context]), "work reference never automatically recalled")
    server.onboarding.confirm_context_injected(**scope, wake_id=wake["wake_id"],
        wake_capability=wake["wake_capability"], context_hash=prepared["context_hash"])
    registry = ExecutionStore(database, deployment_epoch=os.environ["STBRAIN_EXECUTION_EPOCH"],
                              capability_secret=server.WAKE_SECRET)

    async def bound(name, arguments, *, compact=False, suffix):
        public = "stbrain_manage" if compact else name
        issued = registry.issue_batch(**scope, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
            batch_id="synthetic-work-batch-" + suffix, revision=1, calls=[{"call_id": "synthetic-work-call-" + suffix,
                "advertised_name": public, "canonical_tool": name,
                "schema_hash": canonical_hash(server.mcp._tool_manager.get_tool(public).parameters),
                "catalog_hash": advertised["catalog_hash"], "arguments_hash": canonical_hash(arguments)}])
        lease = issued["executions"][0]["execution_ref"]
        payload = ({"action": name, "arguments": arguments} if compact else arguments)
        result = await call(public, {**payload, "execution_ref": lease})
        again = await call(public, {**payload, "execution_ref": lease})
        check(again.get("decision") == "reject", "claimed lease cannot replay:" + suffix)
        return result

    saved = await bound("remember_work_memory", {"content": "Synthetic gateway work body", "tag": "bound"}, suffix="native")
    check(saved.get("decision") == "stored", "native gateway work claim accepted")
    saved2 = await bound("remember_work_memory", {"content": "Synthetic compact work body", "tag": "bound-compact"}, compact=True, suffix="compact")
    check(saved2.get("decision") == "stored", "compact canonical gateway work claim accepted")
    bad_request = await bound("remember_work_memory", {"content": "Do not store", "tag": "bound", "request_id": "2" * 32}, suffix="request")
    check(bad_request.get("decision") == "reject" and count() == 4, "gateway cannot choose direct idempotency key")
    for arguments in ({"target_ref": saved["target_ref"], "lifecycle": "deleted"},
                      {"target_ref": saved["target_ref"], "lifecycle": True},
                      {"target_ref": saved["target_ref"], "lifecycle": "retired", "bulk": True}):
        invalid = await call("revise_work_memory", arguments)
        check(invalid.get("decision") == "reject" and count() == 4, "strict single-item lifecycle schema")
    invalid = await call("recall_work_memory", {"include_retired": "true"})
    check(invalid.get("decision") == "reject", "strict include_retired boolean")
    retired = await bound("revise_work_memory", {"target_ref": saved["target_ref"], "lifecycle": "retired"}, suffix="retire")
    check(retired.get("lifecycle") == "retired" and retired["version"] == 2, "bound retirement appends version")
    hidden = await call("recall_work_memory", {"target_ref": saved["target_ref"]})
    check(hidden.get("reason_code") == "work_memory_not_found", "retirement hides even old exact references")
    explicit = await call("stbrain_manage", {"action": "recall_work_memory", "arguments": {
        "target_ref": saved["target_ref"], "include_retired": True}})
    check(explicit["results"][0]["current_target_ref"] == retired["target_ref"], "compact explicit retirement lookup")
    restored = await bound("revise_work_memory", {"target_ref": retired["target_ref"], "lifecycle": "active"},
                           compact=True, suffix="restore")
    check(restored.get("lifecycle") == "active" and restored["version"] == 3, "compact bound exact restore")
    visible = await call("recall_work_memory", {"target_ref": restored["target_ref"]})
    check(visible["results"][0]["content"] == "Synthetic gateway work body", "restored original preserved")
    return {"decision": "PASS", "checks": checks, "check_count": len(checks), "network_attempts": 0,
            "real_memory_accessed": False, "model_called": False}


async def no_network_probe():
    attempts = []
    def deny(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("network_forbidden_in_work_memory_test")
    with ExitStack() as guard:
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
                     "socket.create_connection", "socket.getaddrinfo"):
            guard.enter_context(patch(name, side_effect=deny))
        result = await probe()
    if attempts:
        raise AssertionError("network_attempt_swallowed")
    return result


class WorkMemoryTransportTests(unittest.TestCase):
    def test_synthetic_real_mcp_guards_compact_dispatch_and_never_auto_recall(self):
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
                   "LOCALAPPDATA", "APPDATA", "PATHEXT", "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="work-transport-synthetic-") as scratch:
            env.update({"STBRAIN_ACCESS_PROFILE": "simple-memory-v1", "STBRAIN_MCP_TOKEN": "synthetic-token-000000000000000000000000",
                "STBRAIN_WAKE_SECRET": "synthetic-wake-00000000000000000000000", "STBRAIN_OWNER_ID": "synthetic-owner",
                "STBRAIN_MODEL_ID": "synthetic-model", "STBRAIN_DB_PATH": str(Path(scratch) / "main.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(Path(scratch) / "ideas.db"),
                "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(Path(scratch) / "vault.db"),
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "1", "STBRAIN_EXECUTION_EPOCH": "synthetic-work-epoch",
                "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
            completed = subprocess.run([sys.executable, "-B", "-m", "mcp_server.tests.test_work_memory_transport", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertGreaterEqual(proof["check_count"], 45)
        self.assertEqual(0, proof["network_attempts"])
        print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(no_network_probe()), ensure_ascii=False))
    else:
        unittest.main()
