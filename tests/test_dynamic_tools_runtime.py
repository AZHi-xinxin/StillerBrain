"""Synthetic SQLite/control integration for a changing tool catalog."""
import copy
import json
import unittest
from dataclasses import replace

from mcp_server.control_server import ControlApplication
from runtime.execution_binding import ExecutionBindingError, canonical_hash
from runtime.onboarding import ModuleOneOnboardingStore
from tests import test_execution_binding as fixtures


class DynamicToolsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.ExecutionBindingTests()
        self.addCleanup(self.helper.doCleanups)
        self.helper.setUp()
        self.onboarding = self.helper.onboarding
        self.host_token = "synthetic-host-token-at-least-32-characters"
        self.human_token = "synthetic-human-token-at-least-32-characters"
        self.control = ControlApplication(
            self.onboarding, **self.helper.common, host_token=self.host_token,
            human_token=self.human_token, human_actor_id="synthetic-human",
            execution_store=self.helper.store)
        self.new_schema_hash = canonical_hash({"type": "object", "properties": {"mode": {"type": "string"}}})
        entries = [{"canonical_name": "renamed_stbrain_open", "schema_hash": self.new_schema_hash}]
        self.catalog = {"contract": "advertised-tools/1", "catalog_complete": True,
                        "catalog_hash": canonical_hash(entries), "entries": entries}

    def post_update(self, catalog=None, **override):
        payload = {"wake_id": self.helper.wake["wake_id"],
                   "wake_capability": self.helper.wake["wake_capability"],
                   "advertised_tools": self.catalog if catalog is None else catalog}
        token = override.pop("token", self.host_token)
        payload.update(override)
        return self.control.handle("POST", "/v1/host/context/tools", {
            "Content-Type": "application/json", "Authorization": "Bearer " + token,
        }, json.dumps(payload).encode())

    def snapshot(self):
        with self.onboarding._connect() as connection:
            return dict(connection.execute("SELECT * FROM brain_context_snapshots WHERE wake_id=?",
                (self.helper.wake["wake_id"],)).fetchone())

    def test_latest_tools_persist_without_changing_frozen_context(self):
        before = self.snapshot()
        status, result = self.post_update()
        self.assertEqual((200, "tools_updated"), (status, result["decision"]))
        after = self.snapshot()
        self.assertEqual(self.catalog, json.loads(after.pop("current_tools_json")))
        before.pop("current_tools_json")
        self.assertEqual(before, after)
        reopened = ModuleOneOnboardingStore(self.helper.database, capability_secret=self.helper.secret)
        self.assertEqual(self.catalog, reopened.current_advertised_tools(**self.helper.common))
        opened = reopened.open_brain_context(**self.helper.common, present_details=False)
        bound = reopened.current_open_write_context(**self.helper.common,
            write_context_ref=opened["write_context_ref"])
        self.assertEqual(self.catalog, bound["advertised_tools"])

    def test_old_issued_call_survives_rename_and_new_call_uses_latest_catalog(self):
        old_ref = self.helper.issue()[0]
        status, result = self.post_update()
        self.assertEqual((200, "tools_updated"), (status, result["decision"]))
        claim = self.helper.claim(old_ref)
        self.helper.store.finish(claim)
        call = {"call_id": "new-call", "advertised_name": "renamed_stbrain_open",
                "canonical_tool": "stbrain_open", "schema_hash": self.new_schema_hash,
                "catalog_hash": self.catalog["catalog_hash"], "arguments_hash": canonical_hash({"mode": "brief"})}
        batch = {**self.helper.batch, "batch_id": "next-batch", "revision": 2}
        issued = self.helper.store.issue_batch(**batch, calls=[call])
        new_claim = self.helper.store.claim(**self.helper.common,
            execution_ref=issued["executions"][0]["execution_ref"], tool_name="stbrain_open",
            arguments={"mode": "brief"})
        self.assertEqual(self.helper.wake["wake_id"], new_claim.wake_id)
        self.helper.store.finish(new_claim)
        # A *new* issue using the superseded catalog must not silently execute.
        call.update(catalog_hash=self.helper.catalog["catalog_hash"],
                    schema_hash=self.helper.schema_hash, advertised_name="stbrain_open")
        with self.assertRaisesRegex(ExecutionBindingError, "execution_catalog_mismatch"):
            self.helper.store.issue_batch(**{**batch, "batch_id": "stale-new-batch"}, calls=[call])

    def test_empty_catalog_accepts_old_result_but_offers_no_new_tools(self):
        old_ref = self.helper.issue()[0]
        empty = {"contract": "advertised-tools/1", "catalog_complete": True,
                 "catalog_hash": canonical_hash([]), "entries": []}
        self.assertEqual("tools_updated", self.post_update(empty)[1]["decision"])
        self.assertEqual(empty, self.onboarding.current_advertised_tools(**self.helper.common))
        self.helper.store.finish(self.helper.claim(old_ref))

    def test_wrong_token_capability_or_closed_wake_cannot_change_catalog(self):
        before = self.snapshot()
        self.assertEqual(401, self.post_update(token=self.human_token)[0])
        self.assertNotEqual("tools_updated", self.post_update(wake_capability="wrong")[1]["decision"])
        self.assertEqual(before, self.snapshot())
        self.helper.close()
        self.assertNotEqual("tools_updated", self.post_update()[1]["decision"])

    def test_malformed_update_is_rejected_without_mutation(self):
        before = self.snapshot()
        bad = copy.deepcopy(self.catalog)
        bad["catalog_hash"] = "0" * 64
        self.assertEqual(400, self.post_update(bad)[0])
        self.assertEqual(before, self.snapshot())

    def test_additive_migration_keeps_existing_original_catalog(self):
        # Simulate an older database that has no current-tools column.
        with self.onboarding._connect() as connection:
            connection.execute("ALTER TABLE brain_context_snapshots DROP COLUMN current_tools_json")
        migrated = ModuleOneOnboardingStore(self.helper.database, capability_secret=self.helper.secret)
        self.assertEqual(self.helper.catalog, migrated.current_advertised_tools(**self.helper.common))
        self.assertIsNone(self.snapshot()["current_tools_json"])

    def test_gateway_control_and_executor_follow_changes_in_one_wake(self):
        from rikkahub_gateway.server import GatewayApplication
        from rikkahub_gateway.tool_execution import NativeToolCall
        from rikkahub_gateway.tests import test_gateway as gateway_fixtures
        from rikkahub_gateway.tests.test_gateway_execution_recovery import st_tool
        from rikkahub_gateway.tests.test_gateway_control_integration import DirectControlClient
        app = GatewayApplication(replace(gateway_fixtures.config(), require_execution_binding=True,
            execution_epoch="synthetic-epoch"),
            control=DirectControlClient(self.control, self.host_token), upstream=object())
        user = {"role": "user", "content": "synthetic catalog refresh"}
        headers = {"X-ST-Thread-ID": "synthetic-integration"}
        prepared = app.prepare_turn({"messages": [user], "tools": [st_tool()]}, headers)
        wake_id, context_hash = prepared.session.wake_id, prepared.session.context_hash
        messages = [user]
        for index, next_tools in enumerate(([st_tool("renamed_stbrain_open")], [])):
            name = "stbrain_open" if index == 0 else "renamed_stbrain_open"
            original = NativeToolCall(f"integration-{index}", name, '{"view":"summary"}')
            issued = app.decorate_execution_calls(prepared, [original])[0]
            binding = app.bind_response_tool_calls(prepared, [issued])
            app.finish_turn(prepared, keep_for_tools=True,
                tool_call_ids=[issued.tool_call_id], tool_call_bindings=binding)
            args = json.loads(issued.arguments_text)
            claim = self.helper.store.claim(**self.helper.common,
                execution_ref=args.pop("execution_ref"), tool_name="stbrain_open", arguments=args)
            self.helper.store.finish(claim)
            messages = messages + [
                {"role": "assistant", "tool_calls": [gateway_fixtures.GatewayTests.wire_call(issued)]},
                {"role": "tool", "tool_call_id": issued.tool_call_id, "content": "synthetic result"}]
            prepared = app.prepare_turn({"messages": messages, "tools": next_tools}, headers)
            encoded = json.loads(app.encode_upstream_payload(prepared.payload, session=prepared.session))
            self.assertNotIn("stexec_", json.dumps(encoded))
            self.assertIn("stexec_", json.dumps(prepared.payload))
            self.assertEqual(next_tools, prepared.payload["tools"])
            self.assertEqual((wake_id, context_hash), (prepared.session.wake_id, prepared.session.context_hash))
        self.assertEqual(2, len(prepared.session.host_receipts))
        self.assertEqual([], self.onboarding.current_advertised_tools(**self.helper.common)["entries"])
