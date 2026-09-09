"""No-network gateway reference insertion and actual abnormal-wait timer tests."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import threading
import time
import unittest
from unittest.mock import patch

from rikkahub_gateway.server import (
    GatewayApplication, GatewayError, _buffered_sse_tool_calls, _execution_sse_events, _parse_sse_event,
)
from rikkahub_gateway.tool_execution import NativeToolCall
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tests import test_gateway_interleaved_wire as wire_fixtures


def st_tool(name="stbrain_open"):
    return {"type": "function", "function": {"name": name, "parameters": {
        "type": "object", "properties": {
            "view": {"type": "string"},
            "execution_ref": {"type": "string", "pattern": "^stexec_[A-Za-z0-9_-]{43}$",
                "x-stbrain-execution-tool": "stbrain_open", "x-stbrain-execution-contract": "st-execution/1"},
        }, "additionalProperties": False,
    }}}


class ExecutionControl(FakeControl):
    def __init__(self):
        super().__init__()
        self.running = 0
        self.revoked = False
        self.closed = threading.Event()

    def post(self, path, payload):
        if path.startswith("/v1/host/tool-executions/"):
            self.calls.append((path, copy.deepcopy(payload)))
            if path.endswith("/issue"):
                return {"batch_revision": payload["revision"], "executions": [
                    {"call_id": call["call_id"], "execution_ref": "stexec_" + "r" * 41 + f"{index:02d}"}
                    for index, call in enumerate(payload["calls"])]}
            if path.endswith("/revoke"):
                self.revoked = True
            return {"batch_revision": payload["revision"], "batch_status": "closed" if self.revoked and not self.running else "active",
                    "counts": {"issued": 1, "running": self.running, "completed": 0, "failed": 0, "revoked": int(self.revoked), "orphaned": 0}}
        result = super().post(path, payload)
        if path == "/v1/host/context/close":
            self.closed.set()
        return result


class GatewayExecutionRecoveryTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "sqlite3.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("external access forbidden")))
        self.control = ExecutionControl()
        self.app = GatewayApplication(replace(config(), require_execution_binding=True,
            execution_epoch="synthetic-epoch", abnormal_wait_seconds=0.04), control=self.control, upstream=object())
        self.headers = {"X-ST-Thread-ID": "synthetic-thread"}
        self.user = {"role": "user", "content": "synthetic user"}
        self.tools = fixtures.GatewayTests.bound_tools() + [st_tool()]
        self.payload = {"messages": [self.user], "tools": self.tools}
        self.prepared = self.app.prepare_turn(self.payload, self.headers)
        self.addCleanup(self.cleanup_timer)

    def cleanup_timer(self):
        self.app._cancel_recovery_timer(self.prepared.session)

    def batch(self):
        return [NativeToolCall("synthetic-shell", "workspace_shell", '{"command":"synthetic only"}'),
                NativeToolCall("synthetic-open", "stbrain_open", ' { "view" : "summary" }\n')]

    def arm(self):
        decorated = self.app.decorate_execution_calls(self.prepared, self.batch())
        bindings = self.app.bind_response_tool_calls(self.prepared, decorated)
        self.app.finish_turn(self.prepared, keep_for_tools=True,
            tool_call_ids=[call.tool_call_id for call in decorated], tool_call_bindings=bindings)
        self.app.mark_response_delivered(self.prepared)
        return decorated

    def partial(self, calls=None):
        calls = calls or self.batch()[:1]
        return {"tools": self.tools, "messages": [self.user,
            {"role": "assistant", "content": "keep this text", "reasoning_content": "keep reasoning",
             "tool_calls": [fixtures.GatewayTests.wire_call(call) for call in calls]},
            *[{"role": "tool", "tool_call_id": call.tool_call_id, "content": "synthetic result"} for call in calls]]}

    def test_only_reserved_field_inserted_business_characters_preserved(self):
        before = self.batch()
        after = self.app.decorate_execution_calls(self.prepared, before)
        self.assertIs(before[0], after[0])
        self.assertEqual(before[1].arguments_text, after[1].arguments_text.replace(
            ',"execution_ref":"stexec_' + "r" * 41 + '00"', ""))
        values = json.loads(after[1].arguments_text)
        self.assertEqual("summary", values["view"])
        self.assertEqual({"view", "execution_ref"}, set(values))

    def test_nonempty_model_reference_rejected_before_issue(self):
        call = NativeToolCall("synthetic-open", "stbrain_open", '{"execution_ref":"not-host-issued"}')
        with self.assertRaisesRegex(GatewayError, "execution_reference_model_supplied"):
            self.app.decorate_execution_calls(self.prepared, [call])
        self.assertFalse(any(path.endswith("/issue") for path, _ in self.control.calls))

    def test_canonical_old_schema_is_not_silently_unbound(self):
        self.prepared.payload["tools"][-1]["function"]["parameters"]["properties"].pop("execution_ref")
        with self.assertRaisesRegex(GatewayError, "execution_binding_required"):
            self.app.decorate_execution_calls(self.prepared, self.batch())

    def test_fragmented_sse_inserts_ref_without_losing_text_reasoning_or_arguments(self):
        before = self.batch()
        after = self.app.decorate_execution_calls(self.prepared, before)
        fragments = [before[1].arguments_text[:2], before[1].arguments_text[2:-2], before[1].arguments_text[-2:]]
        payloads = [{"choices": [{"index": 0, "delta": {"content": "intro", "reasoning_content": "thought",
            "tool_calls": [{"index": 0, "id": before[0].tool_call_id, "type": "function", "function": {
                "name": before[0].tool_name, "arguments": before[0].arguments_text}},
                {"index": 1, "id": before[1].tool_call_id, "type": "function", "function": {
                    "name": before[1].tool_name, "arguments": fragments[0]}}]}, "finish_reason": None}]}]
        for fragment in fragments[1:]:
            payloads.append({"choices": [{"index": 0, "delta": {"reasoning_content": "more", "tool_calls": [
                {"index": 1, "function": {"arguments": fragment}}]}, "finish_reason": None}]})
        raw = [("data: " + json.dumps(payload) + "\n\n").encode() for payload in payloads] + [b"data: [DONE]\n\n"]
        rewritten = _execution_sse_events(raw, before, after)
        self.assertEqual(after, _buffered_sse_tool_calls(b"".join(rewritten)))
        self.assertEqual(len(raw), len(rewritten))
        for original, changed in zip(payloads, rewritten):
            result = _parse_sse_event(changed).payload
            self.assertEqual(original["choices"][0]["delta"].get("reasoning_content"), result["choices"][0]["delta"].get("reasoning_content"))
            self.assertEqual(original["choices"][0]["delta"].get("content"), result["choices"][0]["delta"].get("content"))

    def test_sse_usage_without_choices_is_preserved(self):
        before = self.batch()
        after = self.app.decorate_execution_calls(self.prepared, before)
        payload = {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": index, **fixtures.GatewayTests.wire_call(call)} for index, call in enumerate(before)]}}]}
        event = ("data: " + json.dumps(payload) + "\n\n").encode()
        usage = b'data: {"choices":null,"usage":{"completion_tokens":12}}\n\n'
        output = _execution_sse_events([event, usage, b"data: [DONE]\n\n"], before, after)
        self.assertEqual(usage, output[1])
        self.assertEqual(after, _buffered_sse_tool_calls(b"".join(output)))

    def test_verified_partial_real_timer_retires_without_next_request(self):
        self.arm()
        with self.assertRaises(GatewayError):
            self.app.prepare_turn(self.partial(), self.headers)
        self.assertTrue(self.control.closed.wait(timeout=1.0))
        # The fake close event fires within the callback, before its final
        # in-memory retirement. Synchronize on the same critical section.
        with self.app._lock:
            self.assertIsNone(self.app._current_session)
            self.assertTrue(self.control.revoked)
            self.assertIn("synthetic-open", self.app._retired_tool_call_ids)

    def test_wrong_arguments_409_does_not_start_timer(self):
        self.arm()
        wrong = NativeToolCall("synthetic-shell", "workspace_shell", '{"command":"wrong"}')
        with self.assertRaises(GatewayError):
            self.app.prepare_turn(self.partial([wrong]), self.headers)
        self.assertIsNone(self.prepared.session.recovery_timer)
        self.assertFalse(self.control.closed.wait(timeout=0.08))
        self.assertIs(self.prepared.session, self.app._current_session)

    def test_running_call_is_not_revoked_at_deadline(self):
        self.arm()
        self.control.running = 1
        with self.assertRaises(GatewayError):
            self.app.prepare_turn(self.partial(), self.headers)
        self.assertFalse(self.control.closed.wait(timeout=0.09))
        self.assertFalse(self.control.revoked)
        self.control.running = 0
        timer = self.prepared.session.recovery_timer
        if timer is not None:
            timer.cancel()
        self.app._recover_abnormal_wait(self.prepared.session, self.prepared.session.execution_revision)
        self.assertTrue(self.control.closed.is_set())

    def test_undelivered_and_normal_generation_do_not_start_timer(self):
        calls = self.app.decorate_execution_calls(self.prepared, self.batch())
        self.app.bind_response_tool_calls(self.prepared, calls)
        self.assertIsNone(self.prepared.session.recovery_timer)
        self.assertFalse(self.control.closed.wait(timeout=0.08))

    def test_full_result_cancels_old_deadline_without_replaying(self):
        calls = self.arm()
        with self.assertRaises(GatewayError):
            self.app.prepare_turn(self.partial(), self.headers)
        continued = self.app.prepare_turn(self.partial(calls), self.headers)
        self.assertTrue(continued.continuation)
        self.assertIsNone(self.prepared.session.recovery_timer)
        self.assertFalse(self.control.closed.wait(timeout=0.08))

    def test_missing_uninstrumented_tool_does_not_start_timer(self):
        calls = self.arm()
        with self.assertRaises(GatewayError):
            self.app.prepare_turn(self.partial(calls[1:]), self.headers)
        self.assertIsNone(self.prepared.session.recovery_timer)

    def test_close_response_loss_retries_only_confirmed_exact_close(self):
        self.arm()
        original_post = self.control.post
        lost = False

        def lose_first_close(path, payload):
            nonlocal lost
            if lost and path.startswith("/v1/host/tool-executions/"):
                raise GatewayError(409, "execution_wake_not_current")
            result = original_post(path, payload)
            if path == "/v1/host/context/close" and not lost:
                lost = True
                raise GatewayError(502, "synthetic_response_lost")
            return result

        self.control.post = lose_first_close
        self.app.finish_turn(self.prepared, keep_for_tools=False)
        session = self.prepared.session
        self.assertTrue(session.execution_batch_closed)
        self.assertTrue(session.recovery_pending)
        self.assertIs(session, self.app._current_session)
        session.recovery_timer.cancel()
        session.abnormal_wait_started = time.monotonic() - 1
        self.app._recover_abnormal_wait(session, session.execution_revision)
        self.assertIsNone(self.app._current_session)
        self.assertEqual(1, sum(path.endswith("/revoke") for path, _ in self.control.calls))
        self.assertEqual(2, sum(path == "/v1/host/context/close" for path, _ in self.control.calls))

    def test_natural_expiry_does_not_forget_running_execution(self):
        self.arm()
        session = self.prepared.session
        session.expires_at = datetime.now(timezone.utc) - timedelta(minutes=31)
        self.control.running = 1
        with self.assertRaisesRegex(GatewayError, "tool_wait_recovery_in_progress"):
            self.app.prepare_turn(self.payload, self.headers)
        self.assertIs(session, self.app._current_session)
        self.assertTrue(session.recovery_pending)
        self.assertFalse(self.control.closed.is_set())
        self.assertFalse(session.execution_batch_closed)
        self.control.running = 0
        session.recovery_timer.cancel()
        session.abnormal_wait_started = time.monotonic() - 1
        self.app._recover_abnormal_wait(session, session.execution_revision)
        self.assertIsNone(self.app._current_session)
        self.assertTrue(self.control.closed.is_set())
        new = self.app.prepare_turn(self.payload, self.headers)
        self.assertIsNot(session, new.session)
        self.app.finish_turn(new, keep_for_tools=False)

    def test_stale_timer_revision_cannot_close_current_batch(self):
        self.arm()
        session = self.prepared.session
        session.abnormal_wait_started = time.monotonic() - 1
        before = len(self.control.calls)
        self.app._recover_abnormal_wait(session, session.execution_revision - 1)
        self.assertEqual(before, len(self.control.calls))
        self.assertIs(session, self.app._current_session)
        self.assertFalse(self.control.closed.is_set())

    def later_batch_partial(self):
        first_calls = self.arm()
        first = self.partial(first_calls)
        continued = self.app.prepare_turn(first, self.headers)
        second_calls = [NativeToolCall(call.tool_call_id + "-next", call.tool_name, call.arguments_text)
                        for call in self.batch()]
        decorated = self.app.decorate_execution_calls(continued, second_calls)
        bindings = self.app.bind_response_tool_calls(continued, decorated)
        self.app.finish_turn(continued, keep_for_tools=True,
            tool_call_ids=[call.tool_call_id for call in decorated], tool_call_bindings=bindings)
        self.app.mark_response_delivered(continued)
        return {"tools": self.tools, "messages": [*first["messages"],
            *self.partial(decorated[:1])["messages"][1:]]}

    def test_later_partial_with_verified_settled_prefix_starts_real_timer(self):
        payload = self.later_batch_partial()
        original = copy.deepcopy(payload)
        receipts = copy.deepcopy(self.prepared.session.host_receipts)
        with self.assertRaises(GatewayError):
            self.app.prepare_turn(payload, self.headers)
        self.assertEqual(original, payload)
        self.assertEqual(receipts, self.prepared.session.host_receipts)
        self.assertTrue(self.control.closed.wait(timeout=1.0))
        with self.app._lock:
            self.assertIsNone(self.app._current_session)
            self.assertIn("synthetic-open-next", self.app._retired_tool_call_ids)

    def test_recovered_chat_with_old_partial_history_accepts_new_human_not_old_continuation(self):
        partial = self.later_batch_partial()
        original = copy.deepcopy(partial)
        with self.assertRaises(GatewayError):
            self.app.prepare_turn(partial, self.headers)
        self.assertTrue(self.control.closed.wait(timeout=1.0))
        new_payload = {"tools": self.tools, "messages": [*partial["messages"],
            {"role": "user", "content": "synthetic next human turn in the same chat"}]}
        with self.app._lock:
            self.assertIsNone(self.app._current_session)
        new = self.app.prepare_turn(new_payload, self.headers)
        self.assertFalse(new.continuation)
        self.assertNotEqual(self.prepared.session.wake_id, new.session.wake_id)
        self.assertEqual(original, partial)
        for message in new_payload["messages"]:
            self.assertIn(message, new.payload["messages"])
        with self.assertRaisesRegex(GatewayError, "tool_continuation_context_lost"):
            self.app.prepare_turn(partial, self.headers)
        self.assertIs(new.session, self.app._current_session)
        self.app.finish_turn(new, keep_for_tools=False)

    def test_bad_settled_prefix_cannot_start_later_partial_timer(self):
        valid = self.later_batch_partial()
        variants = []
        arguments = copy.deepcopy(valid)
        arguments["messages"][1]["tool_calls"][0]["function"]["arguments"] = '{"command":"tampered"}'
        variants.append(arguments)
        result = copy.deepcopy(valid)
        result["messages"][2]["content"] = "tampered old result"
        variants.append(result)
        order = copy.deepcopy(valid)
        order["messages"][1]["tool_calls"].reverse()
        variants.append(order)
        unknown = copy.deepcopy(valid)
        unknown["messages"][1]["tool_calls"][0]["id"] = "synthetic-unknown-prefix"
        unknown["messages"][2]["tool_call_id"] = "synthetic-unknown-prefix"
        variants.append(unknown)
        current_arguments = copy.deepcopy(valid)
        current_arguments["messages"][4]["tool_calls"][0]["function"]["arguments"] = '{"command":"tampered current"}'
        variants.append(current_arguments)
        before_calls = len(self.control.calls)
        receipts = copy.deepcopy(self.prepared.session.host_receipts)
        for index, payload in enumerate(variants):
            with self.subTest(index=index):
                original = copy.deepcopy(payload)
                with self.assertRaises(GatewayError):
                    self.app.prepare_turn(payload, self.headers)
                self.assertEqual(original, payload)
                self.assertIsNone(self.prepared.session.recovery_timer)
                self.assertIsNone(self.prepared.session.abnormal_wait_started)
                self.assertEqual(before_calls, len(self.control.calls))
                self.assertEqual(receipts, self.prepared.session.host_receipts)
        self.assertFalse(self.control.closed.is_set())

    def test_cross_wake_or_bad_signed_receipt_cannot_start_later_partial_timer(self):
        payload = self.later_batch_partial()
        receipts = copy.deepcopy(self.prepared.session.host_receipts)
        for field, value in (("wake_id", "synthetic-other-wake"), ("result_hash", "0" * 64)):
            with self.subTest(field=field):
                self.prepared.session.host_receipts[0][field] = value
                with self.assertRaises(GatewayError):
                    self.app.prepare_turn(payload, self.headers)
                self.assertIsNone(self.prepared.session.recovery_timer)
                self.assertIsNone(self.prepared.session.abnormal_wait_started)
                self.prepared.session.host_receipts = copy.deepcopy(receipts)
        self.assertFalse(self.control.closed.is_set())


class GatewayExecutionHTTPWireTests(unittest.TestCase):
    """Actual localhost JSON/SSE, with synthetic upstream/control and no database."""
    post = wire_fixtures.InterleavedHTTPWireTests.post
    upstream = wire_fixtures.InterleavedHTTPWireTests.upstream
    interleaved = wire_fixtures.InterleavedHTTPWireTests.interleaved

    def setUp(self):
        wire_fixtures.InterleavedHTTPWireTests.setUp(self)
        self.control = ExecutionControl()
        self.app.control = self.control
        self.app.config = replace(self.app.config, require_execution_binding=True, execution_epoch="synthetic-wire-epoch")
        self.tools[-1] = st_tool()

    def tearDown(self):
        if self.app._current_session is not None:
            self.app._cancel_recovery_timer(self.app._current_session)
        wire_fixtures.InterleavedHTTPWireTests.tearDown(self)

    def roundtrip(self, first_stream, second_stream):
        original = copy.deepcopy(self.calls)
        status, raw = self.post([self.user], stream=first_stream)
        self.assertEqual(200, status)
        if first_stream:
            delivered = _buffered_sse_tool_calls(raw)
            self.calls = [fixtures.GatewayTests.wire_call(call) for call in delivered]
        else:
            self.calls = json.loads(raw)["choices"][0]["message"]["tool_calls"]
        self.assertEqual(original[0], self.calls[0])
        self.assertEqual(original[1]["id"], self.calls[1]["id"])
        args = json.loads(self.calls[1]["function"]["arguments"])
        self.assertEqual({"execution_ref"}, set(args))
        self.assertIsNotNone(self.app._current_session.delivered_at_monotonic)
        submitted = self.interleaved()
        status, result = self.post(submitted, stream=second_stream)
        self.assertEqual(200, status)
        self.assertIn(b"synthetic complete", result)
        forwarded = self.upstream_payloads[-1]["messages"]
        projected = copy.deepcopy(submitted)
        for message in projected:
            for call in message.get("tool_calls", []):
                if call["function"]["name"] == "stbrain_open":
                    # Only the model-facing copy omits the host input; the
                    # submitted history still carries the bound client value.
                    call["function"]["arguments"] = "{}"
        for message in projected:
            self.assertIn(message, forwarded)
        self.assertIn("execution_ref", json.loads(submitted[3]["tool_calls"][0]["function"]["arguments"]))
        self.assertIsNone(self.app._current_session)

    def test_json_json(self):
        self.roundtrip(False, False)

    def test_json_sse(self):
        self.roundtrip(False, True)

    def test_sse_json(self):
        self.roundtrip(True, False)

    def test_sse_sse(self):
        self.roundtrip(True, True)

    def test_json_model_supplied_reference_never_reaches_client_as_tool(self):
        self.calls[1]["function"]["arguments"] = '{"execution_ref":"model-made"}'
        status, raw = self.post([self.user], stream=False)
        self.assertEqual(502, status)
        self.assertEqual("execution_reference_model_supplied", json.loads(raw)["error"]["code"])
        self.assertNotIn(b"model-made", raw)

    def test_sse_model_supplied_reference_never_reaches_client_as_tool(self):
        self.calls[1]["function"]["arguments"] = '{"execution_ref":"model-made"}'
        status, raw = self.post([self.user], stream=True)
        self.assertEqual(502, status)
        self.assertNotIn(b"model-made", raw)
        self.assertNotIn(b'"tool_calls"', raw)


if __name__ == "__main__":
    unittest.main()
