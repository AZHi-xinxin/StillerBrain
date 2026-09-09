"""Synthetic adversarial acceptance for Rikka's assistant/tool interleaving.

Shape reference (read-only archive, not evidence of the installed phone version):
verification/recovered-temp-20260829/rikkahub-8dc3-source.zip, commit
8dc3ebce3f422a422013097447234e4a97db4210. ProviderMessageUtils.kt:45-52
groups adjacent executed tools, while ChatCompletionsAPI.kt:466-505 emits an
assistant declaration and results per group. Even an empty text part separates
groups. All examples below are invented; no archived chat data is loaded.

No server, real upstream, tool executor, environment, or database is used.
"""

from __future__ import annotations

import copy
import json
import os
import unittest
from unittest.mock import patch

from rikkahub_gateway.server import GatewayApplication, GatewayError
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tool_execution import NativeToolCall


class _NoEnvironment(dict):
    def _deny(self, *args, **kwargs):
        raise AssertionError("environment access prohibited")

    __getitem__ = get = __iter__ = keys = items = values = __contains__ = _deny


class _NoUpstream:
    def __getattr__(self, name):
        raise AssertionError("upstream access prohibited")


class GatewayInterleavedAdversarialTests(unittest.TestCase):
    request = staticmethod(fixtures.GatewayTests.request)
    wire_call = staticmethod(fixtures.GatewayTests.wire_call)
    arm_bound_calls = fixtures.GatewayTests.arm_bound_calls

    def setUp(self) -> None:
        for target in (
            "socket.create_connection", "socket.socket", "sqlite3.connect",
            "os.getenv", "subprocess.Popen",
        ):
            guard = patch(target, side_effect=AssertionError("external access prohibited"))
            blocked = guard.start()
            self.addCleanup(guard.stop)
            self.addCleanup(blocked.assert_not_called)
        env_guard = patch.object(os, "environ", _NoEnvironment())
        env_guard.start()
        self.addCleanup(env_guard.stop)
        self.control = fixtures.FakeControl()
        self.app = GatewayApplication(
            fixtures.config(), control=self.control, upstream=_NoUpstream()
        )
        self.headers = {
            "Authorization": "Bearer " + fixtures.config().gateway_token,
            "X-ST-Thread-ID": "thread:synthetic-interleaved",
        }
        self.tools = fixtures.GatewayTests.bound_tools() + [{
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
        }]
        self.user = {"role": "user", "content": "synthetic acceptance only"}
        self.calls = [
            NativeToolCall(
                "synthetic-call-" + str(index),
                "workspace_shell" if index % 2 == 0 else "fake_breath_search",
                json.dumps({
                    "command" if index % 2 == 0 else "query": "synthetic-" + str(index)
                }),
            )
            for index in range(6)
        ]
        self.results = {
            call.tool_call_id: "synthetic result " + str(index)
            for index, call in enumerate(self.calls)
        }

    def prepare(self, messages, *, tools=None, headers=None):
        return self.app.prepare_turn(
            self.request(messages, tools=self.tools if tools is None else tools),
            self.headers if headers is None else headers,
        )

    def arm(self, calls=None, *, history=None):
        prepared = self.prepare([self.user] if history is None else history)
        self.arm_bound_calls(prepared, self.calls[:2] if calls is None else calls)
        return prepared.session

    def interleaved(self, calls, *, texts=None, reasoning=None, values=None):
        values = self.results if values is None else values
        messages = [copy.deepcopy(self.user)]
        for index, call in enumerate(calls):
            assistant = {
                "role": "assistant",
                "content": None if texts is None else texts[index],
                "tool_calls": [self.wire_call(call)],
            }
            if reasoning is not None:
                assistant["reasoning_content"] = reasoning[index]
            messages.extend([
                assistant,
                {"role": "tool", "name": call.tool_name,
                 "tool_call_id": call.tool_call_id, "content": values[call.tool_call_id]},
            ])
        return messages

    def assert_rejected_without_consuming(self, messages, session, *, tools=None, headers=None):
        before = copy.deepcopy(session)
        before_control = copy.deepcopy(self.control.calls)
        before_messages = copy.deepcopy(messages)
        with patch("rikkahub_gateway.server._FAILURE_LOGGER.warning"):
            with self.assertRaises(GatewayError) as caught:
                self.prepare(messages, tools=tools, headers=headers)
        self.assertEqual(409, caught.exception.status)
        self.assertIs(self.app._current_session, session)
        self.assertEqual(before, session)
        self.assertEqual(before_control, self.control.calls)
        self.assertEqual(before_messages, messages)
        return caught.exception.code

    def assert_forwarded_intact(self, messages, continued):
        # Only the gateway's independently injected system context may be new.
        self.assertEqual(messages, continued.payload["messages"][1:])
        self.assertTrue(continued.continuation)

    def test_parallel_two_and_three_complete_interleaved_calls_are_accepted(self):
        for count in (2, 3):
            with self.subTest(count=count):
                session = self.arm(self.calls[:count])
                messages = self.interleaved(self.calls[:count])
                original = copy.deepcopy(messages)
                continued = self.prepare(messages)
                self.assert_forwarded_intact(original, continued)
                self.assertEqual(original, messages)
                self.assertIs(session, continued.session)
                self.assertEqual(count, len(session.host_receipts))
                self.assertEqual([c.tool_call_id for c in self.calls[:count]], session.settled_tool_call_order)
                self.assertEqual(set(), session.expected_tool_call_ids)
                self.app.finish_turn(continued, keep_for_tools=False)

    def test_each_segment_text_and_reasoning_are_forwarded_without_merging(self):
        self.arm(self.calls[:3])
        messages = self.interleaved(
            self.calls[:3], texts=["synthetic first text", "", "synthetic later text"],
            reasoning=["synthetic reasoning one", None, "synthetic reasoning three"],
        )
        original = copy.deepcopy(messages)
        self.assert_forwarded_intact(original, self.prepare(messages))
        self.assertEqual(original, messages)

    def test_first_segment_can_contain_all_prose_without_reassigning_it(self):
        self.arm()
        messages = self.interleaved(
            self.calls[:2], texts=["synthetic first segment only", None],
            reasoning=["synthetic first reasoning only", ""],
        )
        self.assert_forwarded_intact(copy.deepcopy(messages), self.prepare(messages))

    def test_group_of_two_followed_by_singleton_is_one_exact_parallel_batch(self):
        self.arm(self.calls[:3])
        messages = self.interleaved(self.calls[:3])
        messages[1]["tool_calls"].extend(messages[3]["tool_calls"])
        del messages[3]
        self.assert_forwarded_intact(copy.deepcopy(messages), self.prepare(messages))

    def test_results_within_one_group_keep_their_original_wire_order(self):
        self.arm(self.calls[:3])
        messages = self.interleaved(self.calls[:3])
        messages[1]["tool_calls"].extend(messages[3]["tool_calls"])
        del messages[3]
        messages[2], messages[3] = messages[3], messages[2]
        self.assert_forwarded_intact(copy.deepcopy(messages), self.prepare(messages))

    def test_nonempty_adjacent_declarations_are_not_silently_reinterpreted(self):
        session = self.arm()
        for field in ("content", "reasoning_content"):
            with self.subTest(field=field):
                interleaved = self.interleaved(self.calls[:2])
                messages = [interleaved[i] for i in (0, 1, 3, 2, 4)]
                messages[1][field] = "synthetic must not be moved or erased"
                self.assert_rejected_without_consuming(messages, session)

    def test_partial_final_or_earlier_segment_cannot_consume_the_wait(self):
        session = self.arm(self.calls[:3])
        original = self.interleaved(self.calls[:3])
        variants = [original[:3], original[:5], original[:-1]]
        missing_middle = copy.deepcopy(original)
        del missing_middle[4]
        variants.append(missing_middle)
        for index, messages in enumerate(variants):
            with self.subTest(index=index):
                self.assert_rejected_without_consuming(messages, session)
        self.assert_forwarded_intact(original, self.prepare(original))

    def test_user_system_developer_and_non_tool_assistant_are_hard_boundaries(self):
        session = self.arm()
        for barrier in (
            {"role": "user", "content": "synthetic next intent"},
            {"role": "system", "content": "synthetic system"},
            {"role": "developer", "content": "synthetic developer"},
            {"role": "assistant", "content": "synthetic separate answer"},
            {"role": "assistant", "content": "", "tool_calls": []},
        ):
            with self.subTest(barrier=barrier):
                messages = self.interleaved(self.calls[:2])
                messages.insert(3, barrier)
                self.assert_rejected_without_consuming(messages, session)

    def test_history_before_current_user_is_not_collected_or_rewritten(self):
        history = [
            {"role": "user", "content": "synthetic old user"},
            {"role": "assistant", "content": "synthetic old text",
             "tool_calls": [self.wire_call(self.calls[5])]},
            {"role": "tool", "tool_call_id": self.calls[5].tool_call_id,
             "content": "synthetic old result"},
            self.user,
        ]
        session = self.arm(history=history)
        messages = copy.deepcopy(history[:-1]) + self.interleaved(self.calls[:2])
        original = copy.deepcopy(messages)
        self.assert_forwarded_intact(original, self.prepare(messages))
        self.assertEqual(2, len(session.host_receipts))
        self.assertNotIn(self.calls[5].tool_call_id, session.settled_tool_call_order)

    def test_complete_batch_after_separate_assistant_does_not_absorb_older_tools(self):
        self.arm()
        prefix = self.interleaved(self.calls[4:5])[1:]
        prefix.append({"role": "assistant", "content": "synthetic boundary"})
        messages = [self.user, *prefix, *self.interleaved(self.calls[:2])[1:]]
        self.assert_forwarded_intact(copy.deepcopy(messages), self.prepare(messages))

    def test_duplicate_unknown_and_swapped_result_identifiers_are_rejected(self):
        session = self.arm()
        variants = []
        duplicate = self.interleaved(self.calls[:2])
        duplicate.append(copy.deepcopy(duplicate[-1]))
        variants.append(duplicate)
        unknown = self.interleaved(self.calls[:2])
        unknown[-1]["tool_call_id"] = "synthetic-never-issued"
        variants.append(unknown)
        swapped = self.interleaved(self.calls[:2])
        swapped[2]["tool_call_id"], swapped[4]["tool_call_id"] = (
            swapped[4]["tool_call_id"], swapped[2]["tool_call_id"]
        )
        variants.append(swapped)
        for index, messages in enumerate(variants):
            with self.subTest(index=index):
                self.assert_rejected_without_consuming(messages, session)

    def test_declaration_order_and_duplicate_declarations_remain_rejected(self):
        session = self.arm()
        for calls in (list(reversed(self.calls[:2])), [self.calls[0], self.calls[0], self.calls[1]]):
            with self.subTest(count=len(calls)):
                self.assert_rejected_without_consuming(self.interleaved(calls), session)

    def test_arguments_names_and_catalog_are_still_exactly_bound(self):
        session = self.arm()
        for field, value in (
            ("name", "fake_breath_search"),
            ("arguments", '{"command":"synthetic changed"}'),
            ("arguments", '{"command":1}'),
            ("arguments", '{"command":"a","command":"b"}'),
        ):
            with self.subTest(field=field, value=value):
                messages = self.interleaved(self.calls[:2])
                messages[1]["tool_calls"][0]["function"][field] = value
                self.assert_rejected_without_consuming(messages, session)
        tools = copy.deepcopy(self.tools)
        tools[0]["function"]["parameters"]["properties"]["added"] = {"type": "string"}
        code = self.assert_rejected_without_consuming(self.interleaved(self.calls[:2]), session, tools=tools)
        self.assertEqual("tool_continuation_catalog_mismatch", code)

    def test_other_thread_cannot_use_an_exact_interleaved_batch(self):
        session = self.arm()
        code = self.assert_rejected_without_consuming(
            self.interleaved(self.calls[:2]), session,
            headers={**self.headers, "X-ST-Thread-ID": "thread:synthetic-other"},
        )
        self.assertEqual("tool_continuation_thread_mismatch", code)

    def test_three_rounds_mixed_settled_prefix_never_witness_old_results_twice(self):
        session = self.arm(self.calls[:1])
        continued = self.prepare(self.interleaved(self.calls[:1]))
        self.arm_bound_calls(continued, self.calls[1:3])
        continued = self.prepare(self.interleaved(self.calls[:3]))
        self.assertEqual(3, len(session.host_receipts))
        previous_receipts = copy.deepcopy(session.host_receipts)
        self.arm_bound_calls(continued, self.calls[3:5])
        with patch.object(self.app.execution_boundary, "witness_result", wraps=self.app.execution_boundary.witness_result) as witness:
            messages = self.interleaved(self.calls[:5])
            self.assert_forwarded_intact(copy.deepcopy(messages), self.prepare(messages))
        self.assertEqual(2, witness.call_count)
        self.assertEqual(previous_receipts, session.host_receipts[:3])
        self.assertEqual(5, len(session.host_receipts))
        self.assertEqual([c.tool_call_id for c in self.calls[:5]], session.settled_tool_call_order)
        self.assertEqual(1, self.control.seq)

    def test_settled_result_content_and_receipt_tampering_remain_rejected(self):
        session = self.arm(self.calls[:1])
        continued = self.prepare(self.interleaved(self.calls[:1]))
        self.arm_bound_calls(continued, self.calls[1:3])
        messages = self.interleaved(self.calls[:3])
        messages[2]["content"] = "synthetic changed settled result"
        self.assertEqual("tool_continuation_settled_replay_mismatch", self.assert_rejected_without_consuming(messages, session))
        original_receipt = copy.deepcopy(session.host_receipts[0])
        for field in ("result_hash", "arguments_hash", "wake_id", "catalog_hash", "schema_hash"):
            with self.subTest(field=field):
                session.host_receipts[0][field] = "synthetic altered receipt"
                self.assert_rejected_without_consuming(self.interleaved(self.calls[:3]), session)
                session.host_receipts[0] = copy.deepcopy(original_receipt)
        self.assert_forwarded_intact(self.interleaved(self.calls[:3]), self.prepare(self.interleaved(self.calls[:3])))

    def test_prior_wake_result_cannot_be_presented_as_a_settled_prefix(self):
        self.arm(self.calls[:1])
        completed = self.prepare(self.interleaved(self.calls[:1]))
        self.app.finish_turn(completed, keep_for_tools=False)
        session = self.arm(self.calls[1:3])
        self.assert_rejected_without_consuming(self.interleaved(self.calls[:3]), session)
        self.assertEqual(2, self.control.seq)

    def test_non_suffix_settled_prefix_is_not_accepted_as_current_history(self):
        session = self.arm(self.calls[:2])
        continued = self.prepare(self.interleaved(self.calls[:2]))
        self.arm_bound_calls(continued, self.calls[2:4])
        self.assert_rejected_without_consuming(
            self.interleaved([self.calls[0], *self.calls[2:4]]), session
        )

    def test_cancel_content_is_unknown_result_and_does_not_close_current_turn(self):
        session = self.arm()
        values = {**self.results, self.calls[1].tool_call_id: json.dumps({
            "status": "cancelled",
            "error": "Generation cancelled by user before tool execution completed.",
        })}
        messages = self.interleaved(self.calls[:2], values=values)
        continued = self.prepare(messages)
        self.assert_forwarded_intact(copy.deepcopy(messages), continued)
        self.assertIs(session, self.app._current_session)
        self.assertEqual(["unknown", "unknown"], [r["completion_state"] for r in session.host_receipts])
        self.assertFalse(any(path == "/v1/host/context/close" for path, _ in self.control.calls))
        with self.assertRaises(GatewayError) as caught:
            self.prepare([{"role": "user", "content": "synthetic new request"}])
        self.assertEqual("human_turn_in_progress", caught.exception.code)

    def test_partial_cancel_content_cannot_close_or_authorize_a_wait(self):
        session = self.arm()
        values = {**self.results, self.calls[0].tool_call_id: "Generation cancelled by user"}
        self.assert_rejected_without_consuming(self.interleaved(self.calls[:1], values=values), session)
        self.assertFalse(any(path == "/v1/host/context/close" for path, _ in self.control.calls))
        self.assertEqual(0, len(session.host_receipts))

    def test_partial_diagnostic_has_only_fixed_reason_and_counts(self):
        session = self.arm(self.calls[:3])
        messages = self.interleaved(
            self.calls[:2], texts=["synthetic do not log text", None],
            reasoning=["synthetic do not log reasoning", ""],
        )
        before = copy.deepcopy(session)
        with patch("rikkahub_gateway.server._FAILURE_LOGGER.warning") as logger:
            with self.assertRaises(GatewayError):
                self.prepare(messages)
        self.assertEqual(before, session)
        self.assertEqual(1, logger.call_count)
        encoded = logger.call_args.args[1]
        record = json.loads(encoded)
        self.assertLessEqual(set(record), {
            "at", "event", "code", "reason", "declared_count", "result_count",
            "missing_result_count", "unexpected_result_count", "expected_count",
            "terminal_declared_count", "segment_declared_count", "segment_result_count",
        })
        self.assertEqual("current_batch_order_mismatch", record["reason"])
        self.assertEqual((1, 2, 2, 3), tuple(record[key] for key in (
            "terminal_declared_count", "segment_declared_count", "segment_result_count",
            "expected_count",
        )))
        for value in (
            "synthetic", "workspace_shell", "fake_breath_search", session.wake_id,
            session.wake_capability, *session.protected_values,
        ):
            self.assertNotIn(value, encoded)


if __name__ == "__main__":
    unittest.main()
