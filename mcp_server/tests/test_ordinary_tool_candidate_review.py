"""Legacy tool proposals adopted by authenticated ordinary authors.

Synthetic SQLite only. Real onboarding/service binding and a subprocess-isolated
registered MCP dispatcher are exercised; no HTTP listener or upstream request.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
import hashlib
import inspect
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

from mcp_server.tool_guidance_service import ToolGuidanceAccessService
from runtime import ModuleOneOnboardingStore
from runtime.ordinary_access import authenticated_ordinary_operation
from runtime.tool_guidance import ToolGuidanceError, ToolGuidanceStore
from tests.test_tool_guidance import action_card, catalog
from tests import test_onboarding as onboarding_fixtures

OWNER, MODEL = "synthetic-tool-author", "synthetic-tool-model"
OLD_SEQ = 2**62


def seed_proposal(store, card, *, expected, advertised):
    return store.propose_revision(
        owner_id=OWNER, model_id=MODEL, wake_id="synthetic-legacy-wake", wake_seq=OLD_SEQ,
        expected_row_version=expected, card_id=card["card_id"], expected_card_version=card["version"],
        intent="revise", edit_class="major", purpose="合成旧候选：更清楚地说明场景。",
        reason="Synthetic legacy fixture, no real tool request",
        correctness_assessment="I compared this complete synthetic proposal carefully.",
        calm_check_stability="This synthetic meaning remains stable over time.",
        calm_check_necessity="The narrower description improves this synthetic advice.",
        calm_check_consequences="The proposal changes advice and preserves execution checks.",
        calm_check_alternatives="Keeping the previous version remains possible.", catalog=advertised,
    )


class OrdinaryToolCandidateReviewTests(unittest.TestCase):
    def setUp(self):
        self.scratch = self.enterContext(tempfile.TemporaryDirectory(prefix="ordinary-tool-candidate-"))
        self.database = Path(self.scratch) / "synthetic.db"
        self.onboarding = ModuleOneOnboardingStore(
            self.database, capability_secret=b"synthetic-only-tool-candidate-binding-key",
        )
        self.store = ToolGuidanceStore(self.database)
        self.advertised = catalog()
        self.service = ToolGuidanceAccessService(self.store, onboarding=self.onboarding,
            owner_id=OWNER, model_id=MODEL, catalog_provider=lambda binding: self.advertised)
        self.card = self.store.remember(owner_id=OWNER, model_id=MODEL, wake_id="synthetic-create",
            expected_row_version=0, catalog=self.advertised, **action_card())["card"]
        self.pending = seed_proposal(self.store, self.card, expected=1, advertised=self.advertised)

    def row(self):
        return self.service.status()["row_version"]

    def context(self, *, owner=OWNER, model=MODEL, scope="tool_guidance"):
        return authenticated_ordinary_operation(owner_id=owner, model_id=model, scope=scope)

    def rows(self, table):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM " + table)]

    def snapshot(self):
        return {table: self.rows(table) for table in (
            "tool_module_state", "tool_cards", "tool_card_versions", "tool_guidance_candidates", "tool_audit_events")}

    def review(self, access, **overrides):
        values = dict(write_context_ref=access["write_context_ref"], expected_tool_row_version=self.row(),
            candidate_id=self.pending["candidate_id"], candidate_hash=self.pending["candidate_hash"],
            expected_base_version=1, decision="accept", reason="我明确采纳这份合成候选。")
        values.update(overrides)
        return self.service.review(**values)

    def legacy_review(self, **overrides):
        values = dict(owner_id=OWNER, model_id=MODEL, wake_id="synthetic-legacy-wake", wake_seq=OLD_SEQ,
            expected_row_version=self.row(), candidate_id=self.pending["candidate_id"],
            candidate_hash=self.pending["candidate_hash"], expected_base_version=1,
            decision="accept", correctness_decision="correct", ai_confirmation=True,
            correctness_assessment="I reviewed the complete synthetic candidate in its actual legacy wake.",
            reason="Synthetic old-wake compatibility check", catalog=self.advertised)
        values.update(overrides)
        return self.store.review_candidate(**values)

    def assert_no_fake_wake(self):
        self.assertEqual([], self.rows("brain_wake_sessions"))
        self.assertEqual([], self.rows("brain_context_snapshots"))
        row = self.rows("tool_guidance_candidates")[0]
        self.assertIsNone(row["presented_wake_id"])
        self.assertIsNone(row["presented_wake_seq"])
        self.assertIsNone(row["reviewed_wake_id"])
        self.assertIsNone(row["reviewed_wake_seq"])

    def test_real_service_manual_then_accept_in_two_independent_ordinary_requests(self):
        self.advertised = None
        before = self.snapshot()
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            with self.context() as first:
                shown = self.service.manual(write_context_ref=first["write_context_ref"])["pending_candidates"][0]
            self.assertEqual(before, self.snapshot(), "ordinary presentation must be read-only")
            with self.context() as second:
                accepted = self.review(second, candidate_hash=shown["candidate_hash"])
        self.assertNotEqual(first["wake_id"], second["wake_id"])
        self.assertLess(second["wake_seq"], OLD_SEQ, "must not pass merely because ordinary timestamps are large")
        self.assertFalse(shown["review_requires_later_wake"])
        self.assertFalse(shown["presentation_binding_required"])
        self.assertEqual("accepted", accepted["decision"])
        self.assertEqual("ordinary_candidate_acceptance", accepted["submission_mode"])
        self.assertTrue(accepted["author_confirmed"])
        self.assertFalse(accepted["independent_review_performed"])
        self.assertFalse(accepted["execution_performed"])
        self.assertEqual(2, accepted["card"]["version"])
        self.assertEqual(2, len(self.rows("tool_card_versions")))
        versions = self.rows("tool_card_versions")
        self.assertEqual(self.card["content_hash"], versions[0]["content_hash"])
        self.assertEqual(["ordinary_candidate_accepted"], json.loads(versions[1]["classification_reason_codes_json"]))
        event = self.rows("tool_audit_events")[-1]
        self.assertIn("author_confirmed", json.loads(event["reason_codes_json"]))
        self.assertNotIn("cross_wake_review_accepted", json.dumps(event))
        self.assertFalse(json.loads(event["details_json"])["independent_review_performed"])
        self.assertEqual(second["wake_id"], event["wake_id"], "operation id belongs in audit, not real-wake evidence")
        self.assert_no_fake_wake()

    def test_keep_pending_then_accept_with_new_request_and_no_review_essay(self):
        self.advertised = None
        with self.context() as first:
            kept = self.review(first, decision="keep_pending")
        self.assertEqual("keep_pending", kept["decision"])
        self.assertEqual(["author_kept_pending"], kept["reason_codes"])
        self.assertEqual("pending", self.rows("tool_guidance_candidates")[0]["status"])
        self.assertEqual(1, len(self.rows("tool_card_versions")))
        with self.context() as second:
            accepted = self.review(second)
        self.assertEqual("accepted", accepted["decision"])
        self.assertEqual(4, self.row())
        self.assertNotEqual(first["wake_id"], second["wake_id"])
        self.assert_no_fake_wake()

    def test_elapsed_pending_time_and_changed_catalog_do_not_block_author_adoption(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE tool_guidance_candidates SET expires_at='2000-01-01T00:00:00+00:00'")
            connection.commit()
        self.advertised = catalog(home_schema="c" * 64)
        with self.context() as first:
            shown = self.service.manual(write_context_ref=first["write_context_ref"])["pending_candidates"][0]
        self.assertTrue(shown["time_limit_elapsed"])
        self.assertEqual("stale_schema", shown["schema_status"])
        self.assertEqual("pending", self.rows("tool_guidance_candidates")[0]["status"])
        with self.context() as second:
            accepted = self.review(second)
        self.assertEqual("accepted", accepted["decision"])
        self.assertEqual("a" * 64, accepted["card"]["content"]["observed_schema_hash"], "author adoption is not a new observation")

    def test_hash_base_and_row_conflicts_are_atomic(self):
        cases = (
            ({"candidate_hash": "0" * 64}, "candidate_hash_mismatch"),
            ({"expected_base_version": 2}, "candidate_base_changed"),
            ({"expected_tool_row_version": 0}, "tool_row_version_conflict"),
        )
        for override, reason in cases:
            with self.subTest(reason=reason):
                before = self.snapshot()
                with self.context() as access:
                    result = self.review(access, **override)
                self.assertEqual([reason], result["reason_codes"])
                self.assertFalse(result["state_changed"])
                self.assertEqual(before, self.snapshot())

    def test_current_card_advanced_after_proposal_rejects_stale_acceptance(self):
        with self.context() as first:
            revised = self.service.revise(write_context_ref=first["write_context_ref"],
                expected_tool_row_version=self.row(), card_id=self.card["card_id"], expected_card_version=1,
                reminder="后来作者直接写下的当前提醒。", reason="Synthetic newer version")
        self.assertEqual(2, revised["card"]["version"])
        before = self.snapshot()
        with self.context() as second:
            result = self.review(second)
        self.assertEqual(["candidate_base_changed"], result["reason_codes"])
        self.assertEqual(before, self.snapshot())

    def test_wrong_owner_model_scope_and_stale_context_cannot_adopt(self):
        before = self.snapshot()
        for override in ({"owner": "other"}, {"model": "other"}, {"scope": "learning_memory"}):
            with self.subTest(override=override), self.context(**override) as access:
                self.assertEqual("reject", self.review(access)["decision"])
        with self.context() as old:
            pass
        with self.context() as current:
            result = self.review(current, write_context_ref=old["write_context_ref"])
        self.assertEqual(["brain_open_required"], result["reason_codes"])
        self.assertEqual(before, self.snapshot())

    def test_runtime_candidate_namespace_is_still_enforced(self):
        self.store.ensure_state(owner_id="synthetic-other", model_id=MODEL)
        before = self.snapshot()
        with self.assertRaisesRegex(ToolGuidanceError, "candidate_not_found"):
            self.store.review_candidate(owner_id="synthetic-other", model_id=MODEL, wake_id="ordinaryop_synthetic",
                wake_seq=1, expected_row_version=0, candidate_id=self.pending["candidate_id"],
                candidate_hash=self.pending["candidate_hash"], expected_base_version=1,
                decision="accept", reason="Synthetic wrong owner", ordinary_author=True)
        self.assertEqual(before, self.snapshot())

    def test_mode_is_not_a_service_argument_and_unbound_reference_cannot_enable_it(self):
        signature = inspect.signature(self.service.review)
        self.assertNotIn("ordinary_author", signature.parameters)
        self.assertNotIn("context_mode", signature.parameters)
        with self.context() as access:
            for key in ("ordinary_author", "context_mode"):
                with self.subTest(key=key), self.assertRaises(TypeError):
                    self.review(access, **{key: True})
        result = self.review({"write_context_ref": "ordinaryctx_caller_invented"})
        self.assertEqual("reject", result["decision"])
        self.assertEqual(1, len(self.rows("tool_card_versions")))

    def test_terminal_candidate_states_are_not_revived(self):
        for state in ("withdrawn", "expired", "accepted"):
            with self.subTest(state=state):
                with closing(sqlite3.connect(self.database)) as connection:
                    connection.execute("UPDATE tool_guidance_candidates SET status=?", (state,))
                    connection.commit()
                before = self.snapshot()
                with self.context() as access:
                    result = self.review(access)
                self.assertEqual(["candidate_not_pending"], result["reason_codes"])
                self.assertEqual(before, self.snapshot())

    def test_legacy_wake_still_requires_later_wake_presentation_and_live_schema(self):
        with self.assertRaisesRegex(ToolGuidanceError, "later_real_wake_required"):
            self.legacy_review()
        with self.assertRaisesRegex(ToolGuidanceError, "candidate_not_fully_presented"):
            self.legacy_review(wake_id="legacy-later", wake_seq=OLD_SEQ+1)
        self.store.present_pending_candidates(owner_id=OWNER, model_id=MODEL,
            wake_id="legacy-later", wake_seq=OLD_SEQ+1, catalog=self.advertised)
        with self.assertRaisesRegex(ToolGuidanceError, "schema_hash_mismatch"):
            self.legacy_review(wake_id="legacy-later", wake_seq=OLD_SEQ+1, catalog=catalog(home_schema="d"*64))
        accepted = self.legacy_review(wake_id="legacy-later", wake_seq=OLD_SEQ+1)
        self.assertEqual("accepted", accepted["decision"])
        self.assertNotIn("author_confirmed", accepted)
        version = self.rows("tool_card_versions")[-1]
        self.assertEqual(["cross_wake_review_accepted"], json.loads(version["classification_reason_codes_json"]))
        self.assertEqual("legacy-later", self.rows("tool_guidance_candidates")[0]["reviewed_wake_id"])

    def test_truthy_mode_and_same_real_wake_still_fail_closed(self):
        with self.assertRaisesRegex(ToolGuidanceError, "later_real_wake_required"):
            self.legacy_review(ordinary_author="ordinary_authenticated")
        with self.assertRaisesRegex(ToolGuidanceError, "ai_confirmation_required"):
            self.legacy_review(ai_confirmation=False)

    def test_reviewed_optional_expiry_gate_and_unchanged_revision_and_withdraw_pins(self):
        source = (Path(__file__).resolve().parents[2] / "runtime/tool_guidance.py").read_bytes()
        expected = {
            # Approved delta: only the expiry predicate now treats absent expiry
            # as undated. Catalog, schema, intent, authorization and confirmation
            # remain enforced; the registered optional-expiry suite checks them.
            "execution_gate": "c3fdad04cfc6b56b82b121fa51ab949c41124745af5256ddeb4629440b0e0161",
            "revise": "50eeb9580050d90c8103268b37aef4d97710728da84e976b1eaf6b324f978b89",
            "withdraw_candidate": "21603b9c4e0674e8afa1333b3f7fa96115b85f7cff6c3c35dd19734be7e9bce9",
        }
        for name, digest in expected.items():
            with self.subTest(name=name):
                method = re.search(rb"(?ms)^    def " + name.encode() + rb"\(.*?(?=^    def |\Z)", source).group()
                self.assertEqual(digest, hashlib.sha256(method).hexdigest())


async def registered_probe():
    attempts = []
    def no_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("network forbidden in synthetic candidate probe")
    root = Path(os.environ["STBRAIN_DB_PATH"]).parent.resolve()
    real_connect = sqlite3.connect
    allowed = {root / name for name in ("main.db", "ideas.db", "vault.db")}
    def only_synthetic(path, *args, **kwargs):
        assert Path(path).resolve() in allowed, "database outside synthetic root"
        return real_connect(path, *args, **kwargs)
    with ExitStack() as guards:
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.socket.sendto", "socket.create_connection", "socket.getaddrinfo"):
            guards.enter_context(patch(target, side_effect=no_network))
        guards.enter_context(patch("sqlite3.connect", side_effect=only_synthetic))
        from mcp_server import server
        # Public ordinary authoring follows genuine initial module-one
        # activation; this fixture performs the real synthetic three-wake flow.
        fixture = onboarding_fixtures.ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        with closing(sqlite3.connect(root / 'main.db')) as connection:
            initial_wakes = connection.execute('SELECT * FROM brain_wake_sessions').fetchall()
            initial_snapshots = connection.execute('SELECT * FROM brain_context_snapshots').fetchall()
        async def call(name, arguments):
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert json.loads(blocks[0].text) == result
            return result
        saved = await call("remember_tool_guidance", {"tool_name": "合成工具服务", "purpose": "留下可用场景。"})
        assert saved["decision"] == "stored", saved
        pending = seed_proposal(server.tool_guidance_service.store, saved["card"],
            expected=saved["tool_row_version"], advertised=None)
        common = {"candidate_id": pending["candidate_id"], "candidate_hash": pending["candidate_hash"],
                  "expected_base_version": 1}
        for mode_field in ("ordinary_author", "context_mode"):
            rejected = await call("review_tool_guidance_candidate", {**common, "decision": "accept", mode_field: True})
            assert rejected["decision"] == "reject", rejected
            assert rejected["state_changed"] is False
        kept = await call("review_tool_guidance_candidate", {**common, "decision": "keep_pending"})
        assert kept["decision"] == "keep_pending", kept
        accepted = await call("review_tool_guidance_candidate", {**common, "decision": "accept"})
        assert accepted["decision"] == "accepted" and accepted["author_confirmed"] is True, accepted
        assert accepted["execution_performed"] is False
        with closing(sqlite3.connect(root / "main.db")) as connection:
            operation_ids = [row[0] for row in connection.execute(
                "SELECT wake_id FROM tool_audit_events WHERE event_id IN (?,?)", (kept["event_id"], accepted["event_id"]))]
            assert len(set(operation_ids)) == 2 and all(value.startswith("ordinaryop_") for value in operation_ids)
            assert connection.execute('SELECT * FROM brain_wake_sessions').fetchall() == initial_wakes
            assert connection.execute('SELECT * FROM brain_context_snapshots').fetchall() == initial_snapshots
            assert connection.execute("SELECT count(*) FROM tool_card_versions").fetchone()[0] == 2
            assert connection.execute("SELECT reviewed_wake_id FROM tool_guidance_candidates").fetchone()[0] is None
    assert not attempts
    return {"decision": "PASS", "independent_ordinary_requests": 2, "versions": 2,
            "model_mode_fields_rejected": 2, "real_wakes_created": 0,
            "initialization_wakes": len(initial_wakes),
            "network_attempts": 0, "http_authentication_tested": False,
            "private_memory_accessed": False}


class RegisteredOrdinaryCandidateTests(unittest.TestCase):
    def test_registered_mcp_two_requests_after_activation_without_extra_wake(self):
        system_keys = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE",
                       "LOCALAPPDATA", "APPDATA", "PATHEXT", "SYSTEMDRIVE"}
        env = {key: value for key, value in os.environ.items() if key.upper() in system_keys}
        with tempfile.TemporaryDirectory(prefix="registered-tool-candidate-") as scratch:
            env.update({"STBRAIN_ACCESS_PROFILE": "simple-memory-v1",
                "STBRAIN_MCP_TOKEN": "synthetic-token-000000000000000000000000",
                "STBRAIN_WAKE_SECRET": "synthetic-wake-00000000000000000000000",
                "STBRAIN_OWNER_ID": OWNER, "STBRAIN_MODEL_ID": MODEL,
                "STBRAIN_DB_PATH": str(Path(scratch) / "main.db"),
                "STBRAIN_LEARNING_IDEA_DB_PATH": str(Path(scratch) / "ideas.db"),
                "STBRAIN_HALLUCINATION_VAULT_DB_PATH": str(Path(scratch) / "vault.db"),
                "STBRAIN_REQUIRE_EXECUTION_BINDING": "1", "STBRAIN_EXECUTION_EPOCH": "synthetic-tool-candidate-epoch",
                "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
            result = subprocess.run([sys.executable, "-B", "-m", "mcp_server.tests.test_ordinary_tool_candidate_review", "--probe"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding="utf-8", timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual("PASS", proof["decision"])
        self.assertEqual(0, proof["network_attempts"])
        self.assertEqual(3, proof["initialization_wakes"])
        self.assertEqual(0, proof["real_wakes_created"])
        print(json.dumps(proof, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(asyncio.run(registered_probe()), ensure_ascii=False))
    else:
        unittest.main()
