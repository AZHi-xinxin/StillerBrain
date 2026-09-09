"""Real FastMCP dispatch and lease registry against a synthetic temporary DB."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp_server.execution_guard import install_execution_guard
from runtime.execution_binding import ExecutionStore, canonical_hash, current_execution_claim
from runtime.onboarding import ModuleOneOnboardingStore


class ExecutionGuardTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="synthetic-mcp-lease-"))
        db = Path(directory) / "test.sqlite3"
        self.common = {"owner_id": "synthetic-owner", "model_id": "synthetic-model"}
        self.secret = "synthetic-secret-0000000000000000000000"
        self.onboarding = ModuleOneOnboardingStore(db, capability_secret=self.secret)
        self.store = ExecutionStore(db, deployment_epoch="synthetic-epoch", capability_secret=self.secret)
        self.mcp = FastMCP("synthetic")
        self.calls = []

        @self.mcp.tool()
        async def stbrain_open(view: str = "summary") -> dict[str, Any]:
            self.calls.append((view, current_execution_claim()))
            if view == "raise":
                raise ValueError("synthetic business failure")
            return {"decision": "opened", "view": view,
                    "bound": current_execution_claim() is not None}

        @self.mcp.tool()
        async def recall_planning_memory(query: str = "") -> dict[str, Any]:
            return {"decision": "no_candidate", "plans": []}

        @self.mcp.tool()
        async def remember_memory(module: str, content: str, write_context_ref: str | None = None) -> dict[str, Any]:
            self.calls.append((module, current_execution_claim()))
            return {"decision": "stored"}

        install_execution_guard(self.mcp, store=self.store, onboarding=self.onboarding, **self.common)
        entries = [{"canonical_name": t.name, "schema_hash": canonical_hash(t.parameters)}
                   for t in self.mcp._tool_manager.list_tools()]
        self.entries = {entry["canonical_name"]: entry for entry in entries}
        self.catalog = {"contract": "advertised-tools/1", "catalog_complete": True,
                        "catalog_hash": canonical_hash(entries), "entries": entries}
        self.wake = self.onboarding.issue_wake(**self.common, host_id="synthetic-host",
            thread_id="synthetic-thread", source_kind="human_message", source_event_id="synthetic-A")
        prepared = self.onboarding.build_pre_generation_context(**self.common,
            wake_id=self.wake["wake_id"], wake_capability=self.wake["wake_capability"],
            source_digest="synthetic-source", host_contract_digest="synthetic-host-contract",
            advertised_tools=self.catalog)
        self.onboarding.confirm_context_injected(**self.common, wake_id=self.wake["wake_id"],
            wake_capability=self.wake["wake_capability"], context_hash=prepared["context_hash"])
        self.batch = {**self.common, "wake_id": self.wake["wake_id"],
                      "wake_capability": self.wake["wake_capability"], "batch_id": "synthetic-batch", "revision": 1}

    def issue(self, name="stbrain_open", args=None):
        result = self.store.issue_batch(**self.batch, calls=[{
            "call_id": "synthetic-call", "canonical_tool": name, "advertised_name": name,
            "schema_hash": self.entries[name]["schema_hash"], "catalog_hash": self.catalog["catalog_hash"],
            "arguments_hash": canonical_hash(args or {})}])
        return result["executions"][0]["execution_ref"]

    def call(self, name, args):
        result = asyncio.run(self.mcp.call_tool(name, args))
        self.assertIsInstance(result, tuple)
        blocks, structured = result
        self.assertEqual(structured, json.loads(blocks[0].text))
        return structured

    def test_raw_arguments_bound_before_defaults_and_reserved_field_not_business_argument(self):
        ref = self.issue()
        result = self.call("stbrain_open", {"execution_ref": ref})
        self.assertEqual("summary", result["view"])
        self.assertTrue(result["bound"])
        self.assertEqual(1, self.store.batch_status(**self.batch)["counts"]["completed"])
        self.assertIsNone(current_execution_claim())

    def test_missing_binding_rejects_without_invoking_open(self):
        result = self.call("stbrain_open", {})
        self.assertEqual("execution_binding_required", result["reason_code"])
        self.assertEqual([], self.calls)

    def test_wrong_arguments_or_tool_do_not_consume_or_execute(self):
        ref = self.issue()
        result = self.call("stbrain_open", {"execution_ref": ref, "view": "manual"})
        self.assertEqual("reject", result["decision"])
        other = self.call("remember_memory", {"execution_ref": ref, "module": "planning_memory", "content": "synthetic"})
        self.assertEqual("reject", other["decision"])
        self.assertEqual([], self.calls)
        self.assertEqual(1, self.store.batch_status(**self.batch)["counts"]["issued"])

    def test_success_cannot_replay(self):
        ref = self.issue()
        self.call("stbrain_open", {"execution_ref": ref})
        self.assertEqual("reject", self.call("stbrain_open", {"execution_ref": ref})["decision"])
        self.assertEqual(1, len(self.calls))

    def test_exception_finishes_failed_and_context_does_not_leak(self):
        ref = self.issue(args={"view": "raise"})
        with self.assertRaises(Exception):
            self.call("stbrain_open", {"view": "raise", "execution_ref": ref})
        self.assertEqual(1, self.store.batch_status(**self.batch)["counts"]["failed"])
        self.assertIsNone(current_execution_claim())

    def test_revoked_late_call_cannot_open_next_wake(self):
        ref = self.issue()
        self.store.revoke_batch(**self.batch)
        self.onboarding.close_context_snapshot(**self.common, wake_id=self.wake["wake_id"], wake_capability=self.wake["wake_capability"])
        self.onboarding.issue_wake(**self.common, host_id="synthetic-host", thread_id="synthetic-thread",
            source_kind="human_message", source_event_id="synthetic-B")
        self.assertEqual("reject", self.call("stbrain_open", {"execution_ref": ref})["decision"])
        self.assertEqual([], self.calls)

    def test_plain_read_does_not_require_write_authorization(self):
        self.assertEqual("no_candidate", self.call("recall_planning_memory", {"query": "synthetic"})["decision"])

    def test_invented_direct_ref_does_not_bypass_binding(self):
        result = self.call("remember_memory", {"module": "planning_memory", "content": "synthetic", "write_context_ref": "synthetic-fake-ref"})
        self.assertEqual("reject", result["decision"])
        self.assertEqual([], self.calls)

    def test_catalog_marks_exact_host_owned_field_and_minimum_daily_args(self):
        schema = self.mcp._tool_manager.get_tool("remember_memory").parameters
        self.assertEqual(["module", "content"], schema["required"])
        prop = schema["properties"]["execution_ref"]
        self.assertEqual("remember_memory", prop["x-stbrain-execution-tool"])
        self.assertEqual("st-execution/1", prop["x-stbrain-execution-contract"])


if __name__ == "__main__":
    unittest.main()
