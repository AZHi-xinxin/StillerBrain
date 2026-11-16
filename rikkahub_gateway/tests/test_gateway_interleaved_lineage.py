"""Synthetic regression for a parallel Shell + stbrain_open split into A/T units."""
from __future__ import annotations

import copy
import json
import os
import unittest
from unittest.mock import patch

from rikkahub_gateway.server import GatewayApplication, GatewayError, _lineage_error
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tool_execution import NativeToolCall


class _NoEnvironment(dict):
    def _deny(self, *args, **kwargs):
        raise AssertionError("environment access prohibited")
    __getitem__ = get = __iter__ = keys = items = values = __contains__ = _deny


class _NoUpstream:
    def __getattr__(self, name):
        raise AssertionError("real upstream prohibited")


class GatewayInterleavedLineageTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "sqlite3.connect",
                       "os.getenv", "subprocess.Popen"):
            guard = patch(target, side_effect=AssertionError("external access prohibited"))
            blocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(blocked.assert_not_called)
        env = patch.object(os, "environ", _NoEnvironment())
        env.start()
        self.addCleanup(env.stop)
        self.control = fixtures.FakeControl()
        self.app = GatewayApplication(fixtures.config(), control=self.control, upstream=_NoUpstream())
        self.headers = {"X-ST-Thread-ID": "synthetic:mixed-parallel"}
        self.user = {"role": "user", "content": "synthetic test, not a real command"}
        self.tools = fixtures.GatewayTests.bound_tools() + [{"type": "function", "function": {
            "name": "stbrain_open", "parameters": {
                "type": "object", "properties": {
                    "view": {"type": "string", "enum": ["summary", "manual"], "default": "summary"},
                }, "additionalProperties": False}}}]
        self.calls = [
            NativeToolCall("synthetic-local", "workspace_shell", '{"command":"synthetic-only"}'),
            NativeToolCall("synthetic-native", "stbrain_open", "{}"),
        ]
        prepared = self.prepare([self.user])
        self.session = prepared.session
        bindings = self.app.bind_response_tool_calls(prepared, self.calls)
        self.app.finish_turn(prepared, keep_for_tools=True,
                             tool_call_ids=list(bindings), tool_call_bindings=bindings)

    def prepare(self, messages):
        return self.app.prepare_turn(fixtures.GatewayTests.request(messages, tools=self.tools), self.headers)

    def history(self):
        messages = [copy.deepcopy(self.user)]
        for index, call in enumerate(self.calls):
            messages.extend([
                {"role": "assistant", "content": "synthetic assistant segment " + str(index),
                 "reasoning_content": "synthetic reasoning " + str(index),
                 "tool_calls": [fixtures.GatewayTests.wire_call(call)]},
                {"role": "tool", "tool_call_id": call.tool_call_id, "name": call.tool_name,
                 "content": '{"exitCode":0}' if index == 0 else '{"synthetic_summary":true}'},
            ])
        return messages

    def reject(self, messages):
        original = copy.deepcopy(messages)
        before = copy.deepcopy(self.session)
        before_control = copy.deepcopy(self.control.calls)
        with patch("rikkahub_gateway.server._FAILURE_LOGGER.warning") as log:
            with self.assertRaises(GatewayError) as caught:
                self.prepare(messages)
        self.assertEqual(409, caught.exception.status)
        self.assertEqual(before, self.session)
        self.assertEqual(before_control, self.control.calls)
        self.assertEqual(original, messages)
        return caught.exception, [json.loads(call.args[1]) for call in log.call_args_list]

    def test_shell_and_native_open_complete_interleaving_keeps_every_message(self):
        messages = self.history()
        original = copy.deepcopy(messages)
        turn = self.prepare(messages)
        self.assertIs(turn.session, self.session)
        self.assertEqual(original, messages)
        self.assertEqual(original, turn.payload["messages"][-len(original):])
        self.assertEqual([call.tool_call_id for call in self.calls], self.session.settled_tool_call_order)
        self.assertEqual(2, len(self.session.host_receipts))
        self.app.finish_turn(turn, keep_for_tools=False)
        self.assertIsNone(self.app._current_session)

    def test_structured_content_and_extra_metadata_remain_on_their_own_assistant(self):
        messages = self.history()
        messages[1].update(content=[{"type": "text", "text": ""}], name="synthetic-speaker",
                           refusal=None, audio={"id": "synthetic-audio"})
        messages[3]["content"] = []
        original = copy.deepcopy(messages)
        turn = self.prepare(messages)
        self.assertEqual(original, messages)
        self.assertEqual(original, turn.payload["messages"][-len(original):])
        self.assertNotIn("name", turn.payload["messages"][-2])

    def test_parser_is_pure_and_retains_result_objects_and_order(self):
        messages = self.history()
        original = copy.deepcopy(messages)
        calls, results = self.app._bound_continuation_tool_call_batch(messages)
        self.assertEqual(self.calls, calls)
        self.assertIs(messages[2], results[0])
        self.assertIs(messages[4], results[1])
        self.assertEqual(original, messages)
        self.assertEqual(0, len(self.session.host_receipts))



    def test_no_unit_borrows_a_result_from_the_next_unit(self):
        messages = self.history()
        messages[2]["tool_call_id"], messages[4]["tool_call_id"] = (
            messages[4]["tool_call_id"], messages[2]["tool_call_id"])
        error, records = self.reject(messages)
        self.assertEqual("tool_continuation_lineage_mismatch", error.code)
        self.assertEqual("declaration_result_set_mismatch", records[0]["reason"])

    def test_duplicate_id_across_complete_units_is_not_consumed_twice(self):
        messages = self.history()
        messages[3]["tool_calls"] = copy.deepcopy(messages[1]["tool_calls"])
        messages[4] = copy.deepcopy(messages[2])
        error, records = self.reject(messages)
        self.assertEqual("tool_continuation_lineage_invalid", error.code)
        self.assertEqual("result_id_duplicate", records[0]["reason"])

    def test_noncanonical_result_ids_are_not_silently_trimmed(self):
        messages = self.history()
        messages[2]["tool_call_id"] = " " + messages[2]["tool_call_id"] + " "
        error, records = self.reject(messages)
        self.assertEqual("tool_continuation_lineage_invalid", error.code)
        self.assertEqual("result_id_noncanonical", records[0]["reason"])

    def test_noncanonical_declaration_ids_are_not_silently_trimmed(self):
        messages = self.history()
        messages[1]["tool_calls"][0]["id"] = " " + self.calls[0].tool_call_id + " "
        error, records = self.reject(messages)
        self.assertEqual("tool_continuation_lineage_invalid", error.code)
        self.assertEqual("declaration_invalid", records[0]["reason"])




    def test_new_diagnostic_counters_are_bounded_and_protected(self):
        with patch("rikkahub_gateway.server._FAILURE_LOGGER.warning") as log:
            _lineage_error("tool_continuation_lineage_mismatch", "current_batch_order_mismatch",
                           terminal_declared_count=9000, segment_declared_count=True,
                           segment_result_count=-1)
        record = json.loads(log.call_args.args[1])
        self.assertEqual(4096, record["terminal_declared_count"])
        self.assertNotIn("segment_declared_count", record)
        self.assertNotIn("segment_result_count", record)
        with patch("rikkahub_gateway.server._FAILURE_LOGGER.warning") as log:
            _lineage_error("tool_continuation_lineage_mismatch", "current_batch_order_mismatch",
                           terminal_declared_count=1, protected_values=("terminal_declared_count",))
        log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
