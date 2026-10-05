"""Authenticated ASGI MCP + real local CLI bytes, entirely synthetic and socket-free."""
import asyncio
import base64
from contextlib import ExitStack, closing
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


def probe():
    import httpx
    from starlette.testclient import TestClient
    from mcp_server import server
    from runtime.execution_binding import ExecutionStore, canonical_hash
    from runtime.memory_image_uploads import UPLOAD_CONTRACT, UPLOAD_TOOL
    from scripts.memory_images import ImageMCPClient, MAX_REQUEST_WIRE, MAX_RESPONSE_WIRE, read_local_image, export_new_file
    from tests.test_memory_images import maximum_png, synthetic_image
    from tests.test_memory_relations import seed_endpoints
    from tests.test_onboarding import ModuleOneOnboardingTests

    database = Path(os.environ["STBRAIN_DB_PATH"])
    identity = {"owner_id": server.OWNER_ID, "model_id": server.MODEL_ID}
    seed_endpoints(database)
    checks, sizes, attempts = [], [], []
    def check(condition, label):
        if not condition: raise AssertionError(label)
        checks.append(label)
    def deny(*args, **kwargs):
        attempts.append(True); raise AssertionError("external network forbidden")

    fixture = ModuleOneOnboardingTests()
    fixture.database, fixture.store, fixture.owner, fixture.model = database, server.onboarding, server.OWNER_ID, server.MODEL_ID
    tools = server.mcp._tool_manager.list_tools()
    check(len(tools) == 57, "existing tools plus one host-only transport capability")
    app = server.mcp.streamable_http_app()
    # ASGI transport invokes actual bearer/host validation, JSON-RPC parsing,
    # compact dispatch, execution gates and result serialization, without listen().
    with TestClient(app, base_url="http://127.0.0.1:8794") as transport, ExitStack() as guard:
        for name in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto", "socket.create_connection", "socket.getaddrinfo"):
            guard.enter_context(patch(name, side_effect=deny))
        def forward(request):
            response = transport.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            sizes.append((len(request.content), len(response.content)))
            return httpx.Response(response.status_code, headers=response.headers, content=response.content)

        unauthorized = transport.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        check(unauthorized.status_code == 401, "unauthenticated HTTP denied")
        oversized = transport.post("/mcp", content=b"x" * (MAX_REQUEST_WIRE + 1), headers={
            "Authorization": "Bearer " + server.MCP_TOKEN, "Content-Type": "application/json"})
        check(oversized.status_code == 413, "HTTP over 6 MiB rejected before JSON parsing")
        client = ImageMCPClient("http://127.0.0.1:8794/mcp", server.MCP_TOKEN, transport=httpx.MockTransport(forward))
        try:
            client.initialize()
            listed = client.rpc("tools/list", {})
            public_tools = [t for t in listed["tools"] if t["name"] != UPLOAD_TOOL]
            check(len(listed["tools"]) == 9 and len(public_tools) == 8, "daily8 plus separately marked host capability")
            capability = next(t for t in listed["tools"] if t["name"] == UPLOAD_TOOL)
            check(capability["_meta"] == {"stbrain/hostOnly": True, "stbrain/contract": UPLOAD_CONTRACT}, "host metadata preserved over MCP")
            check(capability["inputSchema"]["properties"]["protocol"]["enum"] == [UPLOAD_CONTRACT], "host capability protocol enum")
            raw_upload = maximum_png()
            host_args = {"protocol": UPLOAD_CONTRACT, "upload_ref": "stimgup_" + "a" * 64,
                "mime_type": "image/png", "sha256": hashlib.sha256(raw_upload).hexdigest(),
                "data_base64": base64.b64encode(raw_upload).decode()}
            staged_wire = client.rpc("tools/call", {"name": UPLOAD_TOOL, "arguments": host_args})
            staged = staged_wire["structuredContent"]
            check(staged["decision"] == "staged" and not staged["memory_written"] and not staged["write_authority_granted"], "host stage grants no memory authority")
            denied_stage = client.call_tool("stbrain_manage", {"action": UPLOAD_TOOL, "arguments": host_args})
            check(denied_stage["decision"] == "reject", "host stage absent from compact action directory")
            # Invalid scalar/extras must not echo bytes, pixel contents, or an
            # accidental credential, even when rejected before the service.
            for change in ({"data_base64": "secret-canary-upload"}, {"owner_id": "secret-canary-upload"},
                           {"protocol": "secret-canary-upload"}, {"secret-canary-upload": "unexpected"}):
                invalid = client.rpc("tools/call", {"name": UPLOAD_TOOL, "arguments": {**host_args, **change}})
                check(invalid.get("isError") is True or invalid.get("structuredContent", {}).get("decision") == "reject", "malformed upload denied")
                check("secret-canary-upload" not in json.dumps(invalid), "malformed upload never echoes original input")
            missing = {k: v for k, v in host_args.items() if k != "upload_ref"}
            missing["data_base64"] = "secret-canary-upload"
            invalid = client.rpc("tools/call", {"name": UPLOAD_TOOL, "arguments": missing})
            check(invalid.get("isError") is True and "secret-canary-upload" not in json.dumps(invalid), "missing stage fields never echo other payload fields")
            directory = client.call_tool("stbrain_tools", {"category": "images"})
            check(len(directory["actions"]) == 4, "image actions discoverable through compact MCP")
            schema = client.call_tool("stbrain_tools", {"action": "store_memory_image"})["arguments_schema"]
            check(schema["additionalProperties"] is False, "no URL/path/owner override in public schema")
            args = {"target_ref": "emotion://e1@1", "description": "Synthetic original bytes, not a visual interpretation",
                    "mime_type": "image/png", "data_base64": base64.b64encode(synthetic_image()).decode()}
            check(client.action("store_memory_image", args)["reason_code"] == "module_one_required", "HTTP activation gate")
            fixture.bootstrap_live()
            for change in ({"owner_id": "foreign"}, {"url": "https://example.invalid/image"},
                           {"path": "C:/private/file"}, {"execution_ref": "fake"}):
                invalid = client.action("store_memory_image", {**args, **change})
                check(invalid.get("decision") == "reject", "HTTP untrusted field denied:" + next(iter(change)))
            selected = database.parent / "chosen-maximum.png"
            selected.write_bytes(maximum_png())  # Synthetic 4 MiB pixels/metadata only.
            raw, mime = read_local_image(selected)
            args.update(mime_type=mime, data_base64=base64.b64encode(raw).decode())
            saved = client.action("store_memory_image", args)
            check(saved["decision"] == "stored" and saved["byte_count"] == len(raw), "actual 4 MiB original saved over HTTP MCP")
            check("data_uri" not in saved, "upload result metadata only")
            retry = client.action("store_memory_image", args)
            check(retry["image_ref"] == saved["image_ref"] and not retry["state_changed"], "uncertain upload retry is idempotent")
            read_args = {"target_ref": args["target_ref"], "image_ref": saved["image_ref"]}
            metadata = client.action("read_memory_image", read_args)
            check("data_uri" not in metadata, "default read never fetches bytes")
            received = client.action("read_memory_image", {**read_args, "include_data": True})
            restored = database.parent / "exported-new.png"
            digest = export_new_file(received, restored)
            check(digest == hashlib.sha256(raw).hexdigest() and restored.read_bytes() == raw, "CLI export fsync/hash readback preserves exact original")
            try:
                export_new_file(received, restored)
            except FileExistsError:
                check(restored.read_bytes() == raw, "repeat export cannot overwrite original output")
            else:
                raise AssertionError("exclusive output failed")
            check(client.action("read_memory_image", {**read_args, "target_ref": "emotion://e2@1", "include_data": True})["decision"] == "reject",
                  "HTTP wrong target cannot reveal bytes")
            with closing(sqlite3.connect(database)) as db:
                db.execute("UPDATE emotion_memories SET sensitivity='restricted' WHERE memory_id='e1'"); db.commit()
            sensitive = client.action("read_memory_image", {**read_args, "include_data": True})
            check(sensitive["reason_code"] == "image_sensitive_target_unsupported" and "description" not in sensitive and "data_uri" not in sensitive,
                  "reclassified original blocks image/description without separate sensitive consent")
            with closing(sqlite3.connect(database)) as db:
                db.execute("UPDATE emotion_memories SET sensitivity='private' WHERE memory_id='e1'"); db.commit()
            removed = client.action("remove_memory_image", read_args)
            check(removed["decision"] == "removed" and removed["original_retained_for_backup"], "remove preserves original for DB backup")
            check(client.action("store_memory_image", args)["decision"] == "removed", "old upload never revives removed image")
            check(client.action("list_memory_images", {"target_ref": args["target_ref"]})["images"] == [], "removed images not listed")
            check(max(item[0] for item in sizes) < MAX_REQUEST_WIRE, "4 MiB actual JSON request within 6 MiB bound")
            check(max(item[1] for item in sizes) < MAX_RESPONSE_WIRE, "4 MiB actual double MCP result within 12 MiB bound")
            check(max(item[1] for item in sizes) > 10 * 1024 * 1024, "large result actually exercised both content copies")
        finally:
            client.close()

        async def bound_probe():
            entries = [{"canonical_name": t.name, "schema_hash": canonical_hash(t.parameters)} for t in tools]
            advertised = {"contract": "advertised-tools/1", "catalog_complete": True, "catalog_hash": canonical_hash(entries), "entries": entries}
            wake = server.onboarding.issue_wake(**identity, host_id="synthetic-host", thread_id="synthetic-thread",
                source_kind="human_message", source_event_id="synthetic-images-context")
            with patch.object(server.memory_image_service, "read", side_effect=AssertionError("automatic image read forbidden")), \
                 patch.object(server.memory_image_service, "list", side_effect=AssertionError("automatic image list forbidden")):
                prepared = server.onboarding.build_pre_generation_context(**identity, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
                    source_digest="synthetic-source", host_contract_digest="synthetic-host", advertised_tools=advertised)
            check(prepared.get("decision") == "context_prepared", "ordinary context does not read images")
            check("data:image" not in json.dumps(prepared), "no image base64 in ordinary context")
            server.onboarding.confirm_context_injected(**identity, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"], context_hash=prepared["context_hash"])
            registry = ExecutionStore(database, deployment_epoch=os.environ["STBRAIN_EXECUTION_EPOCH"], capability_secret=server.WAKE_SECRET)
            async def bound(name, arguments, suffix):
                public = "stbrain_manage"
                issued = registry.issue_batch(**identity, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
                    batch_id="synthetic-images-batch-" + suffix, revision=1, calls=[{"call_id": "synthetic-call-" + suffix,
                        "canonical_tool": name, "advertised_name": public, "schema_hash": canonical_hash(server.mcp._tool_manager.get_tool(public).parameters),
                        "catalog_hash": advertised["catalog_hash"], "arguments_hash": canonical_hash(arguments)}])
                lease = issued["executions"][0]["execution_ref"]
                payload = {"action": name, "arguments": arguments, "execution_ref": lease}
                blocks, result = await server.mcp.call_tool(public, payload)
                check(json.loads(blocks[0].text) == result, "bound MCP text/structured match:" + suffix)
                _, replay = await server.mcp.call_tool(public, payload)
                check(replay.get("decision") == "reject", "execution lease cannot replay:" + suffix)
                return result
            arguments = {"target_ref": "learning://l1@1", "description": "Synthetic bound picture",
                         "mime_type": "image/png", "data_base64": base64.b64encode(synthetic_image()).decode()}
            saved = await bound("store_memory_image", arguments, "put")
            check(saved["decision"] == "stored", "gateway execution-bound upload")
            read = await bound("read_memory_image", {"target_ref": arguments["target_ref"], "image_ref": saved["image_ref"], "include_data": True}, "read")
            check(base64.b64decode(read["data_uri"].split(",")[1]) == synthetic_image(), "gateway execution-bound exact bytes")

            ref = "stimgup_" + "b" * 64
            upload_arguments = {"target_ref": "learning://l1@1", "description": "Synthetic reference-bound picture",
                                "mime_type": "image/png", "upload_ref": ref}
            issued = registry.issue_batch(**identity, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"],
                batch_id="synthetic-upload-reference-batch", revision=1, calls=[{"call_id": "synthetic-upload-reference-call",
                    "canonical_tool": "store_memory_image", "advertised_name": "stbrain_manage",
                    "schema_hash": canonical_hash(server.mcp._tool_manager.get_tool("stbrain_manage").parameters),
                    "catalog_hash": advertised["catalog_hash"], "arguments_hash": canonical_hash(upload_arguments)}])
            lease = issued["executions"][0]["execution_ref"]
            _, stage_result = await server.mcp.call_tool(UPLOAD_TOOL, {**host_args, "upload_ref": ref})
            check(stage_result["decision"] == "staged", "host can stage after exact final call was issued")
            with closing(sqlite3.connect(database)) as db:
                before_mismatch = db.execute("SELECT count(*) FROM memory_image_items").fetchone()[0]
            for changed in ({**upload_arguments, "upload_ref": "stimgup_" + "c" * 64},
                            {k: v for k, v in {**upload_arguments, "data_base64": host_args["data_base64"]}.items() if k != "upload_ref"}):
                _, denied = await server.mcp.call_tool("stbrain_manage", {"action": "store_memory_image", "arguments": changed, "execution_ref": lease})
                check(denied["decision"] == "reject", "bound final arguments cannot be substituted by host")
                with closing(sqlite3.connect(database)) as db:
                    check(db.execute("SELECT count(*) FROM memory_image_items").fetchone()[0] == before_mismatch,
                          "mismatched final arguments write zero formal image rows")
            payload = {"action": "store_memory_image", "arguments": upload_arguments, "execution_ref": lease}
            _, stored = await server.mcp.call_tool("stbrain_manage", payload)
            check(stored["decision"] == "stored", "original upload_ref arguments satisfy exact execution hash")
            check(stored["sha256"] == hashlib.sha256(raw_upload).hexdigest(), "staged formal record retains actual original SHA")
            _, replay = await server.mcp.call_tool("stbrain_manage", payload)
            check(replay["decision"] == "reject", "staged final execution lease cannot replay")
        # Reuse the ASGI loop: Windows creates its own loopback socketpair when
        # constructing another event loop, which is intentionally blocked here.
        transport.portal.call(bound_probe)
    check(not attempts, "no sockets/model/production contacted")
    return {"decision": "PASS", "checks": checks, "check_count": len(checks), "max_request_bytes": max(x[0] for x in sizes),
            "max_response_bytes": max(x[1] for x in sizes), "external_network_attempts": 0, "real_memory_accessed": False}


class MemoryImageTransportTests(unittest.TestCase):
    def test_real_authenticated_http_mcp_cli_four_mib_and_gateway_claims(self):
        allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PATHEXT", "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="images-transport-synthetic-") as scratch:
            env.update({"STBRAIN_ACCESS_PROFILE": "simple-memory-v1",
                "STBRAIN_MCP_TOKEN": "synthetic-token-000000000000000000000000", "STBRAIN_WAKE_SECRET": "synthetic-wake-00000000000000000000000",
                "STBRAIN_OWNER_ID": "synthetic-owner", "STBRAIN_MODEL_ID": "synthetic-model", "STBRAIN_DB_PATH": str(Path(scratch) / "main.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(Path(scratch) / "ideas.db"), "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(Path(scratch) / "vault.db"),
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "1", "STBRAIN_EXECUTION_EPOCH": "synthetic-images-epoch", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
            process = subprocess.run([sys.executable, "-B", "-m", "mcp_server.tests.test_memory_image_transport", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
        self.assertEqual(0, process.returncode, process.stderr)
        evidence = json.loads(process.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", evidence["decision"]); self.assertGreaterEqual(evidence["check_count"], 30)
        print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]: print(json.dumps(probe(), ensure_ascii=False))
    else: unittest.main()
