"""Independent simple-memory audit: synthetic files/stores, no network or live ST.

The subprocess probe imports the actual registered MCP server against three new
temporary databases. Dispatch is in process (not an HTTP/model/phone test).
"""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp_server.self_password import SelfPasswordAuthority, encode_password
from runtime.execution_binding import ExecutionClaim
from runtime.ordinary_access import (
    authenticated_ordinary_operation, current_ordinary_access, tool_scope,
)


PASSWORD = "synthetic-local-authority-passphrase"
OWNER, MODEL = "synthetic-simple-owner", "synthetic-simple-model"


class PasswordAuthorityTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="simple-password-synthetic-"))
        self.path = Path(directory) / "verifier.json"
        self.path.write_text(json.dumps(encode_password(PASSWORD, salt=b"S" * 16)), encoding="utf-8")
        self.now = 1000.0
        self.authority = SelfPasswordAuthority(self.path, clock=lambda: self.now)

    def test_hash_format_roundtrip_never_contains_plaintext(self):
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual({"format", "salt_hex", "digest_hex"}, set(data))
        self.assertEqual(32, len(data["salt_hex"]))
        self.assertEqual(64, len(data["digest_hex"]))
        self.assertNotIn(PASSWORD, self.path.read_text(encoding="utf-8"))
        result = self.authority.authorize(PASSWORD)
        self.assertEqual("authorized", result["decision"])
        self.assertEqual(900, result["expires_in_seconds"])
        self.assertTrue(self.authority.allowed())
        self.assertNotIn(PASSWORD, repr(result))
        self.assertNotIn(PASSWORD, repr(vars(self.authority)))

    def test_wrong_password_and_five_attempt_rate_limit(self):
        for _ in range(5):
            self.assertEqual("self_password_invalid", self.authority.authorize("wrong")["reason_code"])
        self.assertFalse(self.authority.allowed())
        self.assertEqual("self_password_retry_later", self.authority.authorize(PASSWORD)["reason_code"])
        self.now += 60
        self.assertEqual("authorized", self.authority.authorize(PASSWORD)["decision"])

    def test_expiry_exactly_fifteen_minutes_and_revoke(self):
        self.authority.authorize(PASSWORD)
        self.now += 899.99
        self.assertTrue(self.authority.allowed())
        self.now += 0.01
        self.assertFalse(self.authority.allowed())
        self.authority.authorize(PASSWORD)
        self.authority.revoke()
        self.assertFalse(self.authority.allowed())

    def test_rotation_immediately_invalidates_prior_authorization(self):
        self.authority.authorize(PASSWORD)
        self.path.write_text(json.dumps(encode_password("synthetic-replacement", salt=b"R" * 16)), encoding="utf-8")
        self.assertFalse(self.authority.allowed())
        self.assertEqual("self_password_invalid", self.authority.authorize(PASSWORD)["reason_code"])
        self.assertEqual("authorized", self.authority.authorize("synthetic-replacement")["decision"])

    def test_missing_malformed_and_oversized_verifiers_fail_closed(self):
        self.assertFalse(SelfPasswordAuthority(None).allowed())
        for content in ("[]", "null", "{}", "malformed", " " * 1025):
            self.path.write_text(content, encoding="utf-8")
            with self.subTest(content_kind=content[:10]):
                self.assertEqual("reject", self.authority.authorize(PASSWORD)["decision"])
                self.assertFalse(self.authority.allowed())
        self.path.unlink()
        self.assertFalse(self.authority.allowed())

    def test_failed_receipt_revokes_global_authority_and_hides_exception(self):
        class FailedReceipt:
            def issue_direct_grant(self, **kwargs):
                raise RuntimeError(PASSWORD)
        result = self.authority.issue(PASSWORD, onboarding=FailedReceipt(), owner_id=OWNER,
                                      model_id=MODEL, client_principal="synthetic-principal")
        self.assertEqual("self_authorization_receipt_unavailable", result["reason_code"])
        self.assertFalse(self.authority.allowed())
        self.assertNotIn(PASSWORD, repr(result))

    def test_missing_grant_reference_is_not_authorization_success(self):
        class MissingReceipt:
            def issue_direct_grant(self, **kwargs):
                return {"decision": "already_issued", "grant_ref": None}
        result = self.authority.issue(PASSWORD, onboarding=MissingReceipt(), owner_id=OWNER,
                                      model_id=MODEL, client_principal="synthetic-principal")
        self.assertEqual("reject", result["decision"])
        self.assertFalse(self.authority.allowed())


class OrdinaryContextTests(unittest.TestCase):
    def test_scope_allowlist_excludes_self_vault_and_unknown_generic_targets(self):
        for name in ("submit_self_model_candidate", "activate_self_model_candidate",
                     "open_hallucination_vault", "hold_hallucination_record", "authorize_self_model"):
            self.assertIsNone(tool_scope(name, {}))
        for module in ("self_revision", "hallucination_vault", "../../self_revision"):
            self.assertIsNone(tool_scope("remember_memory", {"module": module}))
        self.assertIsNone(tool_scope("revise_memory", {"target_ref": "self://synthetic@1"}))
        with self.assertRaises(ValueError):
            with authenticated_ordinary_operation(owner_id=OWNER, model_id=MODEL, scope="self_revision"):
                pass

    def test_identity_scope_copy_and_context_cleanup(self):
        with authenticated_ordinary_operation(owner_id=OWNER, model_id=MODEL, scope="learning_memory") as access:
            self.assertIsNone(current_ordinary_access(owner_id="other", model_id=MODEL))
            self.assertIsNone(current_ordinary_access(owner_id=OWNER, model_id="other"))
            self.assertIsNone(current_ordinary_access(owner_id=OWNER, model_id=MODEL, scope="self_revision"))
            access["authorized_scopes"].append("self_revision")
            self.assertIsNone(current_ordinary_access(owner_id=OWNER, model_id=MODEL, scope="self_revision"))
        self.assertIsNone(current_ordinary_access(owner_id=OWNER, model_id=MODEL))
        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            with authenticated_ordinary_operation(owner_id=OWNER, model_id=MODEL, scope="learning_memory"):
                raise RuntimeError("synthetic")
        self.assertIsNone(current_ordinary_access(owner_id=OWNER, model_id=MODEL))

    def test_mismatched_claim_cannot_enter_context(self):
        claim = ExecutionClaim("synthetic-ref", OWNER, MODEL, "wake", "batch", "call",
                               "remember_memory", "epoch", "claim", "unused")
        for wrong in (replace(claim, owner_id="other"), replace(claim, model_id="other")):
            with self.assertRaisesRegex(ValueError, "ordinary_identity_mismatch"):
                with authenticated_ordinary_operation(owner_id=OWNER, model_id=MODEL,
                                                       scope="learning_memory", claim=wrong):
                    pass

    def test_concurrent_tasks_do_not_share_context(self):
        async def work(scope):
            with authenticated_ordinary_operation(owner_id=OWNER, model_id=MODEL, scope=scope) as access:
                await asyncio.sleep(0)
                current = current_ordinary_access(owner_id=OWNER, model_id=MODEL)
                self.assertEqual([scope], current["authorized_scopes"])
                self.assertEqual(access["operation_id"], current["operation_id"])
                return access["operation_id"]
        async def both():
            return await asyncio.gather(work("learning_memory"), work("emotional_memory"))
        values = asyncio.run(both())
        self.assertEqual(2, len(set(values)))
        self.assertIsNone(current_ordinary_access(owner_id=OWNER, model_id=MODEL))


async def registered_probe():
    """Only the subprocess calls this; loop exists before socket guards."""
    root = Path(os.environ["STBRAIN_DB_PATH"]).parent.resolve()
    real_connect = sqlite3.connect
    allowed = {root / "main.db", root / "ideas.db", root / "vault.db"}
    def synthetic_connect(path, *args, **kwargs):
        assert Path(path).resolve() in allowed, "non-synthetic database access rejected"
        return real_connect(path, *args, **kwargs)
    with ExitStack() as stack:
        stack.enter_context(patch("socket.create_connection", side_effect=AssertionError("offline synthetic probe")))
        stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("offline synthetic probe")))
        stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("no child services in probe")))
        stack.enter_context(patch("sqlite3.connect", side_effect=synthetic_connect))
        from mcp_server import server
        from runtime.execution_binding import ExecutionStore, canonical_hash, current_execution_claim
        from tests.test_direct_grants import self_model_content
        common = {"owner_id": OWNER, "model_id": MODEL}
        async def call(name, args):
            blocks, result = await server.mcp.call_tool(name, args)
            assert json.loads(blocks[0].text) == result
            assert current_ordinary_access(**common) is None
            assert current_execution_claim() is None
            return result
        def count(table):
            assert table in {"brain_wake_sessions", "self_model_revisions", "brain_execution_calls"}
            with closing(sqlite3.connect(root / "main.db")) as connection:
                return connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
        def state_version():
            return server.onboarding.state(**common)["state"]["row_version"]
        assert await server.StaticBearerVerifier().verify_token("wrong") is None
        assert await server.StaticBearerVerifier().verify_token(os.environ["STBRAIN_MCP_TOKEN"]) is not None
        # Before initial activation, ordinary modules are readable but refuse
        # writes. This is a real registered-dispatch negative, not a forged flag.
        assert count("brain_wake_sessions") == 0
        assert count("self_model_revisions") == 0
        for module in ("emotional_memory", "learning_memory", "planning_memory"):
            rejected = await call("remember_memory", {"module": module, "content": "Synthetic blocked preactivation record"})
            assert rejected["reason_code"] == "module_one_required", "preactivation write unexpectedly allowed"
            assert rejected["state_changed"] is False
        assert count("brain_wake_sessions") == 0 and count("self_model_revisions") == 0

        async def ordinary_checks_after_activation():
            # The actual password/direct three-context chain below establishes
            # this baseline. Ordinary operations create no extra real wakes.
            bootstrap_wake_count = count("brain_wake_sessions")
            assert bootstrap_wake_count == 3 and count("self_model_revisions") == 1
            for module in ("emotional_memory", "learning_memory", "planning_memory"):
                stored = await call("remember_memory", {"module": module, "content": "Synthetic independent ordinary record"})
                assert stored["decision"] == "stored", stored
                revised = await call("revise_memory", {"target_ref": stored["ref"], "changes": {"summary": "Synthetic amended summary"}})
                assert revised["decision"] == "revised", revised
                stale = await call("revise_memory", {"target_ref": stored["ref"], "changes": {"summary": "Must preserve newer version"}})
                assert stale["decision"] == "reject" and stale["state_changed"] is False, stale
            for name in ("recall_emotional_memory", "recall_learning_memory", "recall_planning_memory"):
                recalled = await call(name, {"query": "Synthetic"})
                assert recalled.get("decision") != "reject", recalled
                assert "Synthetic amended summary" in json.dumps(recalled), recalled
            reminder = await call("remember_tool_guidance", {"tool_name": "synthetic-read-only-helper",
                             "purpose": "Read synthetic examples", "reminder": "Check the synthetic example first"})
            assert reminder["decision"] != "reject", reminder
            assert count("brain_wake_sessions") == bootstrap_wake_count
            assert count("self_model_revisions") == 1
            # Governance callers omit both mechanical CAS fields. Repeating the
            # same authored content and then editing it must both append history.
            governance_args = {"action": "set", "scope": "global",
                "text": "Synthetic chosen governance", "trigger_mode": "manual_only", "scene_tags": [],
                "reason": "Synthetic author decision"}
            first = await call("manage_self_governance_profile", governance_args)
            assert first["decision"] == "committed" and first["state_changed"] is True, first
            repeated = await call("manage_self_governance_profile", governance_args)
            assert repeated["decision"] == "committed" and repeated["row_version"] == 2, repeated
            assert repeated["revision_id"] != first["revision_id"], repeated
            revised_args = {**governance_args, "text": "Synthetic revised governance"}
            revised = await call("manage_self_governance_profile", revised_args)
            assert revised["decision"] == "committed" and revised["row_version"] == 3, revised
            assert revised["revision_id"] not in {first["revision_id"], repeated["revision_id"]}, revised
            before = await call("query_self_governance_profile", {
                "view": "revisions", "scope": "global", "include_content": True})
            assert len(before["result"]) == 3, before
            assert before["result"][-1]["content"]["text"] == revised_args["text"], before
            # Explicit old coordinates remain an intentional CAS assertion. Test
            # the stale row and stale active reference independently, not omission.
            for version, reason_code in (
                (first["row_version"], "governance_version_conflict"),
                (revised["row_version"], "active_governance_revision_conflict"),
            ):
                stale = await call("manage_self_governance_profile", {
                    **governance_args, "text": "Must preserve newer governance content",
                    "expected_profile_version": version,
                    "expected_active_revision": first["revision_id"]})
                assert stale["decision"] in {"reject", "rejected"} and stale["state_changed"] is False, stale
                assert stale["reason_codes"] == [reason_code], stale
                after = await call("query_self_governance_profile", {
                    "view": "revisions", "scope": "global", "include_content": True})
                assert after == before, after
                current = await call("query_self_governance_profile", {"view": "status"})
                assert current["result"]["scopes"]["global"]["active_revision_id"] == revised["revision_id"], current
            assert "ai_confirmation" not in governance_args
            # Injection controls retain their separate legacy active-reference
            # assertion. This change does not broaden their write semantics.
            control_args = {"action": "set", "scope": "self_model", "target_mode": "paused",
                "reason": "Synthetic switch decision"}
            first = await call("manage_injection_control", control_args)
            assert first["decision"] == "committed" and first["state_changed"] is True, first
            stale = await call("manage_injection_control", {**control_args, "target_mode": "enabled"})
            assert stale["decision"] in {"reject", "rejected"} and stale["state_changed"] is False, stale
            second = await call("manage_injection_control", {**control_args, "target_mode": "enabled",
                "expected_active_revision": first["revision_id"]})
            assert second["decision"] == "committed" and second["revision_id"] != first["revision_id"], second
            assert "ai_confirmation" not in control_args
            vault_control = await call("manage_injection_control", {"action": "set", "scope": "hallucination_vault",
                                "target_mode": "enabled", "reason": "Synthetic unauthorized vault switch"})
            assert vault_control["decision"] in {"reject", "rejected"} and vault_control["state_changed"] is False, vault_control
            assert count("brain_wake_sessions") == bootstrap_wake_count
            # Client data cannot install ContextVar identity or broaden scopes.
            for extra in ({"owner_id": "other"}, {"authorized_scopes": ["self_revision"]},
                          {"context_mode": "ordinary_authenticated"}):
                before = server.learning_service.status()["row_version"]
                try:
                    result = await call("remember_memory", {"module": "learning_memory", "content": "Synthetic", **extra})
                except Exception:
                    pass  # strict SDK/public schema rejection
                else:
                    assert result["decision"] == "reject", result
                assert server.learning_service.status()["row_version"] == before
            for bad in (None, "", "invented", 7, "stexec_" + "A" * 43):
                result = await call("remember_memory", {"module": "learning_memory", "content": "Bad binding", "execution_ref": bad})
                assert result["decision"] == "reject" and result["state_changed"] is False, ("invalid execution_ref", type(bad).__name__, result)
            blocked = await call("submit_self_model_candidate", {"intent": "acknowledge", "write_context_ref": "invented", "expected_row_version": 0})
            assert blocked["reason_code"] == "execution_binding_required", blocked
            assert "authorize_self_model" in blocked["next_action"], blocked
            assert "stbrain_open_direct" in blocked["next_action"], blocked
            vault = await call("hold_hallucination_record", {})
            assert vault["decision"] == "reject" and vault["state_changed"] is False, vault
            # Real valid gateway lease still binds exact raw arguments, finishes, rejects replay.
            tools = server.mcp._tool_manager.list_tools()
            entries = [{"canonical_name": t.name, "schema_hash": canonical_hash(t.parameters)} for t in tools]
            catalog = {"contract": "advertised-tools/1", "catalog_complete": True,
                       "catalog_hash": canonical_hash(entries), "entries": entries}
            wake = server.onboarding.issue_wake(**common, host_id="synthetic-host", thread_id="synthetic-thread",
                                               source_kind="human_message", source_event_id="synthetic-event")
            prepared = server.onboarding.build_pre_generation_context(**common, wake_id=wake["wake_id"],
                        wake_capability=wake["wake_capability"], source_digest="synthetic", host_contract_digest="synthetic", advertised_tools=catalog,
                        source_frame={"query_text": "Synthetic independent ordinary record", "lineage_stable": False, "capture_items": []})
            with closing(sqlite3.connect(root / "main.db")) as connection:
                snapshot = connection.execute("SELECT stable_json,dynamic_json FROM brain_context_snapshots WHERE wake_id=?", (wake["wake_id"],)).fetchone()
            assert "active_identity_capsule" in json.loads(snapshot[0]), "activated core missing"
            dynamic = json.loads(snapshot[1])
            assert dynamic and "Synthetic" in snapshot[1], dynamic
            assert "active_identity_capsule" not in dynamic and "hallucination_vault" not in dynamic, dynamic
            server.onboarding.confirm_context_injected(**common, wake_id=wake["wake_id"],
                        wake_capability=wake["wake_capability"], context_hash=prepared["context_hash"])
            registry = ExecutionStore(root / "main.db", deployment_epoch="synthetic-simple-epoch", capability_secret=server.WAKE_SECRET)
            args = {"module": "learning_memory", "content": "Synthetic signed record"}
            tool = server.mcp._tool_manager.get_tool("remember_memory")
            batch = dict(**common, wake_id=wake["wake_id"], wake_capability=wake["wake_capability"], batch_id="synthetic-batch", revision=1)
            lease = registry.issue_batch(**batch, calls=[{"call_id": "synthetic-call", "canonical_tool": "remember_memory",
                        "advertised_name": "remember_memory", "schema_hash": canonical_hash(tool.parameters),
                        "catalog_hash": catalog["catalog_hash"], "arguments_hash": canonical_hash(args)}])["executions"][0]["execution_ref"]
            altered = await call("remember_memory", {**args, "content": "Tampered", "execution_ref": lease})
            assert altered["decision"] == "reject", altered
            stored = await call("remember_memory", {**args, "execution_ref": lease})
            assert stored["decision"] == "stored", stored
            replay = await call("remember_memory", {**args, "execution_ref": lease})
            assert replay["decision"] == "reject", replay
            assert registry.batch_status(**batch)["counts"]["completed"] == 1
        # Password holder can traverse actual direct-grant + public self-edit chain.
        verifier = root / "password.json"
        verifier.write_text(json.dumps(encode_password(PASSWORD, salt=b"S" * 16)), encoding="utf-8")
        assert server.mcp._tool_manager.get_tool("authorize_self_model").parameters["required"] == ["password"]
        for bad_password_args in ({"password": PASSWORD, "debug": PASSWORD}, {"password": {"unsafe": PASSWORD}}):
            try:
                await call("authorize_self_model", bad_password_args)
            except Exception as exc:
                assert PASSWORD not in str(exc), "password leaked through validation error"
            else:
                raise AssertionError("malformed password call accepted")
        async def authorized_open():
            issued = await call("authorize_self_model", {"password": PASSWORD})
            assert issued["decision"] == "authorized", issued
            assert PASSWORD not in json.dumps(issued)
            opened = await call("stbrain_open_direct", {"grant_ref": issued["grant_ref"]})
            assert opened["write_context_available"] is True, opened
            return opened
        opened = await authorized_open()
        for intent, payload in (("acknowledge", {}), ("acknowledge", {}), ("save_calm_prompt", {"text": "Synthetic self revision deliberate intent."})):
            result = await call("submit_self_model_candidate", {"intent": intent, "payload": payload,
                                "write_context_ref": opened["write_context_ref"], "expected_row_version": state_version()})
            assert result["decision"] in {"advanced", "saved"}, result
        submitted = await call("submit_self_model_candidate", {"intent": "submit", "payload": {"content": self_model_content("synthetic"),
                             "reason": "Synthetic chosen self definition."}, "write_context_ref": opened["write_context_ref"],
                             "expected_row_version": state_version()})
        assert submitted["decision"] == "pending", submitted
        async def activate(opened):
            return await call("activate_self_model_candidate", {"candidate_id": submitted["candidate_id"],
                 "write_context_ref": opened["write_context_ref"], "expected_row_version": state_version(),
                 "expected_active_revision": None, "ai_confirmation": True})
        assert (await activate(opened))["decision"] != "activate"
        second = await authorized_open()
        reviewed = await call("submit_self_model_candidate", {"intent": "accept_review", "payload": {"ai_confirmation": True},
                       "write_context_ref": second["write_context_ref"], "expected_row_version": state_version()})
        assert reviewed["decision"] == "review_accepted", reviewed
        assert (await activate(second))["decision"] != "activate"
        third = await authorized_open()
        activated = await activate(third)
        assert activated["decision"] == "activate" and activated["pointer_changed"], activated
        with closing(sqlite3.connect(root / "main.db")) as connection:
            issued_events = connection.execute("SELECT actor,reason_codes_json FROM brain_onboarding_events WHERE action='issue_direct_grant'").fetchall()
            opened_events = connection.execute("SELECT reason_codes_json FROM brain_onboarding_events WHERE action='consume_direct_grant'").fetchall()
        assert len(issued_events) == 3 and all(actor == "password_holder" and "password_possession_direct_grant_issued" in json.loads(reasons)
                                             for actor, reasons in issued_events), issued_events
        assert len(opened_events) == 3 and all("password_possession_direct_context_opened" in json.loads(reasons) for reasons, in opened_events), opened_events
        # Rotation/missing verifier closes self edits even with a valid direct context.
        direct_binding = server.onboarding.current_open_write_context(
            **common, write_context_ref=third["write_context_ref"], required_scope="self_revision")
        assert direct_binding["write_context_available"] is True, direct_binding
        assert direct_binding["context_mode"] == "human_attested_direct", direct_binding
        verifier.unlink()
        closed = await call("submit_self_model_candidate", {"intent": "acknowledge", "write_context_ref": third["write_context_ref"], "expected_row_version": state_version()})
        assert closed["reason_code"] == "self_password_required", closed
        assert "authorize_self_model" in closed["next_action"], closed
        assert "stbrain_open_direct" in closed["next_action"], closed
        await ordinary_checks_after_activation()
        for database in allowed:
            with closing(sqlite3.connect(database)) as connection:
                dump = "\n".join(connection.iterdump())
            assert PASSWORD not in dump
        return {"decision": "PASS", "ordinary_modules": 3, "unactivated_ordinary_writes_rejected": True, "activated_ordinary_writes_passed": True,
                "gateway_lease_replay_rejected": True, "direct_self_chain_complete": True,
                "real_model_calls": 0, "real_memory_accessed": False}


class RegisteredSimpleMemoryTests(unittest.TestCase):
    def test_real_server_registration_in_synthetic_subprocess(self):
        allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATH", "PATHEXT", "TEMP", "TMP", "OS"}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix="simple-native-synthetic-") as scratch:
            root = Path(scratch)
            env.update(STBRAIN_MCP_TOKEN="synthetic-token-00000000000000000000000",
                       STBRAIN_WAKE_SECRET="synthetic-wake-0000000000000000000000", STBRAIN_OWNER_ID=OWNER,
                       STBRAIN_MODEL_ID=MODEL, STBRAIN_DB_PATH=str(root / "main.db"),
                       STBRAIN_LEARNING_IDEA_DB_PATH=str(root / "ideas.db"),
                       STBRAIN_HALLUCINATION_VAULT_DB_PATH=str(root / "vault.db"),
                       STBRAIN_REQUIRE_EXECUTION_BINDING="1", STBRAIN_EXECUTION_EPOCH="synthetic-simple-epoch",
                       STBRAIN_ACCESS_PROFILE="simple-memory-v1", STBRAIN_SELF_PASSWORD_HASH_FILE=str(root / "password.json"),
                       PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, "-B", "-m", "tests.test_simple_memory_access", "--probe"],
                          cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=90)
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertTrue(proof["direct_self_chain_complete"])


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(registered_probe())))
    else:
        unittest.main()
