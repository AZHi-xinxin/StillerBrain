"""Product tests for anchored new-human interruption, entirely synthetic.

No sockets, SQLite, subprocesses, external tools or live configuration. A local
cancel string is not treated as a completed operation. Exact branch identity is
tested; absent a stable thread header, a full cloned branch cannot be identified
as a different physical window and is intentionally not claimed to be detected.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import replace
import io
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import GatewayApplication, GatewayError, _GatewayHandler
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from rikkahub_gateway.tool_execution import NativeToolCall


class InterruptionControl(FakeControl):
    def __init__(self):
        super().__init__()
        self.running = 0
        self.close_failures = 0
        self.close_decision = "closed"

    def post(self, path, payload):
        if path.startswith("/v1/host/tool-executions/"):
            self.calls.append((path, copy.deepcopy(payload)))
            if path.endswith("/issue"):
                return {"batch_revision": payload["revision"], "executions": [
                    {"call_id": item["call_id"], "execution_ref": "stexec_" + "z" * 41 + f"{i:02d}"}
                    for i, item in enumerate(payload["calls"])]}
            return {"batch_revision": payload["revision"],
                    "batch_status": "active" if self.running else "closed",
                    "counts": {"running": self.running}}
        if path == "/v1/host/context/close":
            self.calls.append((path, copy.deepcopy(payload)))
            if self.close_failures:
                self.close_failures -= 1
                raise GatewayError(502, "st_control_unavailable")
            return {"decision": self.close_decision}
        return super().post(path, payload)


def schema(name, properties):
    return {"type": "function", "function": {"name": name,
        "parameters": {"type": "object", "properties": properties, "additionalProperties": False}}}


def wire(call):
    return {"id": call.tool_call_id, "type": "function",
            "function": {"name": call.tool_name, "arguments": call.arguments_text}}


class TimeoutConnection:
    """Socket timeout surface only; deliberately has no socket or network API."""

    def __init__(self, previous=None, *, fail_restore=False):
        self.timeout = previous
        self.changes = []
        self.fail_restore = fail_restore

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.changes.append(value)
        if self.fail_restore and len(self.changes) > 1:
            raise OSError("synthetic closed connection during restore")
        self.timeout = value


class TimedOutWriter:
    def __init__(self, connection, entered, contender_attempted, acquired):
        self.connection = connection
        self.entered = entered
        self.contender_attempted = contender_attempted
        self.acquired = acquired
        self.writes = 0

    def write(self, raw):
        self.writes += 1
        # A terminal commit shares one total deadline, so later writes may
        # receive less than 10 seconds, but never an unbounded/larger timeout.
        if not 0 < self.connection.gettimeout() <= 10.0:
            raise AssertionError("client write did not receive bounded timeout")
        self.entered.set()
        if not self.contender_attempted.wait(2):
            raise AssertionError("lock contender did not run")
        if self.acquired.is_set():
            raise AssertionError("generation lock was not held during client write")
        raise TimeoutError("synthetic client write timeout; no real delay")

    def flush(self):
        pass


class ManualTimer:
    """Deterministic callback capture: no timer thread or clock wait."""

    def __init__(self, interval, function, args=()):
        self.interval = interval
        self.function = function
        self.args = args
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # Intentionally can invoke an already queued callback after cancellation.
        self.function(*self.args)


class AnchoredInterruptionTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "sqlite3.connect", "subprocess.Popen"):
            self.enterContext(patch(target, side_effect=AssertionError("external I/O forbidden")))
        self.enterContext(patch("rikkahub_gateway.server._FAILURE_LOGGER.warning"))
        self.enterContext(patch("rikkahub_gateway.server.threading.Timer", ManualTimer))
        self.control = InterruptionControl()
        self.app = GatewayApplication(replace(config(), require_execution_binding=True,
            execution_epoch="synthetic-interruption-epoch"), control=self.control, upstream=object())
        self.addCleanup(self.cleanup)
        self.headers = {"X-ST-Thread-ID": "synthetic-thread-A"}
        self.history = [{"role": "system", "content": "synthetic original system"},
                        {"role": "user", "content": "synthetic original human"}]
        self.tools = [schema("mcp__TechHub__read_messages", {"channel": {"type": "string"}}),
                      schema("workspace_shell", {"command": {"type": "string"}})]
        self.calls = [NativeToolCall("synthetic-hub", "mcp__TechHub__read_messages", '{"channel":"synthetic"}'),
                      NativeToolCall("synthetic-shell", "workspace_shell", '{"command":"synthetic-never-run"}')]

    def cleanup(self):
        if self.app._current_session is not None:
            self.app._cancel_recovery_timer(self.app._current_session)

    @contextmanager
    def expect_code(self, code):
        with self.assertRaises(GatewayError) as caught:
            yield
        self.assertEqual(code, caught.exception.code)

    def payload(self, messages=None):
        return {"model": "stiller-rikka", "messages": copy.deepcopy(self.history if messages is None else messages),
                "tools": copy.deepcopy(self.tools)}

    def handler(self, writer, connection=None):
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=self.app)
        handler.wfile = writer
        handler.connection = connection or TimeoutConnection()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False
        return handler

    def mock_responses(self, bodies, *, stream):
        captured = []

        def respond(request):
            captured.append(json.loads(request.content))
            body = bodies[len(captured) - 1]
            if stream:
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)
            return httpx.Response(200, json=body)

        client = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
        self.addCleanup(client.close)
        self.app.upstream = client
        return captured

    @staticmethod
    def upstream_body(calls=None, *, stream):
        if not stream:
            message = {"role": "assistant", "content": None if calls else "synthetic response"}
            if calls:
                message["tool_calls"] = [wire(call) for call in calls]
            return {"choices": [{"index": 0, "message": message,
                                 "finish_reason": "tool_calls" if calls else "stop"}]}
        delta = ({"tool_calls": [{"index": index, **wire(call)} for index, call in enumerate(calls)]}
                 if calls else {"content": "synthetic response"})
        event = {"choices": [{"index": 0, "delta": delta,
                              "finish_reason": "tool_calls" if calls else "stop"}]}
        return ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()

    def arm(self, prepared=None, calls=None, *, delivered=True):
        prepared = prepared or self.app.prepare_turn(self.payload(), self.headers)
        calls = self.calls if calls is None else calls
        decorated = self.app.decorate_execution_calls(prepared, calls)
        bound = self.app.bind_response_tool_calls(prepared, decorated)
        self.app.finish_turn(prepared, keep_for_tools=True,
            tool_call_ids=[call.tool_call_id for call in decorated], tool_call_bindings=bound)
        if delivered:
            self.app.mark_response_delivered(prepared)
        return prepared, decorated

    def units(self, calls, *, split=False, content="synthetic result; execution not attested"):
        groups = [[call] for call in calls] if split else [calls]
        return [item for group in groups for item in [
            {"role": "assistant", "content": None, "tool_calls": [wire(call) for call in group]},
            *[{"role": "tool", "tool_call_id": call.tool_call_id, "content": content} for call in group]]]

    def new_human(self, calls, *, history=None, split=False, content="synthetic result; execution not attested"):
        return self.payload([*(self.history if history is None else history),
            *self.units(calls, split=split, content=content),
            {"role": "user", "content": "synthetic fresh human message"}])

    def rejected_without_control(self, payload, headers=None):
        session = self.app._current_session
        before = len(self.control.calls)
        snapshot = copy.deepcopy(payload)
        with self.assertRaises(GatewayError) as caught:
            self.app.prepare_turn(payload, self.headers if headers is None else headers)
        self.assertEqual("human_turn_in_progress", caught.exception.code)
        self.assertIs(session, self.app._current_session)
        self.assertEqual(before, len(self.control.calls))
        self.assertEqual(snapshot, payload)

    def test_thirdparty_new_human_abandons_exact_wait_without_witness_or_replay(self):
        old, calls = self.arm()
        payload = self.new_human(calls)
        snapshot = copy.deepcopy(payload)
        original_receipts = list(old.session.host_receipts)
        before = len(self.control.calls)
        with patch.object(self.app.execution_boundary, "witness_result", side_effect=AssertionError("no result witnessing")):
            new = self.app.prepare_turn(payload, self.headers)
        operations = self.control.calls[before:]
        self.assertEqual("/v1/host/context/close", operations[0][0])
        self.assertEqual(old.session.wake_id, operations[0][1]["wake_id"])
        self.assertEqual("/v1/host/wakes", operations[1][0])
        self.assertNotEqual(old.session.wake_id, new.session.wake_id)
        self.assertFalse(new.continuation)
        self.assertEqual(original_receipts, old.session.host_receipts)
        self.assertEqual(snapshot, payload)
        self.assertTrue({call.tool_call_id for call in calls} <= self.app._retired_tool_call_ids)
        self.assertFalse(any(path.startswith("/v1/host/tool-executions/") for path, _ in operations))

    def test_ordered_partial_and_interleaved_complete_units_are_supported(self):
        for index, (subset, split) in enumerate(((True, False), (False, True))):
            with self.subTest(subset=subset, split=split):
                unique_calls = [NativeToolCall(f"{call.tool_call_id}-{index}", call.tool_name,
                                              call.arguments_text) for call in self.calls]
                old, calls = self.arm(calls=unique_calls)
                selected = calls[1:] if subset else calls
                new = self.app.prepare_turn(self.new_human(selected, split=split), self.headers)
                self.assertTrue({call.tool_call_id for call in calls} <= self.app._retired_tool_call_ids)
                self.assertEqual([], old.session.host_receipts)
                self.app.finish_turn(new, keep_for_tools=False)

    def test_cancel_text_and_ordinary_result_text_have_no_completion_authority(self):
        old, calls = self.arm()
        with patch.object(self.app.execution_boundary, "witness_result", side_effect=AssertionError("cancel is not success")):
            new = self.app.prepare_turn(self.new_human(calls[:1], content="Generation cancelled by user"), self.headers)
        self.assertEqual([], old.session.host_receipts)
        self.assertEqual([], new.session.host_receipts)

    def test_absent_thread_header_accepts_exact_branch_not_physical_window_identity(self):
        old, calls = self.arm()
        new = self.app.prepare_turn(self.new_human(calls), {})
        self.assertIsNot(old.session, new.session)

    def test_explicit_other_thread_cannot_close_same_cloned_history(self):
        _, calls = self.arm()
        self.rejected_without_control(self.new_human(calls), {"X-ST-Thread-ID": "synthetic-thread-B"})

    def test_empty_compressed_or_modified_history_cannot_guess_cancel(self):
        _, calls = self.arm()
        variants = [self.new_human(calls, history=[]), self.new_human(calls, history=self.history[1:])]
        changed = copy.deepcopy(self.history)
        changed[-1]["content"] = "synthetic changed original request"
        variants.append(self.new_human(calls, history=changed))
        variants.append(self.payload([{"role": "user", "content": "stop"}]))
        for payload in variants:
            with self.subTest(shape=payload["messages"][0]["role"]):
                self.rejected_without_control(payload)

    def test_missing_declaration_result_or_current_call_is_not_an_interrupt(self):
        _, calls = self.arm()
        variants = []
        missing_result = self.new_human(calls)
        del missing_result["messages"][-2]
        variants.append(missing_result)
        declaration_only = self.payload([*self.history, self.units(calls)[0], {"role": "user", "content": "new"}])
        variants.append(declaration_only)
        no_declaration = self.payload([*self.history, self.units(calls)[1], {"role": "user", "content": "new"}])
        variants.append(no_declaration)
        unknown = self.new_human([NativeToolCall("synthetic-unknown", calls[0].tool_name, calls[0].arguments_text)])
        variants.append(unknown)
        for payload in variants:
            self.rejected_without_control(payload)

    def test_missing_tool_result_content_is_not_a_complete_unit(self):
        _, calls = self.arm()
        payload = self.new_human(calls[:1])
        del payload["messages"][-2]["content"]
        self.rejected_without_control(payload)

    def test_wrong_tool_result_content_type_is_not_a_complete_unit(self):
        _, calls = self.arm()
        for content in (None, 12, {"result": "synthetic"}, True):
            with self.subTest(content_type=type(content).__name__):
                self.rejected_without_control(self.new_human(calls[:1], content=content))

    def test_tool_result_empty_string_and_list_are_present_not_success(self):
        for index, content in enumerate(("", [])):
            with self.subTest(content_type=type(content).__name__):
                raw_call = self.calls[0]
                old, calls = self.arm(calls=[NativeToolCall(f"present-{index}", raw_call.tool_name,
                                                          raw_call.arguments_text)])
                with patch.object(self.app.execution_boundary, "witness_result",
                                  side_effect=AssertionError("present is not completed")):
                    new = self.app.prepare_turn(self.new_human(calls, content=content), self.headers)
                self.assertEqual([], old.session.host_receipts)
                self.app.finish_turn(new, keep_for_tools=False)

    def test_catalog_name_arguments_order_and_duplicate_changes_rejected(self):
        _, calls = self.arm()
        base = self.new_human(calls)
        variants = []
        catalog = copy.deepcopy(base)
        catalog["tools"][0]["function"]["parameters"]["properties"]["channel"]["maxLength"] = 8
        variants.append(catalog)
        renamed = copy.deepcopy(base)
        renamed["messages"][2]["tool_calls"][0]["function"]["name"] = calls[1].tool_name
        variants.append(renamed)
        arguments = copy.deepcopy(base)
        arguments["messages"][2]["tool_calls"][0]["function"]["arguments"] = '{"channel":"changed"}'
        variants.append(arguments)
        reversed_calls = self.new_human(list(reversed(calls)))
        variants.append(reversed_calls)
        duplicate = self.new_human([calls[0], calls[0]])
        variants.append(duplicate)
        for payload in variants:
            self.rejected_without_control(payload)

    def test_extra_non_tool_tail_message_or_additional_human_is_rejected(self):
        _, calls = self.arm()
        for middle in ({"role": "assistant", "content": "synthetic plain response"},
                       {"role": "system", "content": "synthetic injected middle"},
                       {"role": "user", "content": "synthetic intermediate human"}):
            payload = self.new_human(calls)
            payload["messages"].insert(-1, middle)
            self.rejected_without_control(payload)

    def test_raw_current_name_and_ids_cannot_be_trimmed_into_a_match(self):
        _, calls = self.arm()
        for field in ("name", "call_id", "result_id"):
            with self.subTest(field=field):
                payload = self.new_human(calls[:1])
                if field == "name":
                    payload["messages"][2]["tool_calls"][0]["function"]["name"] += " "
                elif field == "call_id":
                    payload["messages"][2]["tool_calls"][0]["id"] += " "
                else:
                    payload["messages"][3]["tool_call_id"] += " "
                self.rejected_without_control(payload)

    def test_undelivered_wait_and_active_generation_do_not_allow_takeover(self):
        old, calls = self.arm(delivered=False)
        self.rejected_without_control(self.new_human(calls))
        self.app.finish_turn(old, keep_for_tools=False)
        self.app.prepare_turn(self.payload(), self.headers)
        self.rejected_without_control(self.new_human(calls))

    def test_unmanaged_close_failure_quarantines_and_exact_retry_can_recover(self):
        old, calls = self.arm()
        payload = self.new_human(calls)
        anchor = (old.session.history_anchor_count, old.session.history_anchor_digest)
        self.control.close_failures = 1
        with self.expect_code("tool_wait_recovery_in_progress"):
            self.app.prepare_turn(payload, self.headers)
        self.assertEqual(1, self.control.seq)
        self.assertTrue(old.session.recovery_pending)
        self.assertEqual(anchor, (old.session.history_anchor_count, old.session.history_anchor_digest))
        ordinary = self.payload([*self.history, *self.units(calls)])
        with self.expect_code("tool_wait_recovery_in_progress"):
            self.app.prepare_turn(ordinary, self.headers)
        new = self.app.prepare_turn(payload, self.headers)
        self.assertIsNot(old.session, new.session)

    def test_unconfirmed_close_response_never_creates_new_wake(self):
        old, calls = self.arm()
        self.control.close_decision = "not_confirmed"
        with self.expect_code("tool_wait_recovery_in_progress"):
            self.app.prepare_turn(self.new_human(calls), self.headers)
        self.assertIs(old.session, self.app._current_session)
        self.assertEqual(1, self.control.seq)

    def use_managed_call(self):
        marker = {"type": "string", "pattern": "^stexec_[A-Za-z0-9_-]{43}$",
                  "x-stbrain-execution-tool": "stbrain_open", "x-stbrain-execution-contract": "st-execution/1"}
        self.tools.append(schema("stbrain_open", {"execution_ref": marker}))
        self.calls.append(NativeToolCall("synthetic-managed-open", "stbrain_open", "{}"))

    def test_running_st_claim_prevents_new_wake_until_confirmed_drain(self):
        self.use_managed_call()
        old, calls = self.arm()
        payload = self.new_human(calls[:1])
        self.control.running = 1
        with self.expect_code("tool_wait_recovery_in_progress"):
            self.app.prepare_turn(payload, self.headers)
        self.assertIs(old.session, self.app._current_session)
        self.assertTrue(old.session.recovery_pending)
        self.assertFalse(old.session.execution_batch_closed)
        self.assertEqual(1, self.control.seq)
        self.assertFalse(any(path == "/v1/host/context/close" for path, _ in self.control.calls))
        self.control.running = 0
        new = self.app.prepare_turn(payload, self.headers)
        self.assertTrue(old.session.execution_batch_closed)
        self.assertIsNot(old.session, new.session)

    def test_managed_close_response_loss_retries_exact_close_without_new_revoke(self):
        self.use_managed_call()
        old, calls = self.arm()
        self.control.close_failures = 1
        payload = self.new_human(calls)
        with self.expect_code("tool_wait_recovery_in_progress"):
            self.app.prepare_turn(payload, self.headers)
        self.assertTrue(old.session.execution_batch_closed)
        self.assertEqual(1, sum(path.endswith("/revoke") for path, _ in self.control.calls))
        self.app.prepare_turn(payload, self.headers)
        self.assertEqual(1, sum(path.endswith("/revoke") for path, _ in self.control.calls))
        self.assertEqual(2, sum(path == "/v1/host/context/close" for path, _ in self.control.calls))

    def test_old_continuation_and_callbacks_do_not_touch_replacement(self):
        old, calls = self.arm()
        new = self.app.prepare_turn(self.new_human(calls), self.headers)
        before = len(self.control.calls)
        self.app.finish_turn(old, keep_for_tools=False)
        self.app.mark_response_delivered(old)
        self.app._recover_abnormal_wait(old.session, old.session.execution_revision)
        with self.expect_code("tool_continuation_context_lost"):
            self.app.prepare_turn(self.payload([*self.history, *self.units(calls)]), self.headers)
        with self.expect_code("tool_continuation_context_lost"):
            self.app.assert_response_current(old)
        self.assertIs(new.session, self.app._current_session)
        self.assertEqual(before, len(self.control.calls))

    def test_same_session_next_request_fences_all_old_response_mutators(self):
        self.use_managed_call()
        old, calls = self.arm()
        continuation_history = [*self.history, *self.units(calls)]
        current = self.app.prepare_turn(self.payload(continuation_history), self.headers)
        self.assertIs(old.session, current.session)
        self.assertGreater(current.request_generation, old.request_generation)
        next_calls = [NativeToolCall("synthetic-next-" + call.tool_call_id, call.tool_name,
                                    call.arguments_text) for call in self.calls]
        current, _ = self.arm(current, next_calls, delivered=False)
        session = current.session
        before = (len(self.control.calls), session.request_generation, session.execution_revision,
                  set(session.expected_tool_call_ids), set(session.seen_tool_call_ids))
        self.app.finish_turn(old, keep_for_tools=False)
        self.app.finish_turn(old, keep_for_tools=True, tool_call_ids=["synthetic-stale"])
        self.app.mark_response_delivered(old)
        self.assertIsNone(session.delivered_at_monotonic)
        stale = [NativeToolCall("synthetic-stale-new-id", "stbrain_open", "{}")]
        for operation in (lambda: self.app.bind_response_tool_calls(old, stale),
                          lambda: self.app.decorate_execution_calls(old, stale),
                          lambda: self.app.assert_response_current(old)):
            with self.expect_code("tool_continuation_context_lost"):
                operation()
        after = (len(self.control.calls), session.request_generation, session.execution_revision,
                 set(session.expected_tool_call_ids), set(session.seen_tool_call_ids))
        self.assertEqual(before, after)
        self.assertIs(session, self.app._current_session)

    def test_normal_final_response_can_write_after_its_own_confirmed_finish(self):
        prepared = self.app.prepare_turn(self.payload(), self.headers)
        self.app.finish_turn(prepared, keep_for_tools=False)
        self.assertIsNone(self.app._current_session)
        self.app.assert_response_current(prepared)

    def test_retired_response_cannot_write_after_replacement_also_finishes(self):
        old, calls = self.arm()
        new = self.app.prepare_turn(self.new_human(calls), self.headers)
        self.app.finish_turn(new, keep_for_tools=False)
        self.assertIsNone(self.app._current_session)
        self.app.assert_response_current(new)
        with self.expect_code("tool_continuation_context_lost"):
            self.app.assert_response_current(old)

    def test_same_session_old_response_cannot_write_after_final_continuation(self):
        old, calls = self.arm()
        current = self.app.prepare_turn(self.payload([*self.history, *self.units(calls)]), self.headers)
        self.app.finish_turn(current, keep_for_tools=False)
        self.assertIsNone(self.app._current_session)
        self.app.assert_response_current(current)
        with self.expect_code("tool_continuation_context_lost"):
            self.app.assert_response_current(old)

    def merged_previous_and_current(self):
        _, first = self.arm(calls=self.calls[:1])
        previous = [*copy.deepcopy(self.history), *self.units(first, content="synthetic settled receipt")]
        previous[2]["reasoning_content"] = "synthetic prior reasoning"
        current = self.app.prepare_turn(self.payload(previous), self.headers)
        current, later = self.arm(current, [NativeToolCall("synthetic-second-round", self.calls[1].tool_name,
                                                         self.calls[1].arguments_text)])
        merged = copy.deepcopy(previous)
        merged[2]["tool_calls"].append(wire(later[0]))
        merged.append(self.units(later)[1])
        merged.append({"role": "user", "content": "synthetic new human after merged groups"})
        return current, self.payload(merged)

    def test_merged_settled_previous_round_preserves_exact_raw_history(self):
        current, payload = self.merged_previous_and_current()
        before = copy.deepcopy(payload)
        receipts = copy.deepcopy(current.session.host_receipts)
        with patch.object(self.app.execution_boundary, "witness_result",
                          side_effect=AssertionError("merged cancellation is not a new witness")):
            new = self.app.prepare_turn(payload, self.headers)
        self.assertIsNot(current.session, new.session)
        self.assertEqual(before, payload)
        self.assertEqual(receipts, current.session.host_receipts)

    def test_merged_previous_content_reasoning_call_and_receipt_changes_rejected(self):
        _, payload = self.merged_previous_and_current()
        variants = []
        for field, value in (("content", "changed prior body"),
                             ("reasoning_content", "changed prior reasoning")):
            changed = copy.deepcopy(payload)
            changed["messages"][2][field] = value
            variants.append(changed)
        changed = copy.deepcopy(payload)
        changed["messages"][3]["content"] = "changed prior receipt"
        variants.append(changed)
        changed = copy.deepcopy(payload)
        changed["messages"][2]["tool_calls"][0]["function"]["arguments"] = '{"channel":"changed"}'
        variants.append(changed)
        changed = copy.deepcopy(payload)
        del changed["messages"][2]["reasoning_content"]
        variants.append(changed)
        for index, changed in enumerate(variants):
            with self.subTest(variant=index):
                self.rejected_without_control(changed)

    def test_later_batch_uses_raw_incoming_history_anchor(self):
        old, calls = self.arm()
        incoming = [*self.history, *self.units(calls, split=True)]
        continuation = self.app.prepare_turn(self.payload(incoming), self.headers)
        later_calls = [NativeToolCall("synthetic-later-" + call.tool_call_id, call.tool_name,
                                     call.arguments_text) for call in self.calls]
        continuation, decorated = self.arm(continuation, later_calls)
        self.assertEqual(len(incoming), continuation.session.history_anchor_count)
        new = self.app.prepare_turn(self.new_human(decorated, history=incoming), self.headers)
        self.assertIsNot(old.session, new.session)

    def test_malformed_older_tool_call_prefix_fails_closed_without_raw_exception(self):
        original = copy.deepcopy(self.history)
        shapes = ("bad", {"id": "bad"}, ["bad"], [None], [12], [{"function": "bad"}],
                  [{"function": None}], [{"function": []}])
        for index, shape in enumerate(shapes):
            with self.subTest(shape=index):
                self.history = [*copy.deepcopy(original[:-1]),
                    {"role": "assistant", "content": "synthetic malformed older history",
                     "tool_calls": shape}, copy.deepcopy(original[-1])]
                raw = self.calls[0]
                old, calls = self.arm(calls=[NativeToolCall(f"malformed-prefix-{index}", raw.tool_name,
                                                          raw.arguments_text)])
                self.rejected_without_control(self.new_human(calls))
                self.app.finish_turn(old, keep_for_tools=False)

    def test_write_boundary_restores_existing_timeout_on_success_and_exception(self):
        prepared = self.app.prepare_turn(self.payload(), self.headers)
        for previous, expected in ((None, 10.0), (30.0, 10.0), (2.0, 2.0)):
            for fails in (False, True):
                with self.subTest(previous=previous, fails=fails):
                    connection = TimeoutConnection(previous)
                    handler = self.handler(io.BytesIO(), connection)
                    if fails:
                        with self.assertRaises(TimeoutError):
                            with handler._client_write_boundary(prepared):
                                self.assertEqual(expected, connection.timeout)
                                raise TimeoutError("synthetic timeout")
                    else:
                        with handler._client_write_boundary(prepared):
                            self.assertEqual(expected, connection.timeout)
                    self.assertEqual([expected, previous], connection.changes)
                    self.assertEqual(previous, connection.timeout)

    def test_restore_oserror_does_not_hide_original_write_timeout(self):
        prepared = self.app.prepare_turn(self.payload(), self.headers)
        connection = TimeoutConnection(fail_restore=True)
        with self.assertRaises(TimeoutError):
            with self.handler(io.BytesIO(), connection)._client_write_boundary(prepared):
                raise TimeoutError("synthetic original write error")
        self.assertEqual([10.0, None], connection.changes)

    def exercise_timeout_cleanup(self, *, stream):
        captured = self.mock_responses([self.upstream_body(self.calls, stream=stream)], stream=stream)
        payload = self.payload()
        payload["stream"] = stream
        prepared = self.app.prepare_turn(payload, self.headers)
        connection = TimeoutConnection()
        entered, attempted, acquired = threading.Event(), threading.Event(), threading.Event()
        writer = TimedOutWriter(connection, entered, attempted, acquired)

        def contender():
            if entered.wait(2):
                attempted.set()
                with self.app._lock:
                    acquired.set()

        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        try:
            handler = self.handler(writer, connection)
            (handler._proxy_stream if stream else handler._proxy_json)(prepared)
            self.assertTrue(acquired.wait(2), "client timeout did not release the generation lock")
        finally:
            entered.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, writer.writes)
        self.assertEqual(2, len(connection.changes))
        self.assertGreater(connection.changes[0], 0)
        self.assertLessEqual(connection.changes[0], 10.0)
        self.assertIsNone(connection.changes[1])
        self.assertIsNone(connection.timeout)
        self.assertIsNone(prepared.session.delivered_at_monotonic)
        self.assertIsNone(self.app._current_session)
        self.assertEqual(1, len(captured))
        self.assertEqual("/v1/host/context/close", self.control.calls[-1][0])
        self.assertEqual(prepared.session.wake_id, self.control.calls[-1][1]["wake_id"])

    def test_json_client_write_timeout_restores_socket_policy_releases_lock_and_cleans_up(self):
        self.exercise_timeout_cleanup(stream=False)

    def test_sse_client_write_timeout_restores_socket_policy_releases_lock_and_cleans_up(self):
        self.exercise_timeout_cleanup(stream=True)

    def exercise_http_interruption(self, *, stream):
        captured = self.mock_responses([self.upstream_body(self.calls, stream=stream),
            self.upstream_body(stream=stream)], stream=stream)
        payload = self.payload()
        payload["stream"] = stream
        old = self.app.prepare_turn(payload, self.headers)
        first_writer = io.BytesIO()
        first_handler = self.handler(first_writer)
        (first_handler._proxy_stream if stream else first_handler._proxy_json)(old)
        self.assertIsNotNone(old.session.delivered_at_monotonic)
        if stream:
            self.assertIn(b"[DONE]", first_writer.getvalue())
            actual_calls = []
            for event in first_writer.getvalue().split(b"\n\n"):
                if event.startswith(b"data: ") and event != b"data: [DONE]":
                    body = json.loads(event[6:])
                    for choice in body.get("choices", []):
                        actual_calls.extend(choice.get("delta", {}).get("tool_calls", []))
        else:
            actual_calls = json.loads(first_writer.getvalue())["choices"][0]["message"]["tool_calls"]
        actual = [NativeToolCall(call["id"], call["function"]["name"], call["function"]["arguments"])
                  for call in actual_calls]
        self.assertEqual(self.calls, actual)
        new_payload = self.new_human(actual, content="Generation cancelled by user")
        new_payload.update({"stream": stream, "temperature": 0.25, "max_tokens": 333})
        before = copy.deepcopy(new_payload)
        receipt_before = copy.deepcopy(old.session.host_receipts)
        with patch.object(self.app.execution_boundary, "witness_result",
                          side_effect=AssertionError("interrupt must not witness old results")):
            current = self.app.prepare_turn(new_payload, self.headers)
            expected_forward = copy.deepcopy(current.payload)
            second_writer = io.BytesIO()
            second_handler = self.handler(second_writer)
            (second_handler._proxy_stream if stream else second_handler._proxy_json)(current)
        self.assertEqual(before, new_payload)
        self.assertEqual(expected_forward, captured[1])
        self.assertEqual(new_payload["messages"], captured[1]["messages"][1:])
        self.assertEqual(new_payload["tools"], captured[1]["tools"])
        self.assertEqual(receipt_before, old.session.host_receipts)
        self.assertEqual(2, len(captured))
        self.assertIsNone(self.app._current_session)
        self.assertIn(b"synthetic response", second_writer.getvalue())
        self.assertFalse(any(path.startswith("/v1/host/tool-executions/") for path, _ in self.control.calls))

    def test_json_handler_new_human_roundtrip_preserves_upstream_input_without_witness(self):
        self.exercise_http_interruption(stream=False)

    def test_sse_handler_new_human_roundtrip_preserves_upstream_input_without_witness(self):
        self.exercise_http_interruption(stream=True)

    def test_abandon_running_then_timer_drains_without_replaying_new_human(self):
        self.use_managed_call()
        old, calls = self.arm()
        self.control.running = 1
        with self.expect_code("tool_wait_recovery_in_progress"):
            self.app.prepare_turn(self.new_human(calls[:1]), self.headers)
        timer = old.session.recovery_timer
        self.assertEqual(5.0, timer.interval)
        self.assertTrue(timer.started)
        self.assertTrue(old.session.abandon_requested)
        timer.fire()
        self.assertIs(old.session, self.app._current_session)
        self.assertEqual(1, self.control.seq)
        self.assertFalse(any(path == "/v1/host/context/close" for path, _ in self.control.calls))
        self.control.running = 0
        old.session.recovery_timer.fire()
        self.assertIsNone(self.app._current_session)
        self.assertFalse(old.session.abandon_requested)
        self.assertIsNone(old.session.recovery_timer)
        self.assertEqual(1, self.control.seq)
        with self.expect_code("tool_continuation_context_lost"):
            self.app.assert_response_current(old)

    def test_abandon_unmanaged_failed_close_timer_retries_only_exact_close(self):
        old, calls = self.arm()
        self.control.close_failures = 1
        with self.expect_code("tool_wait_recovery_in_progress"):
            self.app.prepare_turn(self.new_human(calls), self.headers)
        self.assertIsNone(old.session.execution_batch_id)
        timer = old.session.recovery_timer
        before = len(self.control.calls)
        timer.fire()
        self.assertEqual([( "/v1/host/context/close", {"wake_id": old.session.wake_id,
            "wake_capability": old.session.wake_capability})], self.control.calls[before:])
        self.assertIsNone(self.app._current_session)
        self.assertEqual(1, self.control.seq)
        self.assertTrue(timer.cancelled)

    def test_quarantined_wrong_thread_cannot_request_a_new_wake(self):
        old, calls = self.arm()
        self.control.close_failures = 1
        with self.expect_code("tool_wait_recovery_in_progress"):
            self.app.prepare_turn(self.new_human(calls), self.headers)
        timer = old.session.recovery_timer
        self.rejected_without_control(self.new_human(calls), {"X-ST-Thread-ID": "synthetic-thread-B"})
        self.assertIs(timer, old.session.recovery_timer)
        self.assertEqual(1, self.control.seq)

    def test_queued_abandon_timer_does_not_close_replacement(self):
        old, calls = self.arm()
        self.control.close_failures = 1
        with self.expect_code("tool_wait_recovery_in_progress"):
            self.app.prepare_turn(self.new_human(calls), self.headers)
        old_timer = old.session.recovery_timer
        old_timer.fire()
        current = self.app.prepare_turn(self.payload([{"role": "user", "content": "synthetic later human"}]),
                                        self.headers)
        before = len(self.control.calls)
        old_timer.fire()
        self.assertIs(current.session, self.app._current_session)
        self.assertEqual(before, len(self.control.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
