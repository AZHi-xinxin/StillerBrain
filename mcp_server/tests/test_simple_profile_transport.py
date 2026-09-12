"""Isolated real-MCP dispatch with a genuinely activated synthetic module one.

This exercises registered schemas/conversion and actual execution leases, not
HTTP authentication, a network listener, a real user brain, or a model call.
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


async def probe():
    from mcp_server import server
    from runtime.execution_binding import ExecutionStore, canonical_hash
    from tests.test_onboarding import ModuleOneOnboardingTests

    common = {"owner_id": server.OWNER_ID, "model_id": server.MODEL_ID}
    database = Path(os.environ["STBRAIN_DB_PATH"])
    checks = []

    def check(condition, label, result=None):
        if not condition:
            # Every input/store is synthetic, but still omit arbitrary content and refs.
            diagnostic = ({key: result.get(key) for key in
                           ("decision", "reason_code", "reason_codes", "version", "state_changed")}
                          if isinstance(result, dict) else type(result).__name__)
            raise AssertionError(f"{label}: {diagnostic}")
        checks.append(label)

    async def call(name, arguments):
        blocks, result = await server.mcp.call_tool(name, arguments)
        check(json.loads(blocks[0].text) == result, "MCP structured/text conversion:" + name)
        return result

    def scalar(query, values=()):
        with closing(sqlite3.connect(database)) as connection:
            return connection.execute(query, values).fetchone()[0]

    def text_values(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from text_values(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from text_values(item)
        elif isinstance(value, str):
            yield value
            if value.startswith(("{", "[")):
                try:
                    decoded = json.loads(value)
                except ValueError:
                    return
                yield from text_values(decoded)

    tools = server.mcp._tool_manager.list_tools()
    check(len(tools) == 44, "simple profile publishes 44 tools")
    check(server.SIMPLE_MEMORY_ACCESS is True, "simple profile actually active")
    required = server.mcp._tool_manager.get_tool("remember_tool_guidance").parameters.get("required", [])
    check(set(required) == {"tool_name", "purpose"}, "minimal tool card public schema")
    empty = await call("recall_tool_guidance", {})
    check(empty.get("decision") == "directory" and empty.get("results") == [], "empty directory before any wake", empty)

    for module in ("emotional_memory", "learning_memory", "planning_memory"):
        denied = await call("remember_memory", {"module": module, "content": "Synthetic pre-activation write."})
        check(denied.get("reason_code") == "module_one_required" and denied.get("state_changed") is False,
              "unactivated ordinary write rejected:" + module, denied)
    check(scalar("SELECT count(*) FROM brain_wake_sessions") == 0,
          "denied ordinary calls create no real wakes")
    check(scalar("SELECT count(*) FROM self_model_revisions") == 0,
          "denied ordinary calls create no self revision")

    # Reuse the real three-wake submit/review/activate fixture in this fresh DB;
    # do not fake an unlock row or bypass the public ordinary write gate.
    fixture = ModuleOneOnboardingTests()
    fixture.database, fixture.store = database, server.onboarding
    fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
    fixture.bootstrap_live()
    check(server.onboarding.state(**common)["module_one_unlocked"] is True,
          "synthetic module one genuinely activated")
    bootstrap_wakes = scalar("SELECT count(*) FROM brain_wake_sessions")
    check(bootstrap_wakes == 3, "synthetic activation uses three real wake boundaries")
    with closing(sqlite3.connect(database)) as connection:
        original_core = connection.execute("SELECT * FROM self_model_revisions ORDER BY revision_id").fetchall()

    records = {}
    for module in ("emotional_memory", "learning_memory", "planning_memory"):
        original = "  Synthetic " + module + " original\nkept exactly.  "
        stored = await call("remember_memory", {"module": module, "content": original})
        check(stored.get("decision") == "stored" and stored.get("version") == 1,
              "activated ordinary create without another wake:" + module, stored)
        check("write_context_ref" not in stored, "opaque bookkeeping omitted:" + module)
        body_field = "current_understanding" if module == "learning_memory" else "original_text"
        changed_text = "  Synthetic " + module + " revised body\nversion two.  "
        changes = {body_field: changed_text}
        if module == "emotional_memory":
            changes.update(memory_type="shared_event", source_timestamp="2026-09-11T00:00:00+00:00")
        revised = await call("revise_memory", {"target_ref": stored["ref"],
                             "changes": changes, "reason": "Synthetic author correction"})
        check(revised.get("decision") == "revised" and revised.get("version") == 2,
              "activated ordinary revision without another wake:" + module, revised)
        if module == "emotional_memory":
            invalid_timestamp = await call("revise_memory", {"target_ref": revised["ref"],
                                            "changes": {"source_timestamp": "not-a-time"}})
            check(invalid_timestamp.get("decision") == "reject" and invalid_timestamp.get("state_changed") is False,
                  "emotional invalid timestamp rejected by runtime", invalid_timestamp)
        stale = await call("revise_memory", {"target_ref": stored["ref"],
                           "changes": {body_field: "Must not overwrite a newer version."}})
        check(stale.get("decision") == "reject" and stale.get("state_changed") is False,
              "stale content ref rejected:" + module, stale)
        if module == "emotional_memory":
            read = await call("recall_emotional_memory", {"memory_id": stored["id"], "include_originals": True})
        elif module == "learning_memory":
            read = await call("recall_learning_memory", {"target_ref": revised["ref"], "include_versions": True})
        else:
            read = await call("recall_planning_memory", {"plan_ref": revised["ref"], "include_history": True})
        check(changed_text in set(text_values(read)),
              "query returns exact revised body:" + module, read)
        records[module] = {"stored": stored, "revised": revised, "read": read,
                           "original": original, "changed": changed_text}

    # Inspect only these three fresh synthetic DBs to independently prove history.
    check(scalar("SELECT count(*) FROM brain_wake_sessions") == bootstrap_wakes,
          "ordinary calls create no additional injected wakes")
    check(scalar("SELECT count(*) FROM brain_onboarding_state WHERE module_one_status='complete'") == 1,
          "module one remains completed")
    for module, table, key in (("emotional_memory", "emotion_memory_versions", "memory_id"),
                               ("learning_memory", "learning_versions", "learning_id"),
                               ("planning_memory", "planning_versions", "plan_id")):
        with closing(sqlite3.connect(database)) as c:
            snapshots = c.execute(f"SELECT * FROM {table} WHERE {key}=? ORDER BY version",
                                  (records[module]["stored"]["id"],)).fetchall()
        check(records[module]["original"] in set(text_values(snapshots)),
              "original retained in earlier version:" + module)

    minimal = await call("remember_tool_guidance", {"tool_name": "中文最小服务", "purpose": "整理公开笔记。",
                         "reminder": "整理笔记时可以看看已有的分类。"})
    check(minimal.get("decision") == "stored", "exact three-field Chinese tool reminder saves", minimal)
    check(minimal["card"]["content"]["scenario_tags"] == [] and minimal["card"]["content"]["call_notes"] == "",
          "omitted technical details have empty defaults")
    tool = await call("remember_tool_guidance", {
        "tool_name": "家庭服务", "purpose": "到家时查看家中设备的状态。", "reminder": "到家了，可以看看设备状态。",
        "keywords": ["到家"], "scenario_tags": [], "operation_key": "synthetic_exact_operation",
        "call_notes": "synthetic_parameter_detail_only", "reason": "Synthetic tool reminder"})
    check(tool.get("decision") == "stored", "Chinese service card with empty tags", tool)
    card = tool["card"]
    check(card["content"].get("observed_schema_hash") is None, "unadvertised service observation stays null")
    revision = await call("revise_tool_guidance", {"card_id": card["card_id"], "expected_card_version": 1,
        "reminder": "到家后可以先查看设备的状态。", "reason": "Synthetic direct revision"})
    check(revision.get("submission_mode") == "direct_revision" and revision["card"]["version"] == 2,
          "tool ordinary edit directly appends", revision)
    directory = await call("recall_tool_guidance", {"view": "directory"})
    check(card["card_id"] in json.dumps(directory), "tool directory readable without open", directory)

    # Explicit old bookkeeping is not trusted as an authorization grant. In
    # simple mode it may be ignored/replaced, or rejected with zero state change.
    legacy = await call("remember_memory", {"module": "learning_memory", "content": "Synthetic legacy field test",
                                           "write_context_ref": "old-stale-synthetic-ref"})
    check(legacy.get("decision") in {"stored", "reject"}, "legacy generic binding handled explicitly", legacy)
    if legacy.get("decision") == "reject":
        check(legacy.get("state_changed") is False, "legacy binding rejection has no write", legacy)
    else:
        legacy_wake = scalar("SELECT wake_id FROM learning_versions WHERE learning_id=? AND version=1", (legacy["id"],))
        check(legacy_wake.startswith("ordinaryop_"), "legacy generic field grants no caller-chosen wake")
    dedicated = await call("remember_tool_guidance", {"tool_name": "LegacyService", "purpose": "A synthetic directory entry.",
                             "write_context_ref": "old-stale-synthetic-ref"})
    check(dedicated.get("decision") == "stored", "dedicated legacy binding replaced by authenticated operation", dedicated)

    plan = records["planning_memory"]["read"]["plans"][0]
    target_ref = records["planning_memory"]["revised"]["ref"]
    wrong = await call("advance_plan", {"plan_id": plan["plan_id"], "event_type": "pause", "note": "Synthetic wrong interface"})
    check(wrong.get("decision") == "reject" and wrong.get("state_changed") is False,
          "plan wrong argument shape rejected", wrong)
    check(bool(wrong.get("message")) and bool(wrong.get("next_action")), "plan error supplies current-interface guidance", wrong)
    paused = await call("advance_plan", {"target_ref": target_ref, "expected_event_seq": plan["event_seq"],
                        "event_type": "pause", "note": "Synthetic pause without external action"})
    check(paused.get("decision") == "event_recorded", "plan normal pause works", paused)
    bad_event = await call("advance_plan", {"target_ref": target_ref, "expected_event_seq": plan["event_seq"],
                           "event_type": "resume", "note": "Synthetic stale event"})
    check(bad_event.get("decision") == "reject" and bad_event.get("state_changed") is False,
          "plan event CAS remains", bad_event)

    for bad_ref in (None, "stexec_bad_synthetic", 1):
        before = server.learning_service.status()["row_version"]
        invalid = await call("remember_memory", {"module": "learning_memory", "content": "Must remain absent.",
                              "execution_ref": bad_ref})
        check(invalid.get("decision") == "reject" and invalid.get("state_changed") is False,
              "explicit invalid execution reference rejected:" + type(bad_ref).__name__, invalid)
        check(before == server.learning_service.status()["row_version"], "invalid lease causes no ordinary write")

    entries = [{"canonical_name": t.name, "schema_hash": canonical_hash(t.parameters)} for t in tools]
    advertised = {"contract": "advertised-tools/1", "catalog_complete": True,
                  "catalog_hash": canonical_hash(entries), "entries": entries}
    wake = server.onboarding.issue_wake(**common, host_id="synthetic-host", thread_id="synthetic-thread",
        source_kind="human_message", source_event_id="synthetic-simple-context")
    prepared = server.onboarding.build_pre_generation_context(**common, wake_id=wake["wake_id"],
        wake_capability=wake["wake_capability"], source_digest="synthetic-source", host_contract_digest="synthetic-host",
        advertised_tools=advertised, source_frame={"query_text": "到家了", "lineage_stable": False,
                                                   "prior_assistant_present": False, "capture_items": []})
    check(prepared.get("decision") == "context_prepared", "activated dynamic context prepared", prepared)
    with closing(sqlite3.connect(database)) as c:
        stable_json, dynamic_json = c.execute("SELECT stable_json,dynamic_json FROM brain_context_snapshots WHERE wake_id=?",
                                            (wake["wake_id"],)).fetchone()
    dynamic = json.loads(dynamic_json)
    check("active_identity_capsule" in json.loads(stable_json), "actual active self model remains in stable context")
    check("到家后可以先查看设备的状态。" in dynamic_json, "tool reminder surfaces after module one activation")
    tool_projection = json.dumps(dynamic.get("tool_guidance", {}), ensure_ascii=False)
    for forbidden in ("synthetic_exact_operation", "synthetic_parameter_detail_only", "operation_key", "observed_schema_hash", "call_notes"):
        check(forbidden not in tool_projection, "automatic tool details absent:" + forbidden)
    server.onboarding.confirm_context_injected(**common, wake_id=wake["wake_id"],
        wake_capability=wake["wake_capability"], context_hash=prepared["context_hash"])

    registry = ExecutionStore(database, deployment_epoch=os.environ["STBRAIN_EXECUTION_EPOCH"],
                              capability_secret=server.WAKE_SECRET)
    args = {"module": "learning_memory", "content": "Synthetic gateway-bound memory."}
    name = "remember_memory"
    issued = registry.issue_batch(**common, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
        batch_id="synthetic-simple-batch", revision=1, calls=[{"call_id": "synthetic-simple-call",
            "advertised_name": name, "canonical_tool": name,
            "schema_hash": canonical_hash(server.mcp._tool_manager.get_tool(name).parameters),
            "catalog_hash": advertised["catalog_hash"], "arguments_hash": canonical_hash(args)}])
    execution_ref = issued["executions"][0]["execution_ref"]
    bound = await call(name, {**args, "execution_ref": execution_ref})
    check(bound.get("decision") == "stored", "actual gateway claim allows ordinary operation", bound)
    check(scalar("SELECT wake_id FROM learning_versions WHERE learning_id=? AND version=1", (bound["id"],)) == wake["wake_id"],
          "gateway ordinary write retains claimed wake identity")
    replay = await call(name, {**args, "execution_ref": execution_ref})
    check(replay.get("decision") == "reject" and replay.get("state_changed") is False, "gateway claim cannot replay", replay)
    check(scalar("SELECT count(*) FROM brain_onboarding_state WHERE module_one_status='complete'") == 1,
          "gateway ordinary operation retains completed self definition")
    with closing(sqlite3.connect(database)) as connection:
        check(connection.execute("SELECT * FROM self_model_revisions ORDER BY revision_id").fetchall() == original_core,
              "all ordinary operations leave core revision bytes unchanged")
    check(scalar("SELECT count(*) FROM brain_wake_sessions") == bootstrap_wakes + 1,
          "only the explicit synthetic gateway turn adds one wake")
    return {"decision": "PASS", "checks": checks, "check_count": len(checks), "tool_count": len(tools),
            "synthetic_databases": 3, "module_one_bootstrapped": True,
            "unactivated_ordinary_writes_rejected": True, "ordinary_operations_created_wakes": 0,
            "network_listener_started": False, "real_memory_accessed": False,
            "http_authentication_tested": False, "gateway_claim_replay_rejected": True,
            "legacy_generic_binding_decision": legacy["decision"]}


async def no_network_probe():
    # The asyncio loop already exists here, so even Windows' internal socketpair
    # setup has finished. Block both remote and loopback application networking.
    attempts = []

    def deny_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("network_access_forbidden_in_simple_transport_test")

    with ExitStack() as guards:
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto",
                       "socket.create_connection", "socket.getaddrinfo"):
            guards.enter_context(patch(target, side_effect=deny_network))
        result = await probe()
    if attempts:
        raise AssertionError("a network attempt was swallowed by the application")
    result["network_attempts"] = 0
    return result


class SimpleProfileTransportTests(unittest.TestCase):
    def test_emotional_author_changes_schema_and_manual_match_runtime(self):
        import jsonschema
        root = Path(__file__).resolve().parents[2]
        schema = json.loads((root / "schemas/emotional-memory.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        author_schema = {"$ref": "#/$defs/authoredChanges", "$defs": schema["$defs"]}
        jsonschema.validate({"original_text": "Synthetic exact authored body", "memory_type": "shared_event",
                             "source_timestamp": "2026-09-11T00:00:00+00:00"}, author_schema,
                            format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
        for invalid in ({"owner_id": "other"}, {"source_timestamp": 1},
                        {"memory_type": "unknown_type"}, {"original_text": ""}):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid, author_schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
        self.assertFalse(schema["x-invariants"]["automatic_sensitive_original_disclosure"])
        self.assertTrue(schema["x-invariants"]["model_asserted_emergency_never_expands_disclosure"])
        self.assertEqual(["normal", "neutral_hint", "ask_first", "never_auto"], schema["properties"]["context_policy"]["enum"])
        self.assertEqual(["never", "ask_first", "allow_after_confirmation"], schema["properties"]["explicit_request_override"]["enum"])
        manual = (root / "mcp_server/emotional_service.py").read_text(encoding="utf-8")
        self.assertNotIn("不能修改 original_text", manual)
        self.assertNotIn("原文一经保存不可覆盖", manual)
        self.assertIn("source_timestamp", manual)

    def test_isolated_real_dispatch_after_genuine_synthetic_activation(self):
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
                   "LOCALAPPDATA", "APPDATA", "PATHEXT", "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="simple-profile-synthetic-") as scratch:
            env.update({"STBRAIN_ACCESS_PROFILE": "simple-memory-v1",
                "STBRAIN_MCP_TOKEN": "synthetic-token-000000000000000000000000",
                "STBRAIN_WAKE_SECRET": "synthetic-wake-00000000000000000000000",
                "STBRAIN_OWNER_ID": "synthetic-owner", "STBRAIN_MODEL_ID": "synthetic-model",
                "STBRAIN_DB_PATH": str(Path(scratch) / "main.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(Path(scratch) / "ideas.db"),
                "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(Path(scratch) / "vault.db"),
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "1", "STBRAIN_EXECUTION_EPOCH": "synthetic-simple-epoch",
                "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
            completed = subprocess.run([sys.executable, "-B", "-m", "mcp_server.tests.test_simple_profile_transport", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding="utf-8", timeout=60)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertEqual(44, proof["tool_count"])
        self.assertTrue(proof["module_one_bootstrapped"])
        self.assertTrue(proof["unactivated_ordinary_writes_rejected"])
        self.assertEqual(0, proof["ordinary_operations_created_wakes"])
        self.assertEqual(0, proof["network_attempts"])
        print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(no_network_probe()), ensure_ascii=False))
    else:
        unittest.main()
