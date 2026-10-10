from __future__ import annotations

import gc
import json
import os
import socket
import tracemalloc
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import (
    DEFAULT_MAX_BODY_BYTES, DEFAULT_MAX_STREAM_BYTES,
    DEFAULT_MAX_TOOL_TAIL_BYTES, MAX_TOOL_TAIL_BYTES,
    GatewayApplication, GatewayConfig, GatewayError, _GatewayHandler,
    _ParsedSSEEvent, _ProtectedSSEQuarantine, _RequestPerformance, _canonical_client_sse_event,
    _observable_failure_code, _parse_sse_event,
)
from rikkahub_gateway.tests.test_gateway import FakeControl, config


DONE = b"data: [DONE]\n\n"
TOOL_ID = "synthetic-call-1"
TOOL_SCHEMA = [{"type": "function", "function": {
    "name": "orbis_games_install", "parameters": {
        "type": "object", "properties": {"html": {"type": "string"}},
        "required": ["html"], "additionalProperties": False,
    },
}}]


def event(delta=None, finish=None, *, usage=None):
    payload = {
        "id": "chatcmpl-synthetic-00000000000000000000000000000001",
        "object": "chat.completion.chunk", "created": 1790690000,
        "model": "synthetic-model", "system_fingerprint": "fp_synthetic_0123456789",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }
    if usage is not None:
        payload["usage"] = usage
    result = _canonical_client_sse_event(_ParsedSSEEvent(raw=b"", payload=payload))
    assert result is not None
    return result.raw


def html_arguments(size):
    opening, closing = "<!doctype html>\n<html><body>\n", "\n</body></html>"
    fill = '<p class="item">synthetic text</p>\n'
    space = size - len(opening) - len(closing)
    html = opening + (fill * (space // len(fill) + 1))[:space] + closing
    assert len(html.encode("utf-8")) == size
    return json.dumps({"html": html}, ensure_ascii=False, separators=(",", ":"))


def tool_events(arguments, fragment_chars):
    yield event({"tool_calls": [{
        "index": 0, "id": TOOL_ID, "type": "function",
        "function": {"name": "orbis_games_install", "arguments": ""},
    }]})
    for position in range(0, len(arguments), fragment_chars):
        yield event({"tool_calls": [{"index": 0, "function": {
            "arguments": arguments[position:position + fragment_chars],
        }}]})
    yield event({}, "tool_calls")
    yield DONE


class SyntheticStream(httpx.SyncByteStream):
    def __init__(self, factory, *, prefix=True):
        self.factory, self.prefix = factory, prefix
        self.closed = False
        self.wire_bytes = self.event_count = 0

    def __iter__(self):
        if self.prefix:
            value = event({"reasoning_content": "synthetic safe prefix"})
            self.wire_bytes += len(value)
            self.event_count += 1
            yield value
        for value in self.factory():
            self.wire_bytes += len(value)
            self.event_count += value.count(b"data: ")
            yield value

    def close(self):
        self.closed = True


class RecordingWriter:
    """Keep only small receipt facts and tool fragments, not the large SSE wire."""

    def __init__(self, *, capture_payloads=False):
        self.ids = []
        self.arguments = []
        self.errors = []
        self.finishes = []
        self.done = self.events = self.bytes = 0
        self.payloads = [] if capture_payloads else None

    def write(self, raw):
        self.bytes += len(raw)
        # Error delivery can contain both an error and the terminal delimiter.
        for segment in raw.split(b"\n\n"):
            if not segment:
                continue
            parsed = _parse_sse_event(segment + b"\n\n")
            self.events += 1
            if parsed.done:
                self.done += 1
            payload = parsed.payload
            if payload is None:
                continue
            if self.payloads is not None:
                self.payloads.append(payload)
            if "error" in payload:
                self.errors.append(payload["error"])
            for choice in payload.get("choices", []):
                if choice.get("finish_reason") is not None:
                    self.finishes.append(choice["finish_reason"])
                for call in choice.get("delta", {}).get("tool_calls", []):
                    if call.get("id"):
                        self.ids.append(call["id"])
                    arguments = call.get("function", {}).get("arguments")
                    if isinstance(arguments, str):
                        self.arguments.append(arguments)
        return len(raw)

    def flush(self):
        pass


class ToolTailLimitTests(unittest.TestCase):
    def run_stream(self, stream, *, cfg=None, protected=False, tools=True, measure=False, writer=None,
                   expected_failed_bind_count=0, performance=None, observe_exchange=None):
        chosen = cfg or config()
        requests = []

        def upstream(request):
            requests.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

        client = httpx.Client(transport=httpx.MockTransport(upstream))
        self.addCleanup(client.close)
        app = GatewayApplication(chosen, control=FakeControl(), upstream=client)
        prepared = app.prepare_turn({
            "model": chosen.public_model, "stream": True,
            "messages": [{"role": "user", "content": "capability-1" if protected else "synthetic test"}],
            **({"tools": TOOL_SCHEMA} if tools else {}),
        }, {"Authorization": "Bearer " + chosen.gateway_token})
        writer = writer or RecordingWriter()
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        handler.wfile = writer
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False
        if performance is not None:
            handler._request_performance = performance
        if measure:
            gc.collect()
            tracemalloc.start()
        raised = None
        try:
            with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")), \
                    patch.object(app, "bind_response_tool_calls", wraps=app.bind_response_tool_calls) as bind, \
                    patch.object(handler, "_commit_final_delivery", wraps=handler._commit_final_delivery) as commit, \
                    patch.object(handler, "_proxy_stream_attempt", wraps=handler._proxy_stream_attempt) as attempt:
                try:
                    handler._proxy_stream(prepared)
                except GatewayError as error:
                    raised = error
                bind_count, commit_count = bind.call_count, commit.call_count
                if observe_exchange is not None:
                    observe_exchange(attempt.call_args.args[1])
        finally:
            peak = tracemalloc.get_traced_memory()[1] if measure else None
            if measure:
                tracemalloc.stop()
        self.assertEqual(1, len(requests), "no model replay on a buffering failure")
        self.assertTrue(stream.closed)
        error = raised.payload()["error"] if raised else (writer.errors[-1] if writer.errors else None)
        if error:
            self.assertIsNone(app._current_session)
            self.assertEqual(expected_failed_bind_count, bind_count)
            self.assertEqual(0, commit_count)
        else:
            self.assertEqual(1, commit_count)
            if tools:
                self.assertEqual(1, bind_count)
                self.assertEqual({TOOL_ID}, prepared.session.expected_tool_call_ids)
            app.finish_turn(prepared, keep_for_tools=False)
        return error, writer, peak

    def assert_no_tool_or_success(self, writer):
        self.assertEqual([], writer.ids)
        self.assertEqual([], writer.arguments)
        self.assertEqual([], writer.finishes)
        # The error protocol can close with DONE; this is not a successful
        # finish/tool batch. No successful DONE is emitted before its error.
        self.assertEqual(1, len(writer.errors))
        self.assertEqual(1, writer.done)

    def test_defaults_keep_non_tool_buffers_unchanged(self):
        chosen = config()
        self.assertEqual(2 * 1024 * 1024, chosen.max_body_bytes)
        self.assertEqual(64 * 1024 * 1024, chosen.max_stream_bytes)
        self.assertEqual(32 * 1024 * 1024, chosen.max_tool_tail_bytes)
        self.assertEqual(DEFAULT_MAX_TOOL_TAIL_BYTES, MAX_TOOL_TAIL_BYTES)

    def test_18kib_two_character_tool_fragments_pass_exactly_once(self):
        arguments = html_arguments(18432)
        stream = SyntheticStream(lambda: tool_events(arguments, 2))
        error, writer, _ = self.run_stream(stream)
        self.assertIsNone(error)
        self.assertEqual([TOOL_ID], writer.ids)
        self.assertEqual(arguments, "".join(writer.arguments))
        self.assertEqual(["tool_calls"], writer.finishes)
        self.assertEqual(1, writer.done)
        self.assertEqual(10015, stream.event_count)  # tool tail plus one safe prefix
        self.assertEqual(3076660, stream.wire_bytes - len(event({"reasoning_content": "synthetic safe prefix"})))

    def test_same_18kib_fixture_fails_with_old_two_mib_tail_budget(self):
        arguments = html_arguments(18432)
        error, writer, _ = self.run_stream(
            SyntheticStream(lambda: tool_events(arguments, 2)),
            cfg=replace(config(), max_tool_tail_bytes=DEFAULT_MAX_BODY_BYTES),
        )
        self.assertEqual("upstream_tool_buffer_limit_reached", error["code"])
        self.assertIn("本批工具未执行，已显示内容不代表工具成功", error["message"])
        self.assert_no_tool_or_success(writer)

    def test_64kib_two_character_fragments_pass(self):
        arguments = html_arguments(65536)
        stream = SyntheticStream(lambda: tool_events(arguments, 2))
        error, writer, _ = self.run_stream(stream)
        self.assertIsNone(error)
        self.assertEqual([TOOL_ID], writer.ids)
        self.assertEqual(arguments, "".join(writer.arguments))
        self.assertEqual(35586, stream.event_count)

    def test_64kib_one_character_fragments_pass_with_bounded_peak(self):
        arguments = html_arguments(65536)
        stream = SyntheticStream(lambda: tool_events(arguments, 1))
        measure = os.environ.get("STBRAIN_TOOL_TAIL_MEMORY_TEST") == "1"
        error, writer, peak = self.run_stream(stream, cfg=replace(config(), timeout_seconds=300), measure=measure)
        self.assertIsNone(error)
        self.assertEqual([TOOL_ID], writer.ids)
        self.assertEqual(arguments, "".join(writer.arguments))
        self.assertEqual(71167, stream.event_count)
        self.assertEqual(21785889, stream.wire_bytes - len(event({"reasoning_content": "synthetic safe prefix"})))
        if measure:
            self.assertLess(peak, 256 * 1024 * 1024)
            print(json.dumps({"synthetic_tool_tail_memory": {
                "html_bytes": 65536, "argument_bytes": len(arguments.encode()),
                "fragment_chars": 1, "tail_events": stream.event_count - 1,
                "tail_bytes": 21785889, "python_peak_bytes": peak,
                "scope": "MockTransport gateway streaming + receipt writer, not process RSS; no live model/tool/network",
            }}), flush=True)

    def test_exact_budget_passes_and_one_byte_less_fails_without_tool_leak(self):
        arguments = html_arguments(512)
        amount = sum(len(raw) for raw in tool_events(arguments, 2))
        error, writer, _ = self.run_stream(
            SyntheticStream(lambda: tool_events(arguments, 2)),
            cfg=replace(config(), max_tool_tail_bytes=amount),
        )
        self.assertIsNone(error)
        self.assertEqual(arguments, "".join(writer.arguments))
        error, writer, _ = self.run_stream(
            SyntheticStream(lambda: tool_events(arguments, 2)),
            cfg=replace(config(), max_tool_tail_bytes=amount - 1),
        )
        self.assertEqual("upstream_tool_buffer_limit_reached", error["code"])
        self.assert_no_tool_or_success(writer)

    def test_before_headers_error_has_safe_human_message(self):
        arguments = html_arguments(512)
        error, writer, _ = self.run_stream(
            SyntheticStream(lambda: tool_events(arguments, 2), prefix=False),
            cfg=replace(config(), max_tool_tail_bytes=1024),
        )
        self.assertEqual("upstream_tool_buffer_limit_reached", error["code"])
        self.assertIn("本批工具未执行", error["message"])
        self.assertEqual(0, writer.events)
        self.assertNotIn("retry_class", error)

    def test_new_code_is_value_free_and_observable(self):
        self.assertEqual("upstream_tool_buffer_limit_reached", _observable_failure_code("upstream_tool_buffer_limit_reached"))
        self.assertEqual("gateway_request_failed", _observable_failure_code("untrusted failure with content"))

    def test_single_event_still_cannot_exceed_two_mib(self):
        arguments = html_arguments(DEFAULT_MAX_BODY_BYTES + 1)
        error, writer, _ = self.run_stream(SyntheticStream(lambda: tool_events(arguments, len(arguments))))
        self.assertEqual("upstream_buffer_limit_reached", error["code"])
        self.assert_no_tool_or_success(writer)

    def test_sensitive_full_buffer_still_cannot_exceed_two_mib(self):
        arguments = html_arguments(18432)
        error, writer, _ = self.run_stream(SyntheticStream(lambda: tool_events(arguments, 2)), protected=True)
        self.assertEqual("upstream_buffer_limit_reached", error["code"])
        self.assertEqual(0, writer.events)

    def test_sensitive_prefix_quarantine_still_uses_two_mib(self):
        quarantine = _ProtectedSSEQuarantine(("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefg",), DEFAULT_MAX_BODY_BYTES)
        self.assertEqual([], quarantine.feed(_parse_sse_event(event({"content": "ABCDEFGH"}))))
        held = _parse_sse_event(event({"role": "assistant", "unused": "x" * 1024}))
        with self.assertRaises(GatewayError) as raised:
            for _ in range(2048):
                quarantine.feed(held)
        self.assertEqual("upstream_buffer_limit_reached", raised.exception.code)

    def test_nonterminal_usage_keeps_ordinary_reply_progressive_and_in_order(self):
        writer = RecordingWriter(capture_payloads=True)
        source = [event({}, usage={})]
        source.extend(event({"content": "safe synthetic answer"}) for _ in range(8))
        source.extend((event({}, "stop"), event({}, usage={"completion_tokens": 8}), DONE))

        def response():
            for index, raw in enumerate(source):
                yield raw
                # First usage + eight content events stream immediately. A
                # finish, its trailing usage and DONE wait for the final commit.
                self.assertEqual(min(index + 1, 9), writer.events)
                self.assertEqual([], writer.finishes)
                self.assertEqual(0, writer.done)

        error, writer, _ = self.run_stream(
            SyntheticStream(response, prefix=False), writer=writer,
            cfg=replace(config(), max_body_bytes=1024), tools=False,
        )
        self.assertIsNone(error)
        expected = [{**_parse_sse_event(raw).payload, "model": config().public_model}
                    for raw in source[:-1]]
        self.assertEqual(expected, writer.payloads)
        self.assertEqual(["stop"], writer.finishes)
        self.assertEqual(1, writer.done)

    def test_usage_inside_tool_tail_waits_for_validation_and_preserves_order(self):
        writer = RecordingWriter(capture_payloads=True)
        source = [event({}, usage={"completion_tokens": 0})]
        source.extend(tool_events('{"html":"synthetic"}', 64))
        source.insert(2, event({}, usage={"completion_tokens": 1}))
        source.insert(-1, event({}, usage={"completion_tokens": 2}))

        def response():
            for raw in source:
                yield raw
                self.assertEqual([], writer.ids)
                self.assertEqual([], writer.finishes)
                self.assertEqual(0, writer.done)

        error, writer, _ = self.run_stream(SyntheticStream(response, prefix=False), writer=writer)
        self.assertIsNone(error)
        expected = [{**_parse_sse_event(raw).payload, "model": config().public_model}
                    for raw in source[:-1]]
        self.assertEqual(expected, writer.payloads)
        self.assertEqual([TOOL_ID], writer.ids)
        self.assertEqual(1, writer.done)

    def test_early_usage_never_releases_invalid_tool_batch(self):
        def response():
            yield event({}, usage={"completion_tokens": 0})
            yield from tool_events('{"unexpected":true}', 64)

        error, writer, _ = self.run_stream(SyntheticStream(response, prefix=False), expected_failed_bind_count=1)
        self.assertEqual("tool_arguments_schema_invalid", error["code"])
        self.assert_no_tool_or_success(writer)

    def test_usage_on_first_tool_or_finish_event_does_not_bypass_commit(self):
        for tools in (False, True):
            with self.subTest(tools=tools):
                writer = RecordingWriter(capture_payloads=True)
                source = list(tool_events('{"html":"synthetic"}', 64)) if tools else [
                    event({"content": "synthetic final"}, "stop"), DONE,
                ]
                first = dict(_parse_sse_event(source[0]).payload)
                first["usage"] = {"completion_tokens": 1}
                source[0] = _canonical_client_sse_event(_ParsedSSEEvent(raw=b"", payload=first)).raw

                def response():
                    for raw in source:
                        yield raw
                        self.assertEqual(0, writer.events, "usage must not release a tool/terminal event")

                error, writer, _ = self.run_stream(
                    SyntheticStream(response, prefix=False), writer=writer, tools=tools,
                )
                self.assertIsNone(error)
                self.assertEqual({**first, "model": config().public_model}, writer.payloads[0])
                self.assertEqual(1, writer.done)

    def test_early_usage_string_metadata_cannot_leak_protected_value(self):
        writer = RecordingWriter(capture_payloads=True)

        def response():
            yield event({"content": "synthetic safe prefix"})
            yield event({}, usage={"completion_tokens": 1, "provider_note": config().gateway_token})
            yield event({}, "stop")
            yield DONE

        error, writer, _ = self.run_stream(SyntheticStream(response, prefix=False), writer=writer, tools=False)
        self.assertEqual("upstream_protected_value", error["code"])
        self.assertNotIn(config().gateway_token, json.dumps(writer.payloads))
        self.assert_no_tool_or_success(writer)

    def test_progressive_usage_keeps_last_snapshot_without_summing_chunks(self):
        writer = RecordingWriter(capture_payloads=True)
        performance = _RequestPerformance()
        snapshots = [
            {"prompt_tokens": 17, "completion_tokens": 1, "total_tokens": 18},
            {"prompt_tokens": 17, "completion_tokens": 3, "total_tokens": 20},
            {"prompt_tokens": 17, "completion_tokens": 5, "total_tokens": 22},
        ]

        def response():
            yield event({"content": "synthetic first"}, usage=snapshots[0])
            yield event({"content": "synthetic second"}, usage=snapshots[1])
            yield event({}, "stop")
            yield event({}, usage=snapshots[2])
            yield DONE

        def verify_exchange(exchange):
            self.assertEqual(snapshots[-1], exchange.current_usage)
            self.assertEqual([snapshots[-1]], exchange.usage_attempts())
            self.assertEqual(snapshots[-1], exchange.total_usage())

        error, writer, _ = self.run_stream(
            SyntheticStream(response, prefix=False), writer=writer, tools=False,
            performance=performance, observe_exchange=verify_exchange,
        )
        self.assertIsNone(error)
        self.assertEqual(snapshots, [payload["usage"] for payload in writer.payloads if "usage" in payload])
        self.assertEqual({"prompt_tokens": 17, "completion_tokens": 5}, performance.usage_snapshot)
        self.assertEqual(5, performance.completion_tokens)

    def test_early_usage_does_not_bypass_split_secret_quarantine(self):
        writer = RecordingWriter(capture_payloads=True)
        secret = config().gateway_token
        prefix, suffix = secret[:16], secret[16:]

        def response():
            yield event({}, usage={"completion_tokens": 0})
            yield event({"content": prefix})
            self.assertEqual(1, writer.events, "possible secret prefix stays quarantined")
            yield event({}, usage={"completion_tokens": 1})
            self.assertEqual(1, writer.events, "usage cannot flush a quarantined prefix")
            yield event({"content": suffix}, "stop")
            yield DONE

        error, writer, _ = self.run_stream(SyntheticStream(response, prefix=False), writer=writer, tools=False)
        self.assertEqual("upstream_protected_value", error["code"])
        self.assertNotIn(prefix, json.dumps(writer.payloads))
        self.assert_no_tool_or_success(writer)

    def test_early_usage_does_not_make_incomplete_stream_successful(self):
        for content in (False, True):
            with self.subTest(content=content):
                def response():
                    yield event({}, usage={"completion_tokens": 0})
                    if content:
                        yield event({"content": "synthetic incomplete answer"})
                    yield event({}, usage={"completion_tokens": 1})

                error, writer, _ = self.run_stream(SyntheticStream(response, prefix=False), tools=False)
                self.assertEqual("upstream_incomplete_stream", error["code"])
                self.assert_no_tool_or_success(writer)

    def test_early_usage_does_not_enlarge_sensitive_history_buffer(self):
        def response():
            yield event({}, usage={"completion_tokens": 0})
            for _ in range(8):
                yield event({"content": "synthetic sensitive-history answer"})
            yield event({}, "stop")
            yield DONE

        error, writer, _ = self.run_stream(
            SyntheticStream(response, prefix=False), tools=False, protected=True,
            cfg=replace(config(), max_body_bytes=1024),
        )
        self.assertEqual("upstream_buffer_limit_reached", error["code"])
        self.assertEqual(0, writer.events)

    def test_non_tool_terminal_tail_does_not_get_tool_budget(self):
        def response():
            yield event({"content": "safe synthetic answer"}, "stop")
            for _ in range(8):
                yield event({}, usage={})
            yield DONE
        error, writer, _ = self.run_stream(SyntheticStream(response), cfg=replace(config(), max_body_bytes=1024), tools=False)
        self.assertEqual("upstream_buffer_limit_reached", error["code"])
        self.assert_no_tool_or_success(writer)

    def test_total_stream_limit_is_still_independent(self):
        arguments = html_arguments(512)
        error, writer, _ = self.run_stream(SyntheticStream(lambda: tool_events(arguments, 2)), cfg=replace(config(), max_stream_bytes=1024))
        self.assertEqual("upstream_stream_limit_reached", error["code"])
        self.assert_no_tool_or_success(writer)

    def test_tail_budget_requires_strict_integer_and_reviewed_range(self):
        for invalid in (True, False, 1024.0, float("nan"), float("inf"), "1024", None, 0, 1023, MAX_TOOL_TAIL_BYTES + 1):
            with self.subTest(value=repr(invalid)), self.assertRaises(ValueError):
                replace(config(), max_tool_tail_bytes=invalid)
        for valid in (1024, DEFAULT_MAX_BODY_BYTES, MAX_TOOL_TAIL_BYTES):
            self.assertEqual(valid, replace(config(), max_tool_tail_bytes=valid).max_tool_tail_bytes)

    def test_tail_budget_environment_default_valid_and_invalid(self):
        environment = {
            "STBRAIN_GATEWAY_TOKEN": "synthetic-gateway-token-32-characters-minimum",
            "STBRAIN_CONTROL_URL": "http://synthetic-control.test",
            "STBRAIN_HOST_TOKEN": "synthetic-host-token-32-characters-minimum",
            "STBRAIN_UPSTREAM_BASE_URL": "http://synthetic-model.test/v1",
            "STBRAIN_UPSTREAM_API_KEY": "synthetic-upstream-key",
            "STBRAIN_GATEWAY_MODEL": "synthetic-public-model",
            "STBRAIN_UPSTREAM_MODEL": "synthetic-upstream-model",
            "STBRAIN_HUMAN_TOKEN": "synthetic-human-token-32-characters-minimum",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(DEFAULT_MAX_TOOL_TAIL_BYTES, GatewayConfig.from_env().max_tool_tail_bytes)
            os.environ["STBRAIN_GATEWAY_MAX_TOOL_TAIL_BYTES"] = "1024"
            self.assertEqual(1024, GatewayConfig.from_env().max_tool_tail_bytes)
            for invalid in ("true", "false", "1024.0", "NaN", "1023", str(MAX_TOOL_TAIL_BYTES + 1)):
                os.environ["STBRAIN_GATEWAY_MAX_TOOL_TAIL_BYTES"] = invalid
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    GatewayConfig.from_env()


if __name__ == "__main__":
    unittest.main()
