from __future__ import annotations

import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import (
    GatewayApplication,
    _GatewayHandler,
    _RequestPerformance,
)
from rikkahub_gateway.tests.test_gateway import FakeControl, config


PROMPT_MARKER = "performance-prompt-must-not-be-logged"
SCHEMA_MARKER = "performance-schema-must-not-be-logged"
OUTPUT_MARKER = "performance-output-must-not-be-logged"


class _RecordingWriter:
    def __init__(self) -> None:
        self._buffer = io.BytesIO()
        self._lock = threading.Lock()
        self.written = threading.Event()

    def write(self, value: bytes) -> int:
        with self._lock:
            size = self._buffer.write(value)
        self.written.set()
        return size

    def flush(self) -> None:
        return

    def snapshot(self) -> bytes:
        with self._lock:
            return self._buffer.getvalue()


class _GatedSSEStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.paused = threading.Event()
        self.release = threading.Event()

    def __iter__(self):
        for index, chunk in enumerate(self.chunks):
            yield chunk
            if index == 0:
                self.paused.set()
                if not self.release.wait(timeout=3):
                    raise TimeoutError("test stream was not released")


class GatewayPerformanceLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = GatewayApplication(config(), control=FakeControl())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        self.server.application = self.app  # type: ignore[attr-defined]
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = (
            f"http://127.0.0.1:{self.server.server_port}"
            "/v1/chat/completions"
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        self.app.upstream.close()

    def post_raw(self, body: bytes) -> tuple[int, bytes]:
        request = urllib.request.Request(
            self.url,
            method="POST",
            data=body,
            headers={
                "Authorization": "Bearer " + config().gateway_token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()

    @staticmethod
    def performance_record(records) -> tuple[dict, str]:
        assert len(records) == 1
        text = records[0].getMessage()
        return json.loads(text), text

    @staticmethod
    def assert_no_sensitive_values(test: unittest.TestCase, text: str) -> None:
        for marker in (
            PROMPT_MARKER,
            SCHEMA_MARKER,
            OUTPUT_MARKER,
            "capability-1",
            config().gateway_token,
            config().upstream_api_key,
        ):
            test.assertNotIn(marker, text)

    def test_nonstream_record_has_safe_phase_metrics_only(self) -> None:
        self.app.upstream.close()
        self.app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "model": "upstream-model",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": OUTPUT_MARKER,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 260000,
                            "completion_tokens": 321,
                            "prompt_cache_hit_tokens": 250000,
                            "prompt_cache_miss_tokens": 10000,
                            "provider_private_metadata": OUTPUT_MARKER,
                        },
                    },
                )
            )
        )
        body = json.dumps(
            {
                "model": config().public_model,
                "messages": [{"role": "user", "content": PROMPT_MARKER}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "safe_read",
                            "description": SCHEMA_MARKER,
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                "stream": False,
            }
        ).encode("utf-8")

        emitted = threading.Event()
        original_emit = _RequestPerformance.emit

        def emit_and_signal(performance):
            original_emit(performance)
            emitted.set()

        # A client can finish reading Content-Length before the server reaches
        # its finally/log block. Wait for that event instead of racing assertLogs.
        with patch.object(_RequestPerformance, "emit", emit_and_signal):
            with self.assertLogs("stiller.rikkahub.performance", level="INFO") as logs:
                status, response = self.post_raw(body)
                self.assertTrue(emitted.wait(timeout=3), "server did not emit completion metrics")

        self.assertEqual(200, status)
        self.assertIn(OUTPUT_MARKER.encode("utf-8"), response)
        record, text = self.performance_record(logs.records)
        self.assertEqual(
            {
                "event",
                "request_id",
                "observation_contract",
                "request_started_at_utc",
                "request_finished_at_utc",
                "model",
                "model_is_known",
                "continuation",
                "context_layout",
                "stable_context_bytes",
                "dynamic_context_bytes",
                "stable_context_changed",
                "dynamic_context_changed",
                "wire_tools_changed",
                "prior_input_prefix_preserved",
                "usage_observed",
                "usage_snapshot",
                "usage_snapshot_complete",
                "usage_snapshot_consistent",
                "usage_snapshot_invalid_fields",
                "stage",
                "stream",
                "full_buffer",
                "tool_tail",
                "body_bytes",
                "message_count",
                "tool_count",
                "injected_message_bytes",
                "upstream_request_bytes",
                "upstream_response_bytes",
                "client_response_bytes",
                "http_status",
                "upstream_status",
                "body_read_ms",
                "json_parse_ms",
                "prepare_turn_ms",
                "upstream_header_ms",
                "upstream_first_chunk_ms",
                "client_first_byte_ms",
                "upstream_total_ms",
                "prompt_tokens",
                "completion_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
                "total_ms",
            },
            set(record),
        )
        self.assertEqual("gateway_request_performance", record["event"])
        self.assertEqual("complete", record["stage"])
        self.assertRegex(record["request_id"], r"^[0-9a-f]{32}$")
        self.assertFalse(record["stream"])
        self.assertFalse(record["full_buffer"])
        self.assertFalse(record["tool_tail"])
        self.assertEqual(len(body), record["body_bytes"])
        self.assertEqual(1, record["message_count"])
        self.assertEqual(1, record["tool_count"])
        self.assertEqual(200, record["upstream_status"])
        self.assertEqual(200, record["http_status"])
        self.assertEqual(260000, record["prompt_tokens"])
        self.assertEqual(321, record["completion_tokens"])
        self.assertEqual(250000, record["prompt_cache_hit_tokens"])
        self.assertEqual(10000, record["prompt_cache_miss_tokens"])
        for field in (
            "prompt_tokens",
            "completion_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        ):
            self.assertIs(type(record[field]), int, field)
        for field in (
            "body_read_ms",
            "json_parse_ms",
            "prepare_turn_ms",
            "upstream_header_ms",
            "upstream_first_chunk_ms",
            "client_first_byte_ms",
            "upstream_total_ms",
            "total_ms",
        ):
            self.assertIsInstance(record[field], (int, float), field)
            self.assertGreaterEqual(record[field], 0, field)
        for field in (
            "injected_message_bytes",
            "upstream_request_bytes",
            "upstream_response_bytes",
            "client_response_bytes",
        ):
            self.assertGreater(record[field], 0, field)
        self.assert_no_sensitive_values(self, text)
        self.assertIsNone(logs.records[0].exc_info)

    def test_invalid_json_emits_one_safe_failure_record(self) -> None:
        body = (b'{"messages":["' + PROMPT_MARKER.encode("utf-8") + b'"],')

        with self.assertLogs("stiller.rikkahub.performance", level="INFO") as logs:
            status, _ = self.post_raw(body)

        self.assertEqual(400, status)
        record, text = self.performance_record(logs.records)
        self.assertEqual("gateway_request_performance_failure", record["event"])
        self.assertEqual("json_parse", record["stage"])
        self.assertEqual(len(body), record["body_bytes"])
        self.assertEqual(400, record["http_status"])
        self.assertIsInstance(record["body_read_ms"], (int, float))
        self.assertIsInstance(record["json_parse_ms"], (int, float))
        self.assertIsNone(record["prepare_turn_ms"])
        self.assertIsNone(record["upstream_header_ms"])
        self.assertIsNone(record["prompt_tokens"])
        self.assertIsNone(record["completion_tokens"])
        self.assertIsNone(record["prompt_cache_hit_tokens"])
        self.assertIsNone(record["prompt_cache_miss_tokens"])
        self.assert_no_sensitive_values(self, text)

    def test_usage_ignores_non_integer_and_out_of_range_metadata(self) -> None:
        telemetry = _RequestPerformance()
        telemetry.note_usage(
            {
                "usage": {
                    "prompt_tokens": "123",
                    "completion_tokens": True,
                    "prompt_cache_hit_tokens": -1,
                    "prompt_cache_miss_tokens": 10_000_001,
                    "private": OUTPUT_MARKER,
                }
            }
        )

        with self.assertLogs("stiller.rikkahub.performance", level="INFO") as logs:
            telemetry.emit()

        record, text = self.performance_record(logs.records)
        self.assertIsNone(record["prompt_tokens"])
        self.assertIsNone(record["completion_tokens"])
        self.assertIsNone(record["prompt_cache_hit_tokens"])
        self.assertIsNone(record["prompt_cache_miss_tokens"])
        self.assert_no_sensitive_values(self, text)


class GatewayPerformanceProgressiveStreamTests(unittest.TestCase):
    def test_telemetry_does_not_buffer_normal_sse(self) -> None:
        stream = _GatedSSEStream(
            [
                b'data: {"choices":[{"delta":{"reasoning_content":"phase-one"}}]}\n\n',
                b'data: {"choices":[{"delta":{"reasoning_content":"phase-two"},"finish_reason":"stop"}],"usage":{"prompt_tokens":900,"completion_tokens":12,"prompt_cache_hit_tokens":800,"prompt_cache_miss_tokens":100,"private":"performance-output-must-not-be-logged"}}\n\ndata: [DONE]\n\n',
            ]
        )
        app = GatewayApplication(config(), control=FakeControl())
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    stream=stream,
                )
            )
        )
        prepared = app.prepare_turn(
            {
                "model": config().public_model,
                "messages": [{"role": "user", "content": PROMPT_MARKER}],
                "stream": True,
            },
            {"Authorization": "Bearer " + config().gateway_token},
        )
        writer = _RecordingWriter()
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        handler.wfile = writer
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False
        telemetry = _RequestPerformance(stream=True)
        handler._request_performance = telemetry
        errors: list[BaseException] = []

        def run() -> None:
            try:
                handler._proxy_stream(prepared)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(stream.paused.wait(timeout=2))
            self.assertTrue(writer.written.wait(timeout=2))
            prefix = writer.snapshot()
            self.assertIn(b"phase-one", prefix)
            self.assertNotIn(b"phase-two", prefix)
            self.assertTrue(worker.is_alive())
            stream.release.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertIn(b"phase-two", writer.snapshot())
            self.assertEqual("complete", telemetry.stage)
            self.assertFalse(telemetry.full_buffer)
            self.assertFalse(telemetry.tool_tail)
            self.assertIsNotNone(telemetry.upstream_header_ms)
            self.assertIsNotNone(telemetry.upstream_first_chunk_ms)
            self.assertIsNotNone(telemetry.client_first_byte_ms)
            with self.assertLogs(
                "stiller.rikkahub.performance", level="INFO"
            ) as logs:
                telemetry.emit()
            record = json.loads(logs.records[0].getMessage())
            self.assertTrue(record["stream"])
            self.assertEqual("complete", record["stage"])
            self.assertEqual(900, record["prompt_tokens"])
            self.assertEqual(12, record["completion_tokens"])
            self.assertEqual(800, record["prompt_cache_hit_tokens"])
            self.assertEqual(100, record["prompt_cache_miss_tokens"])
            GatewayPerformanceLogTests.assert_no_sensitive_values(
                self, logs.records[0].getMessage()
            )
        finally:
            stream.release.set()
            app.finish_turn(prepared, keep_for_tools=False)
            app.upstream.close()


if __name__ == "__main__":
    unittest.main()
