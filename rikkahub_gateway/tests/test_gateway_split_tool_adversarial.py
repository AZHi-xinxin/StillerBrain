"""Independent negative-boundary review of split assistant tool declarations.

All control traffic and tool results are synthetic; no server is started.
"""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

from rikkahub_gateway.server import GatewayError
from rikkahub_gateway.tests import test_gateway as fixtures


class SplitToolDeclarationAdversarialTests(unittest.TestCase):
    request = staticmethod(fixtures.GatewayTests.request)
    bound_tools = staticmethod(fixtures.GatewayTests.bound_tools)
    native_call = staticmethod(fixtures.GatewayTests.native_call)
    wire_call = staticmethod(fixtures.GatewayTests.wire_call)
    arm_bound_calls = fixtures.GatewayTests.arm_bound_calls
    settle_then_arm = fixtures.GatewayTests.settle_then_arm
    merged_continuation_messages = fixtures.GatewayTests.merged_continuation_messages

    def setUp(self) -> None:
        fixtures.GatewayTests.setUp(self)
        self.addCleanup(self.app.upstream.close)

    def prepare_split_wait(self):
        settled = [self.native_call("settled-a", "synthetic old result")]
        current = [
            self.native_call("current-b", "synthetic command b"),
            self.native_call("current-c", "synthetic command c"),
        ]
        tools, user, results, session = self.settle_then_arm(settled, current)
        calls = [*settled, *current]
        messages = [
            user,
            *[
                {"role": "assistant", "content": None, "tool_calls": [self.wire_call(call)]}
                for call in calls
            ],
            *[
                {"role": "tool", "tool_call_id": call_id, "content": results[call_id]}
                for call_id in ("current-c", "settled-a", "current-b")
            ],
        ]
        return tools, user, results, session, calls, messages

    def assert_rejected_without_consuming(self, messages, tools, session) -> None:
        before_session = copy.deepcopy(session)
        before_control = copy.deepcopy(self.control.calls)
        with self.assertRaises(GatewayError) as caught:
            self.app.prepare_turn(self.request(messages, tools=tools), self.headers)
        self.assertEqual(409, caught.exception.status)
        self.assertIs(self.app._current_session, session)
        self.assertEqual(before_session, session)
        self.assertEqual(before_control, self.control.calls)

    def test_exact_split_settled_and_current_calls_preserve_input_and_arguments(self) -> None:
        tools, _, _, session, calls, messages = self.prepare_split_wait()
        messages[1]["content"] = ""
        messages[2]["reasoning_content"] = ""
        payload = self.request(messages, tools=tools)
        before = copy.deepcopy(payload)
        resumed = self.app.prepare_turn(payload, self.headers)
        self.assertEqual(before, payload)
        self.assertIs(resumed.session, session)
        self.assertEqual(1, self.control.seq)
        self.assertEqual([call.tool_call_id for call in calls], session.settled_tool_call_order)
        forwarded_calls = [
            call
            for message in resumed.payload["messages"]
            for call in message.get("tool_calls", [])
        ]
        self.assertEqual([self.wire_call(call) for call in calls], forwarded_calls)
        self.assertEqual(set(), session.expected_tool_call_ids)
        self.app.finish_turn(resumed, keep_for_tools=False)
        self.assertIsNone(self.app._current_session)

    def test_nonempty_or_structured_content_cannot_be_erased_to_merge(self) -> None:
        tools, _, _, session, _, original = self.prepare_split_wait()
        for content in ("do not erase", " ", [], {}, False, 0, [{"type": "text", "text": ""}]):
            with self.subTest(content=content):
                messages = copy.deepcopy(original)
                messages[2]["content"] = content
                self.assert_rejected_without_consuming(messages, tools, session)

    def test_reasoning_and_unknown_fields_cannot_be_discarded(self) -> None:
        tools, _, _, session, _, original = self.prepare_split_wait()
        missing_content = copy.deepcopy(original)
        del missing_content[2]["content"]
        self.assert_rejected_without_consuming(missing_content, tools, session)
        for field, value in (
            ("reasoning_content", "private synthetic reasoning"),
            ("refusal", "declined"),
            ("name", "other-speaker"),
            ("audio", {"id": "synthetic-audio"}),
            ("function_call", {"name": "different", "arguments": "{}"}),
        ):
            with self.subTest(field=field):
                messages = copy.deepcopy(original)
                messages[2][field] = value
                self.assert_rejected_without_consuming(messages, tools, session)

    def test_split_does_not_cross_other_roles_or_text_assistant(self) -> None:
        tools, _, _, session, _, original = self.prepare_split_wait()
        for barrier in (
            {"role": "user", "content": "new intent"},
            {"role": "system", "content": "synthetic instruction"},
            {"role": "developer", "content": "synthetic instruction"},
            {"role": "assistant", "content": "separate answer"},
            {"role": "tool", "tool_call_id": "settled-a", "content": "{}"},
        ):
            with self.subTest(barrier=barrier["role"]):
                messages = copy.deepcopy(original)
                messages.insert(3, barrier)
                self.assert_rejected_without_consuming(messages, tools, session)

    def test_missing_duplicate_or_unexpected_result_does_not_authorize_partial_batch(self) -> None:
        tools, _, _, session, _, original = self.prepare_split_wait()
        variants = []
        missing = copy.deepcopy(original)
        missing.pop()
        variants.append(missing)
        duplicate = copy.deepcopy(original)
        duplicate.append(copy.deepcopy(duplicate[-1]))
        variants.append(duplicate)
        unexpected = copy.deepcopy(original)
        unexpected[-1]["tool_call_id"] = "not-issued"
        variants.append(unexpected)
        for index, messages in enumerate(variants):
            with self.subTest(index=index):
                self.assert_rejected_without_consuming(messages, tools, session)

    def test_reordered_declarations_and_settled_result_tampering_remain_rejected(self) -> None:
        tools, user, results, session, calls, original = self.prepare_split_wait()
        reordered = copy.deepcopy(original)
        reordered[2], reordered[3] = reordered[3], reordered[2]
        self.assert_rejected_without_consuming(reordered, tools, session)
        tampered = copy.deepcopy(original)
        tampered[5]["content"] = "changed settled result"
        self.assert_rejected_without_consuming(tampered, tools, session)
        changed_arguments = copy.deepcopy(original)
        changed_arguments[2]["tool_calls"][0]["function"]["arguments"] = '{"command":"changed"}'
        self.assert_rejected_without_consuming(changed_arguments, tools, session)
        # The retained wait must remain usable by the original, exact lineage.
        valid = self.app.prepare_turn(
            self.request(self.merged_continuation_messages(user, calls, results), tools=tools),
            self.headers,
        )
        self.assertIs(valid.session, session)
        self.assertEqual(1, self.control.seq)
        self.app.finish_turn(valid, keep_for_tools=False)

    def test_lineage_diagnostic_contains_only_fixed_fields_and_counts(self) -> None:
        tools, _, _, session, _, messages = self.prepare_split_wait()
        messages.pop()
        with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as captured:
            self.assert_rejected_without_consuming(messages, tools, session)
        self.assertEqual(1, len(captured.records))
        encoded = captured.records[0].getMessage()
        record = json.loads(encoded)
        self.assertEqual("tool_continuation_rejected", record["event"])
        self.assertEqual("declaration_result_set_mismatch", record["reason"])
        self.assertLessEqual(
            set(record),
            {
                "at", "event", "code", "reason", "declared_count", "result_count",
                "missing_result_count", "unexpected_result_count", "expected_count",
            },
        )
        for marker in (
            "settled-a", "current-b", "current-c", "synthetic command", "exitCode",
            "workspace_shell", "thread:test", *session.protected_values,
        ):
            self.assertNotIn(marker, encoded)

    def test_protected_value_collision_suppresses_branch_log(self) -> None:
        tools, _, _, session, _, messages = self.prepare_split_wait()
        messages.pop()
        session.protected_values.add("tool_continuation_lineage_mismatch")
        with patch("rikkahub_gateway.server._FAILURE_LOGGER.warning") as warning:
            self.assert_rejected_without_consuming(messages, tools, session)
        warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
