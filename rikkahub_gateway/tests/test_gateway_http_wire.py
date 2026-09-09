from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx

from rikkahub_gateway.server import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_REQUEST_BODY_BYTES,
    MAX_REQUEST_BODY_BYTES,
    GatewayApplication,
    GatewayConfig,
    GatewayError,
    _GatewayHandler,
    _buffered_sse_contains_protected_value,
)
from mcp_server.control_server import ControlApplication
from mcp_server.service import SelfModelAccessService
from rikkahub_gateway.tests.test_gateway import FakeControl, FakeHumanControl, config
from rikkahub_gateway.tests.test_gateway_control_integration import DirectControlClient
from runtime import ModuleOneOnboardingStore, SelfModelStore


class _ConnectionAbortedWriter:
    def write(self, _: bytes) -> int:
        raise ConnectionAbortedError(10053, "client disconnected while receiving SSE")

    def flush(self) -> None:
        return


class _RecordingWriter:
    def __init__(self, on_write=None) -> None:
        self._buffer = io.BytesIO()
        self._lock = threading.Lock()
        self.written = threading.Event()
        self.on_write = on_write

    def write(self, value: bytes) -> int:
        if self.on_write is not None:
            self.on_write(value)
        with self._lock:
            size = self._buffer.write(value)
        self.written.set()
        return size

    def flush(self) -> None:
        return

    def snapshot(self) -> bytes:
        with self._lock:
            return self._buffer.getvalue()


class GatewayBodyLimitTests(unittest.TestCase):
    @staticmethod
    def make_handler(body: bytes, *, declared_length: int, limit: int) -> _GatewayHandler:
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(
            application=SimpleNamespace(
                config=SimpleNamespace(max_request_body_bytes=limit)
            )
        )
        handler.headers = {
            "Content-Length": str(declared_length),
            "Content-Type": "application/json; charset=utf-8",
        }
        handler.rfile = io.BytesIO(body)
        return handler

    def test_default_limit_matches_reviewed_provider_boundary(self) -> None:
        self.assertEqual(2 * 1024 * 1024, DEFAULT_MAX_BODY_BYTES)
        self.assertEqual(48 * 1024 * 1024, DEFAULT_MAX_REQUEST_BODY_BYTES)
        self.assertEqual(DEFAULT_MAX_REQUEST_BODY_BYTES, MAX_REQUEST_BODY_BYTES)
        self.assertEqual(DEFAULT_MAX_BODY_BYTES, config().max_body_bytes)
        self.assertEqual(
            DEFAULT_MAX_REQUEST_BODY_BYTES,
            config().max_request_body_bytes,
        )

    def test_configuration_cannot_exceed_reviewed_provider_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "reviewed 48 MiB boundary"):
            replace(
                config(),
                max_request_body_bytes=MAX_REQUEST_BODY_BYTES + 1,
            )

    def test_json_body_at_limit_is_accepted(self) -> None:
        limit = 1024
        body = b'{"value":"' + (b"a" * (limit - len(b'{"value":""}'))) + b'"}'
        self.assertEqual(limit, len(body))
        handler = self.make_handler(body, declared_length=len(body), limit=limit)
        payload = handler._read_json()
        self.assertEqual(limit - len(b'{"value":""}'), len(payload["value"]))

    def test_json_body_over_limit_is_rejected_before_reading(self) -> None:
        limit = 1024
        handler = self.make_handler(b"", declared_length=limit + 1, limit=limit)
        with self.assertRaises(GatewayError) as raised:
            handler._read_json()
        self.assertEqual(413, raised.exception.status)
        self.assertEqual("request_too_large", raised.exception.code)
        self.assertEqual(0, handler.rfile.tell())
        self.assertTrue(handler.close_connection)


class HumanDirectGrantWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.human_token = "wire-human-token-that-is-at-least-32-characters"
        self.host_control = FakeControl()
        self.human_control = FakeHumanControl()
        self.app = GatewayApplication(
            replace(
                config(),
                human_token=self.human_token,
                max_direct_grant_body_bytes=1024,
            ),
            control=self.host_control,
            human_control=self.human_control,
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        self.server.application = self.app  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = (
            f"http://127.0.0.1:{self.server.server_address[1]}"
            "/v1/human/direct-grants"
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.upstream.close()

    def post_raw(self, token: str, body: bytes) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()
        with response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_route_uses_only_human_token_and_enforces_its_small_body_contract(self) -> None:
        payload = {
            "request_id": "123e4567-e89b-42d3-a456-426614174000",
            "requested_scopes": ["learning_memory"],
        }
        body = json.dumps(payload).encode("utf-8")

        status, denied = self.post_raw(self.app.config.gateway_token, body)
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", denied["error"]["code"])
        self.assertEqual([], self.human_control.calls)

        status, issued = self.post_raw(self.human_token, body)
        self.assertEqual(200, status)
        self.assertEqual("stgrant_test-one-use-value", issued["grant_ref"])
        self.assertEqual(["learning_memory"], issued["scopes"])
        self.assertEqual(1, len(self.human_control.calls))
        self.assertEqual(0, self.host_control.seq)

        status, invalid = self.post_raw(
            self.human_token,
            json.dumps({**payload, "owner_id": "owner:foreign"}).encode("utf-8"),
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_direct_grant_request", invalid["error"]["code"])
        self.assertEqual(1, len(self.human_control.calls))

        status, oversized = self.post_raw(self.human_token, b"{" + b" " * 1024)
        self.assertEqual(413, status)
        self.assertEqual("request_too_large", oversized["error"]["code"])
        self.assertEqual(1, len(self.human_control.calls))


class _GatedSSEStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes], *, pause_after: int) -> None:
        self.chunks = chunks
        self.pause_after = pause_after
        self.paused = threading.Event()
        self.release = threading.Event()

    def __iter__(self):
        for index, chunk in enumerate(self.chunks):
            yield chunk
            if index == self.pause_after:
                self.paused.set()
                if not self.release.wait(timeout=3):
                    raise TimeoutError("test stream was not released")


class GatewayProgressiveStreamTests(unittest.TestCase):
    def make_handler(
        self,
        app: GatewayApplication,
        writer: _RecordingWriter,
    ) -> _GatewayHandler:
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        handler.wfile = writer
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False
        return handler

    @staticmethod
    def run_handler(
        handler: _GatewayHandler,
        prepared,
        errors: list[BaseException],
    ) -> None:
        try:
            handler._proxy_stream(prepared)
        except BaseException as exc:  # pragma: no cover - asserted by caller
            errors.append(exc)

    def test_reasoning_event_is_released_before_upstream_finishes(self) -> None:
        stream = _GatedSSEStream(
            [
                b'data: {"choices":[{"delta":{"reasoning_content":"phase-one."}}]}\n\n',
                b'data: {"choices":[{"delta":{"reasoning_content":"phase-two."},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            ],
            pause_after=0,
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
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "逐步思考"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        order: list[str] = []
        original_finish_turn = app.finish_turn

        def record_finish_turn(*args, **kwargs):
            order.append("finish_turn")
            return original_finish_turn(*args, **kwargs)

        app.finish_turn = record_finish_turn
        writer = _RecordingWriter(
            lambda value: order.append("terminal")
            if b'"finish_reason":"stop"' in value
            else None
        )
        handler = self.make_handler(app, writer)
        errors: list[BaseException] = []
        worker = threading.Thread(
            target=self.run_handler,
            args=(handler, prepared, errors),
            daemon=True,
        )
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
            self.assertLess(order.index("finish_turn"), order.index("terminal"))
            self.assertIsNone(app._current_session)
        finally:
            stream.release.set()
            app.upstream.close()

    def test_tool_call_tail_waits_for_complete_binding(self) -> None:
        stream = _GatedSSEStream(
            [
                b'data: {"choices":[{"index":0,"delta":{"reasoning_content":"checking."}}]}\n\n',
                b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call-progressive","type":"function","function":{"name":"stbrain_health","arguments":"{"}}]}}]}\n\n',
                b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"}"}}]},"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n',
            ],
            pause_after=1,
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
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "stbrain_health",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {},
                    },
                },
            }
        ]
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "检查健康"}],
                "stream": True,
                "tools": tools,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)
        errors: list[BaseException] = []
        worker = threading.Thread(
            target=self.run_handler,
            args=(handler, prepared, errors),
            daemon=True,
        )
        worker.start()
        try:
            self.assertTrue(stream.paused.wait(timeout=2))
            prefix = writer.snapshot()
            self.assertIn(b"checking", prefix)
            self.assertNotIn(b"call-progressive", prefix)
            stream.release.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            result = writer.snapshot()
            self.assertLess(result.index(b"checking"), result.index(b"call-progressive"))
            self.assertIn(b"data: [DONE]", result)
            self.assertEqual(
                {"call-progressive"}, prepared.session.expected_tool_call_ids
            )
        finally:
            stream.release.set()
            app.finish_turn(prepared, keep_for_tools=False)
            app.upstream.close()

    def test_multiline_data_tool_call_cannot_bypass_schema_binding(self) -> None:
        # SSE joins consecutive data lines with a newline.  The resulting JSON
        # is valid, but the old line-oriented final scanner could not see it and
        # released the native call without binding it.
        wire = (
            'data: {"choices":[{"index":0,\n'
            'data: "delta":{"tool_calls":[{"index":0,'
            '"id":"call-multiline-invalid","type":"function",'
            '"function":{"name":"stbrain_health",'
            '"arguments":"{\\"unexpected\\":true}"}}]},'
            '"finish_reason":"tool_calls"}]}\n\n'
            "data: [DONE]\n\n"
        ).encode("utf-8")
        app = GatewayApplication(config(), control=FakeControl())
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=wire,
                )
            )
        )
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "多行工具调用"}],
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "stbrain_health",
                            "parameters": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {},
                            },
                        },
                    }
                ],
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)

        try:
            with self.assertRaises(GatewayError) as caught:
                handler._proxy_stream(prepared)
            self.assertEqual("tool_arguments_schema_invalid", caught.exception.code)
            self.assertEqual(b"", writer.snapshot())
            self.assertIsNone(app._current_session)
        finally:
            app.upstream.close()

    def test_message_style_call_cannot_hide_beside_valid_delta_call(self) -> None:
        wire = (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call-unbound-message",
                                        "type": "function",
                                        "function": {
                                            "name": "unadvertised_tool",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-valid-delta",
                                        "type": "function",
                                        "function": {
                                            "name": "stbrain_health",
                                            "arguments": "{}",
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            )
            + "\n\ndata: [DONE]\n\n"
        ).encode("utf-8")
        app = GatewayApplication(config(), control=FakeControl())
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=wire,
                )
            )
        )
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "混入未绑定工具"}],
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "stbrain_health",
                            "parameters": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {},
                            },
                        },
                    }
                ],
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)

        try:
            with self.assertRaises(GatewayError) as caught:
                handler._proxy_stream(prepared)
            self.assertEqual("upstream_tool_calls_invalid", caught.exception.code)
            self.assertEqual(b"", writer.snapshot())
            self.assertIsNone(app._current_session)
        finally:
            app.upstream.close()

    def test_model_visible_protected_value_keeps_full_buffering(self) -> None:
        stream = _GatedSSEStream(
            [
                b': model-visible-comment-must-drop\n\n'
                b'data: {"choices":[{"delta":{"reasoning_content":"held-first."}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":"held-last."},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
            ],
            pause_after=0,
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
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "保护值续轮"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        visible = "model-visible-edit-challenge"
        prepared.session.protected_values.add(visible)
        prepared.payload["messages"].append(
            {"role": "tool", "content": json.dumps({"challenge_response": visible})}
        )
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)
        errors: list[BaseException] = []
        worker = threading.Thread(
            target=self.run_handler,
            args=(handler, prepared, errors),
            daemon=True,
        )
        worker.start()
        try:
            self.assertTrue(stream.paused.wait(timeout=2))
            self.assertEqual(b"", writer.snapshot())
            stream.release.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertIn(b"held-first", writer.snapshot())
            self.assertNotIn(b"model-visible-comment-must-drop", writer.snapshot())
        finally:
            stream.release.set()
            app.upstream.close()

    def test_non_sse_json_200_fails_before_any_client_bytes(self) -> None:
        app = GatewayApplication(config(), control=FakeControl())
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json={"choices": [{"message": {"content": "not-sse"}}]},
                )
            )
        )
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "非 SSE"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)

        try:
            with self.assertRaises(GatewayError) as caught:
                handler._proxy_stream(prepared)
            self.assertEqual("upstream_invalid_stream", caught.exception.code)
            self.assertEqual(b"", writer.snapshot())
            self.assertIsNone(app._current_session)
        finally:
            app.upstream.close()

    def test_comment_only_stream_fails_before_any_client_bytes(self) -> None:
        wire = (
            b": keepalive\n\n"
            b"event: ping\nid: 7\nretry: 1000\n\n"
            b"data:\n\n"
        )
        app = GatewayApplication(config(), control=FakeControl())
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=wire,
                )
            )
        )
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "仅注释流"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)

        try:
            with self.assertRaises(GatewayError) as caught:
                handler._proxy_stream(prepared)
            self.assertEqual("upstream_invalid_stream", caught.exception.code)
            self.assertEqual(b"", writer.snapshot())
            self.assertIsNone(app._current_session)
        finally:
            app.upstream.close()

    def test_comments_and_metadata_are_stripped_from_valid_data_stream(self) -> None:
        escaped = "".join(f"\\u{ord(char):04x}" for char in "capability-1")
        wire = (
            f": {escaped}\n\n"
            f"event: {escaped}\nid: {escaped}\nretry: 1000\n"
            'data: {"choices":[{"delta":{"content":"safe-data"},'
            '"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        ).encode("utf-8")
        app = GatewayApplication(config(), control=FakeControl())
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=wire,
                )
            )
        )
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "注释兼容"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)

        try:
            handler._proxy_stream(prepared)
            output = writer.snapshot()
            self.assertIn(b"safe-data", output)
            self.assertIn(b"data: [DONE]", output)
            self.assertNotIn(escaped.encode("utf-8"), output)
            self.assertNotIn(b"event:", output)
            self.assertNotIn(b"id:", output)
            self.assertNotIn(b"retry:", output)
            self.assertIsNone(app._current_session)
        finally:
            app.upstream.close()

    def test_truncated_protected_prefix_is_never_released_at_eof(self) -> None:
        app = GatewayApplication(config(), control=FakeControl())
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "截断保护值"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        protected = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg"
        self.assertEqual(43, len(protected))
        prepared.session.protected_values.add(protected)
        prefix = protected[:8]
        wire = (
            "data: "
            + json.dumps({"choices": [{"delta": {"content": prefix}}]})
            + "\n\ndata: [DONE]\n\n"
        ).encode("utf-8")
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=wire,
                )
            )
        )
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)

        try:
            with self.assertRaises(GatewayError) as caught:
                handler._proxy_stream(prepared)
            self.assertEqual("upstream_protected_value", caught.exception.code)
            self.assertEqual(b"", writer.snapshot())
            self.assertIsNone(app._current_session)
        finally:
            app.upstream.close()

    def test_seven_character_prefix_streams_without_false_positive(self) -> None:
        protected = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg"
        self.assertEqual(43, len(protected))
        prefix = protected[:7]
        stream = _GatedSSEStream(
            [
                (
                    "data: "
                    + json.dumps(
                        {"choices": [{"delta": {"content": prefix}}]}
                    )
                    + "\n\n"
                ).encode("utf-8"),
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n\n",
            ],
            pause_after=0,
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
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "短前缀误报"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        prepared.session.protected_values.add(protected)
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)
        errors: list[BaseException] = []
        worker = threading.Thread(
            target=self.run_handler,
            args=(handler, prepared, errors),
            daemon=True,
        )
        worker.start()

        try:
            self.assertTrue(stream.paused.wait(timeout=2))
            self.assertTrue(writer.written.wait(timeout=2))
            self.assertIn(prefix.encode("utf-8"), writer.snapshot())
            self.assertTrue(worker.is_alive())
            stream.release.set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertIn(b"data: [DONE]", writer.snapshot())
            self.assertIsNone(app._current_session)
        finally:
            stream.release.set()
            app.upstream.close()

    def test_full_buffer_scan_allows_one_to_seven_character_prefixes(self) -> None:
        protected = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg"
        self.assertEqual(43, len(protected))
        for length in range(1, 8):
            wire = (
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {"content": protected[:length]},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                )
                + "\n\ndata: [DONE]\n\n"
            ).encode("utf-8")
            with self.subTest(length=length):
                self.assertFalse(
                    _buffered_sse_contains_protected_value(wire, (protected,))
                )

    def test_full_scan_combines_short_prefix_across_fragmentable_paths(self) -> None:
        protected = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg"
        split = 4
        wire = (
            "data: "
            + json.dumps(
                {"choices": [{"delta": {"content": protected[:split]}}]}
            )
            + "\n\n"
            + "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "arguments": protected[split:]
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            )
            + "\n\ndata: [DONE]\n\n"
        ).encode("utf-8")
        self.assertTrue(
            _buffered_sse_contains_protected_value(wire, (protected,))
        )

    def test_protected_value_cannot_cross_reasoning_and_content(self) -> None:
        app = GatewayApplication(config(), control=FakeControl())
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "跨思考和正文保护值"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        protected = next(iter(prepared.session.protected_values))
        midpoint = max(1, len(protected) // 2)
        wire = (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {"delta": {"reasoning_content": protected[:midpoint]}}
                    ]
                }
            )
            + "\n\n"
            + "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {"content": protected[midpoint:]},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
            + "\n\ndata: [DONE]\n\n"
        ).encode("utf-8")
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=wire,
                )
            )
        )
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)

        try:
            with self.assertRaises(GatewayError) as caught:
                handler._proxy_stream(prepared)
            self.assertEqual("upstream_protected_value", caught.exception.code)
            self.assertEqual(b"", writer.snapshot())
            self.assertIsNone(app._current_session)
        finally:
            app.upstream.close()

    def test_protected_value_cannot_cross_content_and_tool_arguments(self) -> None:
        protected = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg"
        split = 8
        wire = (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {"index": 0, "delta": {"content": protected[:split]}}
                    ]
                }
            )
            + "\n\n"
            + "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-cross-field",
                                        "type": "function",
                                        "function": {
                                            "name": "stbrain_health",
                                            "arguments": protected[split:],
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            )
            + "\n\ndata: [DONE]\n\n"
        ).encode("utf-8")
        app = GatewayApplication(config(), control=FakeControl())
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=wire,
                )
            )
        )
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "跨可见和工具字段"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        prepared.session.protected_values.add(protected)
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)

        try:
            with self.assertRaises(GatewayError) as caught:
                handler._proxy_stream(prepared)
            self.assertEqual("upstream_protected_value", caught.exception.code)
            self.assertEqual(b"", writer.snapshot())
            self.assertIsNone(app._current_session)
        finally:
            app.upstream.close()

    def test_protected_value_cannot_cross_two_tool_argument_paths(self) -> None:
        protected = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg"
        midpoint = len(protected) // 2
        wire = (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-cross-tool-a",
                                        "type": "function",
                                        "function": {
                                            "name": "stbrain_health",
                                            "arguments": protected[:midpoint],
                                        },
                                    },
                                    {
                                        "index": 1,
                                        "id": "call-cross-tool-b",
                                        "type": "function",
                                        "function": {
                                            "name": "stbrain_health",
                                            "arguments": protected[midpoint:],
                                        },
                                    },
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            )
            + "\n\ndata: [DONE]\n\n"
        ).encode("utf-8")
        app = GatewayApplication(config(), control=FakeControl())
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=wire,
                )
            )
        )
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "跨两个工具参数"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        prepared.session.protected_values.add(protected)
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)

        try:
            with self.assertRaises(GatewayError) as caught:
                handler._proxy_stream(prepared)
            self.assertEqual("upstream_protected_value", caught.exception.code)
            self.assertEqual(b"", writer.snapshot())
            self.assertIsNone(app._current_session)
        finally:
            app.upstream.close()

    def test_reassembled_interleaved_tool_arguments_are_rescanned(self) -> None:
        protected = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg"
        split = 4
        events = [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-interleaved-a",
                                    "type": "function",
                                    "function": {
                                        "name": "tool_a",
                                        "arguments": '{"x":"' + protected[:split],
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call-interleaved-b",
                                    "type": "function",
                                    "function": {
                                        "name": "tool_b",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "arguments": protected[split:] + '"}'
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        wire = (
            "".join(
                "data: " + json.dumps(event) + "\n\n" for event in events
            )
            + "data: [DONE]\n\n"
        ).encode("utf-8")
        # The event/path scanner alone intentionally sees the interleaving as
        # separate streams; the fully reassembled call scan must still catch it.
        self.assertFalse(
            _buffered_sse_contains_protected_value(wire, (protected,))
        )

        app = GatewayApplication(config(), control=FakeControl())
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=wire,
                )
            )
        )
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "交错工具参数"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        prepared.session.protected_values.add(protected)
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)

        try:
            with self.assertRaises(GatewayError) as caught:
                handler._proxy_stream(prepared)
            self.assertEqual("upstream_protected_value", caught.exception.code)
            self.assertEqual(b"", writer.snapshot())
            self.assertIsNone(app._current_session)
        finally:
            app.upstream.close()

    def test_full_buffer_scan_detects_cross_channel_protected_value(self) -> None:
        app = GatewayApplication(config(), control=FakeControl())
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "续轮保护值"}],
                "stream": True,
            },
            {"Authorization": f"Bearer {config().gateway_token}"},
        )
        protected = "model-visible-cross-channel-secret"
        prepared.session.protected_values.add(protected)
        prepared.payload["messages"].append(
            {
                "role": "tool",
                "content": json.dumps({"challenge_response": protected}),
            }
        )
        midpoint = len(protected) // 2
        wire = (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {"delta": {"reasoning_content": protected[:midpoint]}}
                    ]
                }
            )
            + "\n\n"
            + "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {"content": protected[midpoint:]},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
            + "\n\ndata: [DONE]\n\n"
        ).encode("utf-8")
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=wire,
                )
            )
        )
        writer = _RecordingWriter()
        handler = self.make_handler(app, writer)

        try:
            with self.assertRaises(GatewayError) as caught:
                handler._proxy_stream(prepared)
            self.assertEqual("upstream_protected_value", caught.exception.code)
            self.assertEqual(b"", writer.snapshot())
            self.assertIsNone(app._current_session)
        finally:
            app.upstream.close()


class GatewayStreamDisconnectTests(unittest.TestCase):
    def test_aborted_tool_call_delivery_closes_turn_instead_of_arming_continuation(
        self,
    ) -> None:
        control = FakeControl()
        app = GatewayApplication(config(), control=control)
        wire = (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-aborted-delivery",
                                        "type": "function",
                                        "function": {
                                            "name": "stbrain_health",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            )
            + "\n\ndata: [DONE]\n\n"
        ).encode("utf-8")
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=wire,
                )
            )
        )
        headers = {
            "Authorization": f"Bearer {config().gateway_token}",
            "X-ST-Thread-ID": "thread:aborted-delivery",
        }
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "stbrain_health",
                    "description": "Read-only health check",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "检查健康"}],
                "stream": True,
                "tools": tools,
            },
            headers,
        )
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        handler.wfile = _ConnectionAbortedWriter()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False

        try:
            handler._proxy_stream(prepared)

            next_turn = app.prepare_turn(
                {
                    "model": "stiller-rikka",
                    "messages": [{"role": "user", "content": "下一条真实消息"}],
                    "tools": tools,
                },
                headers,
            )
            self.assertEqual(2, control.seq)
            app.finish_turn(next_turn, keep_for_tools=False)
            self.assertEqual("/v1/host/context/close", control.calls[-1][0])
        finally:
            app.upstream.close()

    def test_aborted_json_tool_call_delivery_closes_turn_instead_of_arming_continuation(
        self,
    ) -> None:
        control = FakeControl()
        app = GatewayApplication(config(), control=control)
        app.upstream.close()
        app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "model": "real-model",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-aborted-json-delivery",
                                            "type": "function",
                                            "function": {
                                                "name": "stbrain_health",
                                                "arguments": "{}",
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                    },
                )
            )
        )
        headers = {
            "Authorization": f"Bearer {config().gateway_token}",
            "X-ST-Thread-ID": "thread:aborted-json-delivery",
        }
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "stbrain_health",
                    "description": "Read-only health check",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        prepared = app.prepare_turn(
            {
                "model": "stiller-rikka",
                "messages": [{"role": "user", "content": "检查健康"}],
                "tools": tools,
            },
            headers,
        )
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        handler.wfile = _ConnectionAbortedWriter()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None

        try:
            handler._proxy_json(prepared)

            next_turn = app.prepare_turn(
                {
                    "model": "stiller-rikka",
                    "messages": [{"role": "user", "content": "下一条真实消息"}],
                    "tools": tools,
                },
                headers,
            )
            self.assertEqual(2, control.seq)
            app.finish_turn(next_turn, keep_for_tools=False)
            self.assertEqual("/v1/host/context/close", control.calls[-1][0])
        finally:
            app.upstream.close()


class GatewayRealHttpWireTests(unittest.TestCase):
    """Exercise urllib -> http.server -> gateway instead of a direct dict call."""

    def setUp(self) -> None:
        self.control = FakeControl()
        self.app = GatewayApplication(config(), control=self.control)
        self.upstream_calls = 0
        self.upstream_payloads: list[dict] = []

        def upstream(request: httpx.Request) -> httpx.Response:
            self.upstream_calls += 1
            payload = json.loads(request.content)
            self.upstream_payloads.append(payload)
            has_tool_result = any(
                message.get("role") == "tool" for message in payload["messages"]
            )
            if has_tool_result:
                choice = {
                    "index": 0,
                    "message": {"role": "assistant", "content": "完成"},
                    "finish_reason": "stop",
                }
            else:
                choice = {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "synthetic-wire-reasoning",
                        "tool_calls": [
                            {
                                "id": "call-wire",
                                "type": "function",
                                "function": {
                                    "name": "stbrain_health",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            return httpx.Response(
                200,
                json={
                    "id": f"wire-{self.upstream_calls}",
                    "model": "real-model",
                    "choices": [choice],
                },
            )

        self.app.upstream.close()
        self.app.upstream = httpx.Client(transport=httpx.MockTransport(upstream))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        self.server.application = self.app  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = (
            f"http://127.0.0.1:{self.server.server_address[1]}"
            "/v1/chat/completions"
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.upstream.close()

    def post(self, messages: list[dict], **extra: object) -> tuple[int, dict]:
        body = json.dumps(
            {"model": "stiller-rikka", "messages": messages, **extra},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {config().gateway_token}",
                "Content-Type": "application/json; charset=utf-8",
                # urllib canonicalizes this to X-st-thread-id on the wire.
                "X-ST-Thread-ID": "thread:real-wire",
                "Idempotency-Key": "event:real-wire",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_tool_continuation_reuses_wake_over_real_http(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "stbrain_health",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        status, first = self.post(
            [{"role": "user", "content": "开始"}],
            thinking={"type": "enabled"},
            reasoning={"effort": "high"},
            reasoning_effort="high",
            tools=tools,
            tool_choice="auto",
        )
        self.assertEqual(200, status)
        self.assertEqual("tool_calls", first["choices"][0]["finish_reason"])
        self.assertEqual("stiller-rikka", first["model"])
        self.assertIsNone(first["choices"][0]["message"]["content"])
        self.assertEqual(
            "synthetic-wire-reasoning",
            first["choices"][0]["message"]["reasoning_content"],
        )
        self.assertEqual(
            "stbrain_health",
            first["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
        )
        forwarded = self.upstream_payloads[0]
        self.assertEqual({"type": "enabled"}, forwarded["thinking"])
        self.assertEqual({"effort": "high"}, forwarded["reasoning"])
        self.assertEqual("high", forwarded["reasoning_effort"])
        self.assertEqual(tools, forwarded["tools"])
        self.assertEqual("auto", forwarded["tool_choice"])
        self.assertEqual(1, self.control.seq)

        status, second = self.post(
            [
                {"role": "user", "content": "开始"},
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "synthetic-wire-reasoning",
                    "tool_calls": [
                        {
                            "id": "call-wire",
                            "type": "function",
                            "function": {
                                "name": "stbrain_health",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-wire",
                    "content": '{"ok":true}',
                },
            ],
            thinking={"type": "enabled"},
            reasoning={"effort": "high"},
            reasoning_effort="high",
            tools=tools,
            tool_choice="auto",
        )
        self.assertEqual(200, status)
        self.assertEqual("stop", second["choices"][0]["finish_reason"])
        continued = self.upstream_payloads[1]
        self.assertEqual({"type": "enabled"}, continued["thinking"])
        self.assertEqual({"effort": "high"}, continued["reasoning"])
        self.assertEqual("high", continued["reasoning_effort"])
        self.assertEqual(tools, continued["tools"])
        self.assertEqual("auto", continued["tool_choice"])
        assistant_history = next(
            message
            for message in continued["messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        self.assertEqual(
            "synthetic-wire-reasoning",
            assistant_history["reasoning_content"],
        )
        self.assertEqual(1, self.control.seq)

        status, _ = self.post(
            [{"role": "user", "content": "下一条真实消息"}],
            tools=tools,
            tool_choice="auto",
        )
        self.assertEqual(200, status)
        self.assertEqual(2, self.control.seq)

    def test_nonstream_capability_echo_is_replaced_by_safe_gateway_error(self) -> None:
        self.app.upstream.close()
        self.app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "model": "real-model",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "leak capability-1",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    },
                )
            )
        )

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post([{"role": "user", "content": "恶意回显测试"}])
        self.assertEqual(502, caught.exception.code)
        raw = caught.exception.read().decode("utf-8")
        caught.exception.close()
        self.assertNotIn("capability-1", raw)
        self.assertEqual(
            "upstream_protected_value",
            json.loads(raw)["error"]["code"],
        )

    def test_nonstream_capability_in_reasoning_content_is_blocked(self) -> None:
        self.app.upstream.close()
        self.app.upstream = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "model": "real-model",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "reasoning_content": "leak capability-1",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    },
                )
            )
        )

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post([{"role": "user", "content": "思考字段恶意回显测试"}])
        self.assertEqual(502, caught.exception.code)
        raw = caught.exception.read().decode("utf-8")
        caught.exception.close()
        self.assertNotIn("capability-1", raw)
        self.assertEqual(
            "upstream_protected_value",
            json.loads(raw)["error"]["code"],
        )


class GatewayServerBoundWriteWireTests(unittest.TestCase):
    """Prove a native open -> write loop needs no model-visible capability."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "bound-wire.db"
        self.onboarding = ModuleOneOnboardingStore(
            self.database,
            capability_secret=b"bound-wire-wake-secret-that-is-at-least-32-bytes",
            wake_ttl_seconds=300,
            edit_challenge_ttl_seconds=300,
        )
        self.host_token = "bound-wire-host-token-that-is-longer-than-32"
        control_app = ControlApplication(
            self.onboarding,
            owner_id="owner:bound-wire",
            model_id="model:bound-wire",
            host_token=self.host_token,
            human_token="bound-wire-human-token-that-is-longer-than-32",
            human_actor_id="human:bound-wire",
        )
        self.service = SelfModelAccessService(
            SelfModelStore(self.database),
            model_id="model:bound-wire",
            owner_id="owner:bound-wire",
            onboarding=self.onboarding,
        )
        self.config = GatewayConfig(
            gateway_token="bound-wire-gateway-token-that-is-longer-than-32",
            control_url="http://unused.test",
            host_token=self.host_token,
            upstream_base_url="http://upstream.test/v1",
            upstream_api_key="bound-wire-upstream-key",
            public_model="stiller-bound-wire",
            upstream_model="real-model",
        )
        self.app = GatewayApplication(
            self.config,
            control=DirectControlClient(control_app, self.host_token),
        )

        def upstream(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            tool_messages = [
                message for message in payload["messages"] if message.get("role") == "tool"
            ]
            if not tool_messages:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-open-bound",
                            "type": "function",
                            "function": {"name": "stbrain_open", "arguments": "{}"},
                        }
                    ],
                }
                finish_reason = "tool_calls"
            elif len(tool_messages) == 1:
                opened = json.loads(tool_messages[-1]["content"])
                arguments = {
                    "intent": "acknowledge",
                    "write_context_ref": opened["write_context_ref"],
                    "expected_row_version": opened["row_version"],
                }
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-submit-bound",
                            "type": "function",
                            "function": {
                                "name": "submit_self_model_candidate",
                                "arguments": json.dumps(arguments, ensure_ascii=False),
                            },
                        }
                    ],
                }
                finish_reason = "tool_calls"
            else:
                message = {"role": "assistant", "content": "已由我确认。"}
                finish_reason = "stop"
            return httpx.Response(
                200,
                json={
                    "model": "real-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": finish_reason,
                        }
                    ],
                },
            )

        self.app.upstream.close()
        self.app.upstream = httpx.Client(transport=httpx.MockTransport(upstream))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        self.server.application = self.app  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = (
            f"http://127.0.0.1:{self.server.server_address[1]}"
            "/v1/chat/completions"
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.app.upstream.close()
        self.temp.cleanup()

    def current_capability(self) -> str:
        with self.onboarding._connect() as connection:
            wake = connection.execute(
                "SELECT wake_id FROM brain_wake_sessions "
                "WHERE owner_id = ? AND model_id = ? AND status = 'current' "
                "ORDER BY wake_seq DESC LIMIT 1",
                ("owner:bound-wire", "model:bound-wire"),
            ).fetchone()
        if wake is None:
            raise AssertionError("current wake was not issued before upstream call")
        return self.onboarding._capability(wake["wake_id"])

    def post(self, messages: list[dict], **extra: object) -> tuple[int, dict]:
        body = json.dumps(
            {
                "model": self.config.public_model,
                "messages": messages,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "stbrain_open",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "submit_self_model_candidate",
                            "parameters": {"type": "object"},
                        },
                    },
                ],
                "tool_choice": "auto",
                **extra,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.gateway_token}",
                "Content-Type": "application/json; charset=utf-8",
                "X-ST-Thread-ID": "thread:bound-wire",
                "Idempotency-Key": "event:bound-wire",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_native_open_then_server_bound_submit_stays_http_200(self) -> None:
        user = {"role": "user", "content": "如果愿意，我现在打开大脑。"}
        status, first = self.post([user])
        self.assertEqual(200, status)
        open_call = first["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual("stbrain_open", open_call["function"]["name"])

        opened = self.service.open_brain()
        opened_text = json.dumps(opened, ensure_ascii=False)
        self.assertEqual("public-tools/20", opened["contract_version"])
        self.assertEqual(
            "acknowledge",
            opened["current_action_contract"]["allowed_calls"][0]["intent"],
        )
        self.assertNotIn("wake_capability", opened_text)
        self.assertNotIn("challenge_response", opened_text)
        self.assertNotIn('"wake_id"', opened_text)

        open_tool = {
            "role": "tool",
            "tool_call_id": open_call["id"],
            "content": opened_text,
        }
        status, second = self.post(
            [user, {"role": "assistant", "content": None, "tool_calls": [open_call]}, open_tool]
        )
        self.assertEqual(200, status)
        submit_call = second["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(
            "submit_self_model_candidate", submit_call["function"]["name"]
        )
        arguments = json.loads(submit_call["function"]["arguments"])
        self.assertEqual(
            {"intent", "write_context_ref", "expected_row_version"}, set(arguments)
        )
        self.assertNotIn("wake_id", arguments)
        self.assertNotIn("wake_capability", arguments)

        submitted = self.service.submit_self_model_candidate(**arguments)
        self.assertEqual("advanced", submitted["decision"])
        submit_tool = {
            "role": "tool",
            "tool_call_id": submit_call["id"],
            "content": json.dumps(submitted, ensure_ascii=False),
        }
        status, third = self.post(
            [
                user,
                {"role": "assistant", "content": None, "tool_calls": [open_call]},
                open_tool,
                {"role": "assistant", "content": None, "tool_calls": [submit_call]},
                submit_tool,
            ]
        )
        self.assertEqual(200, status)
        self.assertEqual("stop", third["choices"][0]["finish_reason"])
        self.assertEqual(
            "module_intro",
            self.service.module_one_status()["state"]["stage"],
        )

    def test_nonstream_capability_as_json_key_is_blocked(self) -> None:
        self.app.upstream.close()
        observed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            capability = self.current_capability()
            observed.append(capability)
            return httpx.Response(
                200,
                json={
                    "model": "real-model",
                    "choices": [{"finish_reason": "stop"}],
                    capability: "hidden-in-a-key",
                },
            )

        self.app.upstream = httpx.Client(
            transport=httpx.MockTransport(handler)
        )

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post([{"role": "user", "content": "键名恶意回显测试"}])
        self.assertEqual(502, caught.exception.code)
        raw = caught.exception.read().decode("utf-8")
        caught.exception.close()
        self.assertEqual(1, len(observed))
        self.assertNotIn(observed[0], raw)
        self.assertEqual(
            "upstream_protected_value",
            json.loads(raw)["error"]["code"],
        )

    def test_stream_capability_split_across_events_releases_no_partial_bytes(self) -> None:
        self.app.upstream.close()
        observed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            capability = self.current_capability()
            observed.append(capability)
            midpoint = len(capability) // 2
            wire = (
                "data: "
                + json.dumps(
                    {"choices": [{"delta": {"content": capability[:midpoint]}}]}
                )
                + "\n\n"
                + "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "delta": {"content": capability[midpoint:]},
                                "finish_reason": "stop",
                            }
                        ]
                    }
                )
                + "\n\ndata: [DONE]\n\n"
            ).encode("utf-8")
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=wire,
            )

        self.app.upstream = httpx.Client(
            transport=httpx.MockTransport(handler)
        )

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(
                [{"role": "user", "content": "跨分片恶意回显测试"}],
                stream=True,
            )
        self.assertEqual(502, caught.exception.code)
        raw = caught.exception.read().decode("utf-8")
        caught.exception.close()
        self.assertEqual(1, len(observed))
        self.assertNotIn(observed[0], raw)
        self.assertEqual(
            "upstream_protected_value",
            json.loads(raw)["error"]["code"],
        )

    def test_tool_result_challenge_echo_is_blocked_end_to_end(self) -> None:
        challenge = "edit-challenge-value-that-must-stay-in-tool-context"

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            has_tool_result = any(
                message.get("role") == "tool" for message in payload["messages"]
            )
            if not has_tool_result:
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-challenge",
                                            "type": "function",
                                            "function": {
                                                "name": "submit_self_model_candidate",
                                                "arguments": '{"intent":"begin_edit"}',
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "I will repeat " + challenge,
                            },
                            "finish_reason": "stop",
                        }
                    ]
                },
            )

        self.app.upstream.close()
        self.app.upstream = httpx.Client(transport=httpx.MockTransport(handler))
        _, first = self.post([{"role": "user", "content": "开始编辑"}])
        call = first["choices"][0]["message"]["tool_calls"][0]

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(
                [
                    {"role": "user", "content": "开始编辑"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [call],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call-challenge",
                        "content": json.dumps(
                            {"decision": "challenge_issued", "challenge_response": challenge}
                        ),
                    },
                ]
            )
        self.assertEqual(502, caught.exception.code)
        raw = caught.exception.read().decode("utf-8")
        caught.exception.close()
        self.assertNotIn(challenge, raw)
        self.assertEqual(
            "upstream_protected_value",
            json.loads(raw)["error"]["code"],
        )


if __name__ == "__main__":
    unittest.main()
