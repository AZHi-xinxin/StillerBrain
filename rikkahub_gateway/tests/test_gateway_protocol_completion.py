"""Exercise semantic SSE termination and value-free client diagnostics."""
from __future__ import annotations

import copy
import io
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from rikkahub_gateway.protocol_diagnostics import normalize_protocol_diagnostic
from rikkahub_gateway.server import (
    GatewayApplication, GatewayError, _GatewayHandler, _RequestPerformance,
    _log_gateway_failure,
)
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tests.test_gateway_stream_completion import event


class NoEofStream(httpx.SyncByteStream):
    """An upstream that has delivered DONE but will not send HTTP EOF."""
    def __init__(self, chunks, *, eof_allowed=False):
        self.chunks = chunks
        self.eof_allowed = eof_allowed
        self.requested_eof = False
        self.closed = False

    def __iter__(self):
        yield from self.chunks
        self.requested_eof = True
        if not self.eof_allowed:
            raise AssertionError("gateway requested more transport data after DONE")

    def close(self):
        self.closed = True


class ProtocolCompletionTests(unittest.TestCase):
    def stream(self, chunks, *, protected=False, eof_allowed=False):
        upstream_stream = NoEofStream(chunks, eof_allowed=eof_allowed)
        requests = []

        def upstream(request):
            requests.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=upstream_stream)

        client = httpx.Client(transport=httpx.MockTransport(upstream))
        self.addCleanup(client.close)
        app = GatewayApplication(replace(fixtures.config(), tool_result_wait_seconds=600),
                                 control=fixtures.FakeControl(), upstream=client)
        schedule = patch.object(app, "_schedule_tool_result_wait")
        schedule_mock = schedule.start()
        self.addCleanup(schedule.stop)
        user = {"role": "user", "content": "capability-1" if protected else "synthetic request"}
        tools = fixtures.GatewayTests.bound_tools()
        headers = {"Authorization": "Bearer " + fixtures.config().gateway_token, "X-ST-Thread-ID": "synthetic-protocol"}
        prepared = app.prepare_turn(fixtures.GatewayTests.request([user], stream=True, tools=tools), headers)
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False
        metric = _RequestPerformance(stream=True)
        handler._request_performance = metric
        error = None
        try:
            handler._proxy_stream(prepared)
        except GatewayError as caught:
            error = caught.payload()["error"]
        raw = handler.wfile.getvalue()
        for line in raw.splitlines():
            if line.startswith(b"data: {"):
                payload = json.loads(line[6:])
                error = payload.get("error", error)
        self.assertEqual(1, len(requests))
        self.assertTrue(upstream_stream.closed)
        return SimpleNamespace(app=app, error=error, raw=raw, metric=metric, transport=upstream_stream,
                               headers=headers, user=user, tools=tools, schedule=schedule_mock)

    @staticmethod
    def tool_chunks(count=1):
        calls = [fixtures.GatewayTests.native_call("original-" + str(i), "synthetic-" + str(i)) for i in range(count)]
        chunks = []
        for i, call in enumerate(calls):
            wire = fixtures.GatewayTests.wire_call(call)
            arguments = wire["function"]["arguments"]
            wire.update(index=i)
            wire["function"]["arguments"] = arguments[:8]
            chunks.extend([
                event({"tool_calls": [wire]}),
                event({"tool_calls": [{"index": i, "function": {"arguments": arguments[8:]}}]}),
            ])
        chunks += [event({"content": ""}), event({}, "tool_calls"),
                   b'data: {"choices":[],"usage":{"completion_tokens":10}}\n\n', b"data: [DONE]\n\n"]
        return calls, chunks

    def continue_calls(self, result, calls):
        declared = [fixtures.GatewayTests.wire_call(call) for call in calls]
        history = [result.user, {"role": "assistant", "tool_calls": declared}]
        history += [{"role": "tool", "tool_call_id": call["id"], "content": '{"ok":true}'} for call in declared]
        return result.app.prepare_turn(fixtures.GatewayTests.request(history, stream=True, tools=result.tools), result.headers)

    def test_done_completes_tool_batch_without_http_eof_and_continuation_still_binds(self):
        for protected in (False, True):
            for count in (1, 3):
                calls, chunks = self.tool_chunks(count)
                for size in (1, 17, 65536):
                    raw = b"".join(chunks)
                    pieces = [raw[i:i + size] for i in range(0, len(raw), size)]
                    with self.subTest(protected=protected, count=count, chunk_size=size):
                        result = self.stream(pieces, protected=protected)
                        self.assertIsNone(result.error)
                        self.assertFalse(result.transport.requested_eof)
                        self.assertEqual(1, result.raw.count(b"data: [DONE]"))
                        self.assertEqual(count, result.metric.response_tool_count)
                        self.assertEqual("done", result.metric.stream_end)
                        self.assertIsNotNone(result.metric.stream_done_ms)
                        self.assertIsNotNone(result.metric.stream_finish_ms)
                        result.schedule.assert_called_once()
                        self.assertEqual(600, result.schedule.call_args.args[1])
                        continued = self.continue_calls(result, calls)
                        result.app.finish_turn(continued, keep_for_tools=False)
                        self.assertIsNone(result.app._current_session)

    def test_text_done_does_not_wait_for_eof_and_finish_only_keeps_reading_usage(self):
        for protected in (False, True):
            done = self.stream([event({"content": "answer"}, "stop"), b"data: [DONE]\n\n"], protected=protected)
            self.assertIsNone(done.error)
            self.assertFalse(done.transport.requested_eof)
            finish = self.stream([event({"content": "answer"}, "stop"),
                                  b'data: {"choices":[],"usage":{"completion_tokens":7}}\n\n'],
                                 protected=protected, eof_allowed=True)
            self.assertIsNone(finish.error)
            self.assertTrue(finish.transport.requested_eof)
            self.assertEqual("eof", finish.metric.stream_end)
            self.assertIn(b'"completion_tokens":7', finish.raw)

    def test_already_received_invalid_tail_rejected_even_across_local_16k_slices(self):
        calls, chunks = self.tool_chunks()
        for protected in (False, True):
            for trailing, reason in (
                (b"data: [DONE]\n\n", "duplicate_done"),
                (event({"content": "after-done"}), "output_after_finish"),
                (b'data: {"unterminated":', "invalid_json"),
            ):
                raw = b"".join(chunks) + b":" + b" " * 20000 + b"\n\n" + trailing
                result = self.stream([raw], protected=protected)
                self.assertEqual("upstream_invalid_stream", result.error["code"])
                self.assertEqual(reason, result.error["diagnostic"]["reason"])
                self.assertNotIn(calls[0].tool_call_id.encode(), result.raw)
                self.assertIsNone(result.app._current_session)
                result.schedule.assert_not_called()

    def test_done_never_bypasses_arguments_or_protected_value_checks(self):
        for protected in (False, True):
            _, chunks = self.tool_chunks()
            invalid = b"".join(chunks).replace(b'synthetic-0', b'capability-1')
            result = self.stream([invalid], protected=protected)
            self.assertEqual("upstream_protected_value", result.error["code"])
            self.assertNotIn(b"capability-1", result.raw)
            self.assertNotIn(b"original-0", result.raw)
            calls, _ = self.tool_chunks()
            wire = fixtures.GatewayTests.wire_call(calls[0])
            wire.update(index=0)
            wire["function"]["arguments"] = '{"unknown":"synthetic"}'
            invalid = event({"tool_calls":[wire]}, "tool_calls") + b"data: [DONE]\n\n"
            result = self.stream([invalid], protected=protected)
            self.assertEqual("tool_arguments_schema_invalid", result.error["code"])
            self.assertNotIn(b"original-0", result.raw)
            result.schedule.assert_not_called()

    def test_malformed_events_have_fixed_diagnostics_without_private_values(self):
        for raw, reason in ((b'data: {"PRIVATE_PAYLOAD"\n\n', "invalid_json"),
                            (b'data: ["PRIVATE_PAYLOAD"]\n\n', "non_object_event"),
                            (b'data: \xff\n\n', "invalid_utf8"),
                            (b'data: {"value":NaN}\n\n', "non_finite_json")):
            result = self.stream([event({"content": "prefix"}), raw], eof_allowed=True)
            self.assertEqual(reason, result.error["diagnostic"]["reason"])
            self.assertIn("upstream_invalid_stream", result.error["message"])
            self.assertNotIn(b"PRIVATE_PAYLOAD", result.raw)
            self.assertEqual(reason, result.metric.stream_error_reason)

    def test_lineage_diagnostic_distinguishes_changed_ids_and_changed_order(self):
        calls, chunks = self.tool_chunks(2)
        for kind in ("ids_changed", "order_changed"):
            result = self.stream(chunks)
            changed = list(reversed(calls)) if kind == "order_changed" else [
                fixtures.GatewayTests.native_call("replacement-" + str(i), "synthetic-" + str(i)) for i in range(2)]
            before = copy.deepcopy(result.app._current_session)
            with self.assertRaises(GatewayError) as caught:
                self.continue_calls(result, changed)
            error = caught.exception.payload()["error"]
            self.assertEqual(kind, error["diagnostic"]["mismatch_kind"])
            self.assertEqual("tool_continuation_lineage_mismatch", error["code"])
            self.assertNotIn("original-", json.dumps(error))
            self.assertNotIn("replacement-", json.dumps(error))
            self.assertEqual(before, result.app._current_session)
            continued = self.continue_calls(result, calls)
            result.app.finish_turn(continued, keep_for_tools=False)

    def test_diagnostics_and_logs_drop_arbitrary_values_and_bound_counters(self):
        value = {"kind": "stream", "reason": "invalid_json", "event_index": 10**30,
                 "text": "PRIVATE", "token": "SECRET", "result_count": True}
        self.assertEqual({"kind":"stream", "reason":"invalid_json", "event_index":1000000}, normalize_protocol_diagnostic(value))
        self.assertIsNone(normalize_protocol_diagnostic({"kind":"stream", "reason":"PRIVATE"}))
        with self.assertLogs("stiller.rikkahub.gateway", level="WARNING") as logs:
            _log_gateway_failure("upstream_invalid_stream", streamed_prefix=True,
                                 protocol_diagnostic=value, request_id="a" * 32)
        record = json.loads(logs.records[0].getMessage())
        self.assertEqual("a" * 32, record["request_id"])
        self.assertNotIn("PRIVATE", logs.output[0])
        self.assertNotIn("SECRET", logs.output[0])


if __name__ == "__main__":
    unittest.main()
