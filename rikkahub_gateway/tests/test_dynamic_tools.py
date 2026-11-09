"""Tool availability changes between requests; issued calls keep their identity."""
import copy
import unittest
from unittest.mock import patch

from rikkahub_gateway.server import GatewayError, _advertised_tool_catalog
from rikkahub_gateway.tool_execution import NativeToolCall
from rikkahub_gateway.tests import test_gateway as fixtures


class DynamicToolsTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixtures.GatewayTests()
        self.helper.setUp()
        self.app = self.helper.app
        self.headers = self.helper.headers
        self.user = {"role": "user", "content": "synthetic dynamic tools"}
        self.tools = self.helper.bound_tools()
        self.call = self.helper.native_call("old-call", "synthetic")
        self.first = self.app.prepare_turn(
            self.helper.request([self.user], tools=self.tools), self.headers)
        self.helper.arm_bound_calls(self.first, [self.call])
        self.history = self.helper.merged_continuation_messages(
            self.user, [self.call], {self.call.tool_call_id: "old result"})

    def continue_with(self, tools, history=None):
        return self.app.prepare_turn(
            self.helper.request(self.history if history is None else history, tools=tools),
            self.headers)

    def changed_schema(self):
        tools = copy.deepcopy(self.tools)
        tools[0]["function"]["parameters"]["properties"]["command"] = {"type": "integer"}
        return tools

    def test_add_tool_and_keep_the_same_wake_and_memory(self):
        added = copy.deepcopy(self.tools[0])
        added["function"]["name"] = "new_tool"
        tools = self.tools + [added]
        before = (self.first.session.wake_id, self.first.session.context_hash,
                  self.first.session.source_digest, copy.deepcopy(self.first.session.message))
        turn = self.continue_with(tools)
        self.assertTrue(turn.continuation)
        self.assertEqual(tools, turn.payload["tools"])
        self.assertEqual(before, (turn.session.wake_id, turn.session.context_hash,
                                 turn.session.source_digest, turn.session.message))
        self.assertEqual(1, self.helper.control.seq)

    def test_remove_all_tools_while_accepting_old_results(self):
        turn = self.continue_with([])
        self.assertEqual([], turn.payload["tools"])
        self.assertEqual([], turn.session.advertised_tools["entries"])
        with self.assertRaises(GatewayError):
            self.app.bind_response_tool_calls(turn, [self.helper.native_call("removed", "x")])

    def test_omitted_tools_are_an_empty_current_catalog(self):
        turn = self.app.prepare_turn(self.helper.request(self.history), self.headers)
        self.assertNotIn("tools", turn.payload)
        self.assertEqual([], turn.session.advertised_tools["entries"])

    def test_rename_accepts_old_result_and_only_new_name_for_next_call(self):
        tools = copy.deepcopy(self.tools)
        tools[0]["function"]["name"] = "renamed_shell"
        turn = self.continue_with(tools)
        renamed = NativeToolCall("renamed-call", "renamed_shell", '{"command":"new"}')
        binding = self.app.bind_response_tool_calls(turn, [renamed])[renamed.tool_call_id]
        self.assertEqual("renamed_shell", binding.tool_name)
        with self.assertRaises(GatewayError):
            self.app.bind_response_tool_calls(turn, [self.helper.native_call("removed", "x")])

    def test_schema_changes_apply_to_new_calls_only(self):
        turn = self.continue_with(self.changed_schema())
        old_receipt = turn.session.host_receipts[-1]
        self.assertEqual(_advertised_tool_catalog({"tools": self.tools})["catalog_hash"],
                         old_receipt["catalog_hash"])
        valid = NativeToolCall("new-call", "workspace_shell", '{"command":7}')
        binding = self.app.bind_response_tool_calls(turn, [valid])[valid.tool_call_id]
        self.assertEqual(turn.session.advertised_tools["catalog_hash"], binding.catalog_hash)
        with self.assertRaises(GatewayError):
            self.app.bind_response_tool_calls(turn, [self.helper.native_call("invalid-new", "old type")])

    def test_multiple_changes_and_merged_old_history_do_not_reconsume_results(self):
        turn = self.continue_with(self.changed_schema())
        next_call = NativeToolCall("second-call", "workspace_shell", '{"command":7}')
        self.helper.arm_bound_calls(turn, [next_call])
        merged = self.helper.merged_continuation_messages(self.user, [self.call, next_call],
            {self.call.tool_call_id: "old result", next_call.tool_call_id: "second result"})
        continued = self.continue_with([], merged)
        self.assertTrue(continued.continuation)
        self.assertEqual(["old-call", "second-call"], continued.session.settled_tool_call_order)
        self.assertEqual(2, len(continued.session.host_receipts))

    def test_mutated_settled_result_is_rejected_even_when_tools_change(self):
        turn = self.continue_with(self.changed_schema())
        next_call = NativeToolCall("second-call", "workspace_shell", '{"command":7}')
        self.helper.arm_bound_calls(turn, [next_call])
        merged = self.helper.merged_continuation_messages(self.user, [self.call, next_call],
            {self.call.tool_call_id: "altered old result", next_call.tool_call_id: "second result"})
        with self.assertRaises(GatewayError):
            self.continue_with([], merged)
        self.assertEqual({"second-call"}, turn.session.expected_tool_call_ids)

    def test_failed_catalog_update_can_retry_without_consuming_the_batch(self):
        original = self.helper.control.post
        def fail_update(path, payload):
            if path == "/v1/host/context/tools":
                raise GatewayError(503, "synthetic_control_unavailable")
            return original(path, payload)
        with patch.object(self.helper.control, "post", side_effect=fail_update):
            with self.assertRaises(GatewayError):
                self.continue_with([])
        self.assertEqual({"old-call"}, self.first.session.expected_tool_call_ids)
        self.assertEqual([], self.first.session.host_receipts)
        turn = self.continue_with([])
        self.assertEqual(1, len(turn.session.host_receipts))

    def test_reorder_and_description_edits_need_no_catalog_update(self):
        tools = copy.deepcopy(self.tools)
        tools[0]["function"]["description"] = "new description"
        turn = self.continue_with(tools)
        self.assertEqual(tools, turn.payload["tools"])
        self.assertFalse(any(path == "/v1/host/context/tools" for path, _ in self.helper.control.calls))

    def test_lost_update_ack_then_retry_old_catalog_resynchronizes_control(self):
        original = self.helper.control.post
        persisted = {}
        def commit_then_lose_ack(path, payload):
            result = original(path, payload)
            if path == "/v1/host/context/tools":
                persisted.update(payload["advertised_tools"])
                raise GatewayError(504, "synthetic_lost_ack")
            return result
        with patch.object(self.helper.control, "post", side_effect=commit_then_lose_ack):
            with self.assertRaises(GatewayError):
                self.continue_with([])
        self.assertEqual([], persisted["entries"])
        self.assertTrue(self.first.session.tool_catalog_sync_pending)
        self.assertEqual({"old-call"}, self.first.session.expected_tool_call_ids)
        turn = self.continue_with(self.tools)
        updates = [payload for path, payload in self.helper.control.calls if path == "/v1/host/context/tools"]
        self.assertEqual(2, len(updates))
        self.assertEqual(_advertised_tool_catalog({"tools": self.tools}), updates[-1]["advertised_tools"])
        self.assertFalse(turn.session.tool_catalog_sync_pending)
        self.assertEqual(1, len(turn.session.host_receipts))
