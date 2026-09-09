"""Offline mixed-tool shape compatibility and value-free rejection diagnostics."""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from rikkahub_gateway.server import GatewayError, _lineage_error
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tool_execution import NativeToolCall


class GatewayMixedLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guards = [
            patch("socket.create_connection", side_effect=AssertionError("network prohibited")),
            patch("socket.socket.connect", side_effect=AssertionError("network prohibited")),
            patch("sqlite3.connect", side_effect=AssertionError("database prohibited")),
        ]
        self.blocked_calls = [guard.start() for guard in self.guards]
        self.helper = fixtures.GatewayTests()
        self.helper.setUp()
        self.tools = self.helper.bound_tools() + [
            {
                "type": "function",
                "function": {
                    "name": "fake_breath_search",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        self.user = {"role": "user", "content": "synthetic mixed chain"}
        self.calls = [
            NativeToolCall(
                f"synthetic-{index}",
                "workspace_shell" if index % 2 == 0 else "fake_breath_search",
                json.dumps({"command" if index % 2 == 0 else "query": f"synthetic-{index}"}),
            )
            for index in range(4)
        ]
        self.results = {call.tool_call_id: f"synthetic result {index}" for index, call in enumerate(self.calls)}

    def tearDown(self) -> None:
        self.helper.app.upstream.close()
        try:
            for blocked in self.blocked_calls:
                blocked.assert_not_called()
        finally:
            for guard in reversed(self.guards):
                guard.stop()

    def split(self, calls, *, results=None):
        values = self.results if results is None else results
        return [
            self.user,
            *[
                {"role": "assistant", "content": None, "tool_calls": [self.helper.wire_call(call)]}
                for call in calls
            ],
            *[
                {"role": "tool", "tool_call_id": call.tool_call_id, "content": values[call.tool_call_id]}
                for call in calls
            ],
        ]

    def arm(self, *, settled_count=1, current_count=1):
        prepared = self.helper.app.prepare_turn(
            self.helper.request([self.user], tools=self.tools), self.helper.headers
        )
        for index in range(settled_count):
            self.helper.arm_bound_calls(prepared, [self.calls[index]])
            prepared = self.helper.app.prepare_turn(
                self.helper.request(self.split(self.calls[: index + 1]), tools=self.tools),
                self.helper.headers,
            )
        self.helper.arm_bound_calls(
            prepared, self.calls[settled_count : settled_count + current_count]
        )
        return prepared

    def assert_rejected_without_consuming(self, prepared, messages, code, *, tools=None):
        session = prepared.session
        before = (
            set(session.expected_tool_call_ids), list(session.expected_tool_call_order),
            dict(session.expected_tool_calls), copy.deepcopy(session.host_receipts),
            list(session.settled_tool_call_order), session.tool_wait_armed_at,
        )
        control_count = len(self.helper.control.calls)
        with patch("rikkahub_gateway.server._FAILURE_LOGGER.warning") as logger:
            with self.assertRaises(GatewayError) as caught:
                self.helper.app.prepare_turn(
                    self.helper.request(messages, tools=self.tools if tools is None else tools),
                    self.helper.headers,
                )
        self.assertEqual(409, caught.exception.status)
        self.assertEqual(code, caught.exception.code)
        self.assertIs(self.helper.app._current_session, session)
        self.assertEqual(
            before,
            (set(session.expected_tool_call_ids), list(session.expected_tool_call_order),
             dict(session.expected_tool_calls), session.host_receipts,
             list(session.settled_tool_call_order), session.tool_wait_armed_at),
        )
        self.assertEqual(control_count, len(self.helper.control.calls))
        return [json.loads(call.args[1]) for call in logger.call_args_list]

    def test_two_and_three_batches_accept_split_mixed_tools(self) -> None:
        for count in (2, 3):
            with self.subTest(batches=count):
                self.arm(settled_count=count - 1)
                messages = self.split(self.calls[:count])
                messages[1]["content"] = ""
                messages[1]["reasoning_content"] = ""
                original = copy.deepcopy(messages)
                continued = self.helper.app.prepare_turn(
                    self.helper.request(messages, tools=self.tools), self.helper.headers
                )
                self.assertTrue(continued.continuation)
                self.assertEqual(original, messages)
                forwarded = continued.payload["messages"]
                assistants = [item for item in forwarded if item["role"] == "assistant"]
                self.assertEqual(1, len(assistants))
                self.assertEqual(
                    [self.helper.wire_call(call) for call in self.calls[:count]],
                    assistants[0]["tool_calls"],
                )
                self.assertEqual(
                    [item for item in original if item["role"] == "tool"],
                    [item for item in forwarded if item["role"] == "tool"],
                )
                self.assertEqual(count, len(continued.session.host_receipts))
                self.helper.app.finish_turn(continued, keep_for_tools=False)

    def test_current_parallel_batch_preserves_declaration_order_and_result_values(self) -> None:
        self.arm(current_count=2)
        messages = self.split(self.calls[:3])
        messages[-3:] = list(reversed(messages[-3:]))
        continued = self.helper.app.prepare_turn(
            self.helper.request(messages, tools=self.tools), self.helper.headers
        )
        self.assertEqual([call.tool_call_id for call in self.calls[:3]], continued.session.settled_tool_call_order)
        self.assertEqual(messages[-3:], continued.payload["messages"][-3:])

    def test_cancel_text_is_a_result_not_a_lineage_signal(self) -> None:
        self.arm()
        results = {**self.results, self.calls[1].tool_call_id: "Generation cancelled by user"}
        continued = self.helper.app.prepare_turn(
            self.helper.request(self.split(self.calls[:2], results=results), tools=self.tools),
            self.helper.headers,
        )
        self.assertEqual("unknown", continued.session.host_receipts[-1]["completion_state"])

    def test_missing_result_is_not_normalized_and_next_human_stays_rejected(self) -> None:
        prepared = self.arm()
        messages = [item for item in self.split(self.calls[:2]) if not (
            item.get("role") == "tool" and item.get("tool_call_id") == self.calls[1].tool_call_id
        )]
        self.assertIs(messages, self.helper.app._normalize_split_tool_declarations(messages))
        self.assert_rejected_without_consuming(prepared, messages, "tool_continuation_lineage_mismatch")
        with self.assertRaises(GatewayError) as caught:
            self.helper.app.prepare_turn(
                self.helper.request([{"role": "user", "content": "next synthetic human"}], tools=self.tools),
                self.helper.headers,
            )
        self.assertEqual("human_turn_in_progress", caught.exception.code)

    def test_missing_settled_result_does_not_rewrite_original_history(self) -> None:
        messages = [item for item in self.split(self.calls[:2]) if not (
            item.get("role") == "tool" and item.get("tool_call_id") == self.calls[0].tool_call_id
        )]
        original = copy.deepcopy(messages)
        self.assertIs(messages, self.helper.app._normalize_split_tool_declarations(messages))
        self.assertEqual(original, messages)

    def test_reordered_current_batch_reports_fixed_branch(self) -> None:
        prepared = self.arm(current_count=2)
        messages = self.split([self.calls[0], self.calls[2], self.calls[1]])
        records = self.assert_rejected_without_consuming(prepared, messages, "tool_continuation_lineage_mismatch")
        self.assertEqual("current_batch_order_mismatch", records[0]["reason"])

    def test_non_suffix_settled_prefix_reports_fixed_branch(self) -> None:
        prepared = self.arm(settled_count=2)
        messages = self.split([self.calls[0], self.calls[2]])
        records = self.assert_rejected_without_consuming(prepared, messages, "tool_continuation_lineage_mismatch")
        self.assertEqual("settled_prefix_order_mismatch", records[0]["reason"])

    def test_duplicate_result_is_not_normalized(self) -> None:
        prepared = self.arm()
        messages = self.split(self.calls[:2])
        messages.append(copy.deepcopy(messages[-1]))
        self.assertIs(messages, self.helper.app._normalize_split_tool_declarations(messages))
        records = self.assert_rejected_without_consuming(prepared, messages, "tool_continuation_lineage_invalid")
        self.assertEqual("result_id_duplicate", records[0]["reason"])

    def test_nonempty_or_unrecognized_assistant_fields_are_not_normalized(self) -> None:
        prepared = self.arm()
        for update in ({"content": "retain this text"}, {"reasoning_content": "retain reasoning"}, {"name": "metadata"}):
            with self.subTest(update=update):
                messages = self.split(self.calls[:2])
                messages[1].update(update)
                self.assertIs(messages, self.helper.app._normalize_split_tool_declarations(messages))
                self.assert_rejected_without_consuming(prepared, messages, "tool_continuation_lineage_mismatch")

    def test_missing_content_field_is_not_normalized(self) -> None:
        prepared = self.arm()
        messages = self.split(self.calls[:2])
        del messages[1]["content"]
        self.assertIs(messages, self.helper.app._normalize_split_tool_declarations(messages))
        self.assert_rejected_without_consuming(prepared, messages, "tool_continuation_lineage_mismatch")

    def test_normalized_chain_still_checks_catalog_name_arguments_and_receipt(self) -> None:
        prepared = self.arm()
        messages = self.split(self.calls[:2])
        changed_tools = copy.deepcopy(self.tools)
        changed_tools[1]["function"]["parameters"]["properties"]["extra"] = {"type": "string"}
        self.assert_rejected_without_consuming(prepared, messages, "tool_continuation_catalog_mismatch", tools=changed_tools)
        for field, value in (("name", "workspace_shell"), ("arguments", json.dumps({"query": "changed"}))):
            altered = copy.deepcopy(messages)
            altered[2]["tool_calls"][0]["function"][field] = value
            self.assert_rejected_without_consuming(prepared, altered, "tool_continuation_binding_mismatch")
        altered_result = copy.deepcopy(messages)
        altered_result[-2]["content"] = "changed settled result"
        self.assert_rejected_without_consuming(prepared, altered_result, "tool_continuation_settled_replay_mismatch")

    def test_diagnostic_contains_only_fixed_branch_and_counts(self) -> None:
        prepared = self.arm()
        messages = self.helper.merged_continuation_messages(self.user, self.calls[:2], self.results)
        messages.pop()
        records = self.assert_rejected_without_consuming(prepared, messages, "tool_continuation_lineage_mismatch")
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("declaration_result_set_mismatch", record["reason"])
        self.assertEqual((2, 1, 1, 0), tuple(record[key] for key in (
            "declared_count", "result_count", "missing_result_count", "unexpected_result_count"
        )))
        self.assertEqual({"event", "at", "code", "reason", "declared_count", "result_count", "missing_result_count", "unexpected_result_count"}, set(record))
        encoded = json.dumps(record)
        for value in ("workspace_shell", "fake_breath_search", *self.results, *self.results.values(), prepared.session.wake_capability):
            self.assertNotIn(value, encoded)

    def test_diagnostic_filters_unknown_reason_and_protected_collision(self) -> None:
        with patch("rikkahub_gateway.server._FAILURE_LOGGER.warning") as logger:
            _lineage_error("tool_continuation_lineage_invalid", "untrusted reason", declared_count=100000, result_count=True)
        record = json.loads(logger.call_args.args[1])
        self.assertEqual("lineage_validation_failed", record["reason"])
        self.assertEqual(4096, record["declared_count"])
        self.assertNotIn("result_count", record)
        self.assertNotIn("untrusted reason", json.dumps(record))
        with patch("rikkahub_gateway.server._FAILURE_LOGGER.warning") as logger:
            _lineage_error("tool_continuation_lineage_invalid", "result_id_missing", protected_values=("result_id_missing",))
        logger.assert_not_called()


if __name__ == "__main__":
    unittest.main()
