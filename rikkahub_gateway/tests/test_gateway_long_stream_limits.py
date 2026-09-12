from __future__ import annotations

import io
import json
import os
import time
import tracemalloc
import threading
import unittest
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import httpx

from rikkahub_gateway.server import (
    DEFAULT_MAX_BODY_BYTES, DEFAULT_MAX_STREAM_BYTES, GatewayApplication,
    GatewayError, _GatewayHandler, _ProtectedSSEQuarantine, _parse_sse_event,
)
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from rikkahub_gateway.tests.test_gateway_stream_completion import event


DONE = b"data: [DONE]\n\n"
OBSERVED_METRICS = {}
ANSWER = event({"content": "synthetic final answer"}, "stop") + DONE
# Repeated SSE metadata, like a token-wise provider stream. The total wire
# transfer can exceed the old 2 MiB ceiling while individual events stay tiny.
THINKING = b'data: {"id":"synthetic-completion-id","model":"synthetic-model","object":"chat.completion.chunk","created":1234,"system_fingerprint":"synthetic-fingerprint","choices":[{"index":0,"delta":{"reasoning_content":"x"},"finish_reason":null}]}\n\n'
TOOLS = [{"type": "function", "function": {"name": "stbrain_health", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}]
TOOL_TAIL = event({"tool_calls": [{"index": 0, "id": "synthetic-call-1", "type": "function", "function": {"name": "stbrain_health", "arguments": "{}"}}]}, "tool_calls") + DONE


class Chunks(httpx.SyncByteStream):
    def __init__(self, count=1, *, tail=ANSWER, wait_seconds=0.0):
        self.count = count
        self.tail = tail
        self.wait_seconds = wait_seconds
        self.closed = False

    def __iter__(self):
        started = time.monotonic()
        for index in range(self.count):
            if self.wait_seconds:
                delay = started + self.wait_seconds * index / max(1, self.count - 1) - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            yield THINKING
        yield self.tail

    def close(self):
        self.closed = True


class SmallRecordingWriter:
    def __init__(self):
        self.count = 0
        self.bytes = 0
        self.tail = b""

    def write(self, raw):
        self.count += raw.count(b"data:")
        self.bytes += len(raw)
        self.tail = (self.tail + raw)[-8192:]
        return len(raw)

    def flush(self):
        pass


class LongStreamLimitTests(unittest.TestCase):
    def run_stream(self, stream, *, cfg=None, protected=False, tools=False, measure_memory=False):
        requests = []
        def upstream(request):
            requests.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
        client = httpx.Client(transport=httpx.MockTransport(upstream))
        self.addCleanup(client.close)
        chosen = cfg or config()
        app = GatewayApplication(chosen, control=FakeControl(), upstream=client)
        prepared = app.prepare_turn({
            "model": chosen.public_model, "stream": True,
            "messages": [{"role": "user", "content": "capability-1" if protected else "synthetic test"}],
            **({"tools": TOOLS} if tools else {}),
        }, {"Authorization": f"Bearer {chosen.gateway_token}"})
        writer = SmallRecordingWriter()
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        handler.wfile = writer
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False
        if measure_memory:
            tracemalloc.start()
        try:
            handler._proxy_stream(prepared)
        except GatewayError as error:
            code = error.code
        else:
            code = None
            for line in writer.tail.splitlines():
                if line.startswith(b"data: {"):
                    payload = json.loads(line[6:])
                    if "error" in payload:
                        code = payload["error"]["code"]
        finally:
            peak = tracemalloc.get_traced_memory()[1] if measure_memory else None
            if measure_memory:
                tracemalloc.stop()
        self.assertEqual(1, len(requests), "one upstream request only; no automatic replay")
        self.assertTrue(stream.closed)
        if code or not tools:
            self.assertIsNone(app._current_session)
        else:
            self.assertEqual({"synthetic-call-1"}, prepared.session.expected_tool_call_ids)
            app.finish_turn(prepared, keep_for_tools=False)
        return code, writer, peak

    def test_long_reasoning_over_three_mib_reaches_answer_with_bounded_memory(self):
        count = (3 * 1024 * 1024 // len(THINKING)) + 1
        code, writer, peak = self.run_stream(Chunks(count), measure_memory=True)
        OBSERVED_METRICS["three_mib_memory_check"] = {
            "upstream_wire_bytes": count * len(THINKING) + len(ANSWER),
            "client_wire_bytes": writer.bytes,
            "gateway_python_peak_bytes": peak,
        }
        self.assertIsNone(code)
        self.assertEqual(count + 2, writer.count)
        self.assertIn(b"synthetic final answer", writer.tail)
        self.assertGreater(writer.bytes, DEFAULT_MAX_BODY_BYTES)
        self.assertLess(peak, DEFAULT_MAX_BODY_BYTES, f"gateway retained {peak} bytes of a long response")

    def test_long_reasoning_over_two_mib_reaches_bound_tool_tail(self):
        count = (DEFAULT_MAX_BODY_BYTES // len(THINKING)) + 50
        code, writer, _ = self.run_stream(Chunks(count, tail=TOOL_TAIL), tools=True)
        self.assertIsNone(code)
        self.assertEqual(1, writer.tail.count(b"synthetic-call-1"))
        self.assertEqual(1, writer.tail.count(DONE))

    def test_cumulative_stream_ceiling_is_independent_and_explicit(self):
        code, _, _ = self.run_stream(Chunks(20), cfg=replace(config(), max_stream_bytes=1024))
        self.assertEqual("upstream_stream_limit_reached", code)

    def test_single_event_buffer_ceiling_remains_bounded(self):
        code, _, _ = self.run_stream(Chunks(1, tail=event({"content": "a" * 1100}, "stop") + DONE), cfg=replace(config(), max_body_bytes=1024))
        self.assertEqual("upstream_buffer_limit_reached", code)

    def test_missing_event_delimiter_cannot_accumulate_unbounded(self):
        code, _, _ = self.run_stream(Chunks(1, tail=b'data: {"x":"' + b"a" * 1100), cfg=replace(config(), max_body_bytes=1024))
        self.assertEqual("upstream_buffer_limit_reached", code)

    def test_full_buffer_sensitive_history_retains_its_separate_ceiling(self):
        code, writer, _ = self.run_stream(Chunks(20), cfg=replace(config(), max_body_bytes=1024), protected=True)
        self.assertEqual("upstream_buffer_limit_reached", code)
        self.assertEqual(0, writer.count)

    def test_tool_tail_cannot_exceed_its_buffer_limit(self):
        first = event({"tool_calls": [{"index": 0, "id": "synthetic-call-1", "type": "function", "function": {"name": "stbrain_health", "arguments": "{"}}]})
        additions = b"".join(event({"tool_calls": [{"index": 0, "function": {"arguments": " " * 120}}]}) for _ in range(10))
        code, writer, _ = self.run_stream(Chunks(1, tail=first + additions + event({"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}, "tool_calls") + DONE), cfg=replace(config(), max_body_bytes=1024), tools=True)
        self.assertEqual("upstream_buffer_limit_reached", code)
        self.assertNotIn(b"synthetic-call-1", writer.tail)

    def test_quarantine_buffer_cannot_accumulate_unbounded(self):
        quarantine = _ProtectedSSEQuarantine(("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg",), 1024)
        self.assertEqual([], quarantine.feed(_parse_sse_event(event({"content": "ABCDEFGH"}))))
        with self.assertRaises(GatewayError) as raised:
            for _ in range(30):
                quarantine.feed(_parse_sse_event(event({"role": "assistant"})))
        self.assertEqual("upstream_buffer_limit_reached", raised.exception.code)

    def test_secret_after_old_two_mib_boundary_remains_blocked(self):
        count = DEFAULT_MAX_BODY_BYTES // len(THINKING) + 1
        tail = event({"reasoning_content": "capab"}) + event({"content": "ility-1"}, "stop") + DONE
        code, writer, _ = self.run_stream(Chunks(count, tail=tail))
        self.assertEqual("upstream_protected_value", code)
        self.assertNotIn(b"capability-1", writer.tail)
        self.assertNotIn(b'"finish_reason":"stop"', writer.tail)

    def test_stream_limit_configuration_is_bounded(self):
        self.assertEqual(64 * 1024 * 1024, DEFAULT_MAX_STREAM_BYTES)
        for invalid in (1023, DEFAULT_MAX_STREAM_BYTES + 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                replace(config(), max_stream_bytes=invalid)

    @unittest.skipUnless(os.environ.get("STBRAIN_LONG_STREAM_TEST") == "1", "explicit 35-second synthetic upstream check")
    def test_actual_thirty_five_second_reasoning_stream_exceeds_old_cap_and_finishes(self):
        """Real loopback HTTP client/gateway; synthetic upstream, no model call."""
        started = time.monotonic()
        count = DEFAULT_MAX_BODY_BYTES // len(THINKING) + 1000
        stream = Chunks(count, wait_seconds=35.0)
        requests = []
        def upstream(request):
            requests.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
        client = httpx.Client(transport=httpx.MockTransport(upstream))
        app = GatewayApplication(config(), control=FakeControl(), upstream=client)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        server.application = app
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            data=json.dumps({"model": config().public_model, "stream": True, "messages": [{"role": "user", "content": "synthetic 35 second long reasoning"}]}).encode(),
            headers={"Authorization": f"Bearer {config().gateway_token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                raw = response.read()
                self.assertEqual(200, response.status)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
            client.close()
        self.assertGreaterEqual(time.monotonic() - started, 35.0)
        OBSERVED_METRICS["long_loopback_check"] = {
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "upstream_wire_bytes": count * len(THINKING) + len(ANSWER),
            "client_wire_bytes": len(raw),
            "sse_event_count": raw.count(b"data:"),
            "upstream_request_count": len(requests),
            "saw_final_answer": b"synthetic final answer" in raw,
            "saw_error": b'"error"' in raw,
        }
        self.assertGreater(len(raw), DEFAULT_MAX_BODY_BYTES)
        self.assertEqual(1, len(requests))
        self.assertEqual(count + 2, raw.count(b"data:"))
        self.assertNotIn(b'"error"', raw)
        self.assertIn(b"synthetic final answer", raw)
        self.assertEqual(1, raw.count(DONE))
        self.assertIsNone(app._current_session)
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
