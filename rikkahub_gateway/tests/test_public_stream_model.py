"""Public model projection only, after the existing SSE safety barriers."""
import copy
from dataclasses import replace
import json
import socket
import sqlite3
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import (
    GatewayApplication, GatewayError, _GatewayHandler,
    _canonical_client_sse_events,
)
from rikkahub_gateway.tests.test_gateway import FakeControl, config
from rikkahub_gateway.tests.test_gateway_http_wire import _GatedSSEStream, _RecordingWriter


PUBLIC = 'deepseek-v4-flash-vision-exp'
UPSTREAM = 'deepseek-flash'
TOOL = {'type': 'function', 'function': {'name': 'probe_echo', 'parameters': {
    'type': 'object', 'properties': {'model': {'type': 'string'}, 'text': {'type': 'string'}},
    'required': ['model', 'text'], 'additionalProperties': False}}}
ARGUMENTS = '{"model":"deepseek-flash","text":"synthetic"}'


def event(delta=None, finish=None, **extra):
    return {'id': 'synthetic-response', 'object': 'chat.completion.chunk', 'model': UPSTREAM,
            'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}], **extra}


def wire(payload):
    return b'data: ' + json.dumps(payload, ensure_ascii=False).encode() + b'\n\n'


def tool_delta(arguments=ARGUMENTS):
    return {'tool_calls': [{'index': 0, 'id': 'call-synthetic-model', 'type': 'function',
             'function': {'name': 'probe_echo', 'arguments': arguments}}]}


class PublicStreamModelTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, 'connect', side_effect=AssertionError('real_network_forbidden')).start()
        patch.object(socket, 'create_connection', side_effect=AssertionError('real_network_forbidden')).start()
        patch.object(sqlite3, 'connect', side_effect=AssertionError('real_database_forbidden')).start()

    def make(self, stream, *, tools=False, response_status=200, content_type='text/event-stream', on_write=None):
        app = GatewayApplication(replace(config(), public_model=PUBLIC, upstream_model=PUBLIC),
            control=FakeControl(), upstream=httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(response_status, headers={'Content-Type': content_type}, stream=stream)),
                trust_env=False))
        self.addCleanup(app.upstream.close)
        payload = {'model': PUBLIC, 'messages': [{'role': 'user', 'content': 'Synthetic model projection'}], 'stream': True}
        if tools:
            payload['tools'] = [copy.deepcopy(TOOL)]
        prepared = app.prepare_turn(payload, {'X-ST-Thread-ID': 'synthetic:model-projection'})
        writer = _RecordingWriter(on_write=on_write)
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        handler.wfile = writer
        handler.statuses = []
        handler.send_response = handler.statuses.append
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False
        return app, prepared, handler, writer

    def run_paused(self, handler, prepared, stream, *, before_release):
        errors = []
        def run():
            try:
                handler._proxy_stream(prepared)
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(stream.paused.wait(2))
            before_release()
        finally:
            stream.release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)

    def assert_payloads(self, raw, expected):
        parsed = _canonical_client_sse_events(raw)
        self.assertTrue(parsed[-1].done)
        self.assertEqual(b'data: [DONE]\n\n', parsed[-1].raw)
        actual = [item.payload for item in parsed if item.payload is not None]
        self.assertEqual([{**item, 'model': PUBLIC} for item in expected], actual)
        return actual

    def test_ordinary_events_stay_progressive_and_preserve_nested_metadata_and_usage(self):
        first = event({'reasoning_content': 'synthetic first'}, nested={'model': UPSTREAM},
                      system_fingerprint='synthetic-fingerprint')
        last = event({'content': 'synthetic last'}, 'stop')
        usage = {'choices': [], 'usage': {'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15}}
        stream = _GatedSSEStream([
            b': drop comment\nevent: drop-event\nid: drop-id\nretry: 5\n' + wire(first),
            wire(last) + wire(usage) + b'data: [DONE]\n\n'], pause_after=0)
        app, prepared, handler, writer = self.make(stream)
        def before_release():
            prefix = writer.snapshot()
            parsed = _canonical_client_sse_events(prefix)
            self.assertEqual([{**first, 'model': PUBLIC}], [item.payload for item in parsed])
            self.assertNotIn(b'synthetic last', prefix)
            self.assertIsNotNone(app._current_session)
        self.run_paused(handler, prepared, stream, before_release=before_release)
        self.assert_payloads(writer.snapshot(), [first, last, usage])
        self.assertNotIn(b'drop-', writer.snapshot())
        self.assertEqual([200], handler.statuses)
        self.assertIsNone(app._current_session)
        self.assertEqual(UPSTREAM, first['model'])  # original fixture/payload not mutated

    def test_tool_tail_model_projection_happens_after_binding_and_commit(self):
        first = event({'reasoning_content': 'before tool'})
        first_tool = event(tool_delta('{"model":"deepseek-flash",'))
        last_tool = event({'tool_calls': [{'index': 0, 'function': {'arguments': '"text":"synthetic"}'}}]}, 'tool_calls')
        stream = _GatedSSEStream([wire(first), wire(first_tool), wire(last_tool) + b'data: [DONE]\n\n'], pause_after=1)
        order = []
        def on_write(raw):
            if b'call-synthetic-model' in raw:
                order.append('tool_delivered')
        app, prepared, handler, writer = self.make(stream, tools=True, on_write=on_write)
        original_bind, original_finish = app.bind_response_tool_calls, app.finish_turn
        def bind(*args, **kwargs):
            result = original_bind(*args, **kwargs)
            order.append('bound')
            return result
        def finish(*args, **kwargs):
            result = original_finish(*args, **kwargs)
            order.append('committed')
            return result
        app.bind_response_tool_calls, app.finish_turn = bind, finish
        def before_release():
            prefix = writer.snapshot()
            self.assertIn(b'before tool', prefix)
            self.assertNotIn(b'call-synthetic-model', prefix)
            self.assertNotIn('bound', order)
        self.run_paused(handler, prepared, stream, before_release=before_release)
        self.assert_payloads(writer.snapshot(), [first, first_tool, last_tool])
        self.assertLess(order.index('bound'), order.index('committed'))
        self.assertLess(order.index('committed'), order.index('tool_delivered'))
        self.assertEqual({'call-synthetic-model'}, prepared.session.expected_tool_call_ids)
        app.finish_turn(prepared, keep_for_tools=False)

    def test_full_buffer_success_with_and_without_tools_preserves_original_payloads(self):
        for tools in (False, True):
            with self.subTest(tools=tools):
                first = event({'reasoning_content': 'buffered first'}, metadata={'model': UPSTREAM})
                last = event(tool_delta(), 'tool_calls') if tools else event({'content': 'buffered last'}, 'stop')
                stream = _GatedSSEStream([wire(first), wire(last) + b'data: [DONE]\n\n'], pause_after=0)
                app, prepared, handler, writer = self.make(stream, tools=tools)
                visible = 'synthetic-model-visible-capability'
                prepared.session.protected_values.add(visible)
                prepared.payload['messages'].append({'role': 'tool', 'content': visible})
                self.run_paused(handler, prepared, stream, before_release=lambda: self.assertEqual(b'', writer.snapshot()))
                self.assert_payloads(writer.snapshot(), [first, last])
                self.assertEqual([200], handler.statuses)
                if tools:
                    self.assertEqual({'call-synthetic-model'}, prepared.session.expected_tool_call_ids)
                    app.finish_turn(prepared, keep_for_tools=False)

    def test_protected_upstream_model_is_rejected_before_rewriting_normal_and_full_buffer(self):
        secret = 'synthetic-protected-provider-model'
        unsafe = {**event({'content': 'must not release'}, 'stop'), 'model': secret}
        for full_buffer in (False, True):
            with self.subTest(full_buffer=full_buffer):
                app, prepared, handler, writer = self.make(httpx.ByteStream(wire(unsafe) + b'data: [DONE]\n\n'))
                prepared.session.protected_values.add(secret)
                if full_buffer:
                    prepared.payload['messages'].append({'role': 'tool', 'content': secret})
                with self.assertRaises(GatewayError) as raised:
                    handler._proxy_stream(prepared)
                self.assertEqual('upstream_protected_value', raised.exception.code)
                self.assertEqual(b'', writer.snapshot())
                self.assertEqual([], handler.statuses)
                self.assertIsNone(app._current_session)

    def test_material_protected_prefix_remains_quarantined_before_model_projection(self):
        secret = 'synthetic-protected-fragment-value'
        first, last = event({'content': secret[:12]}), event({'content': secret[12:]}, 'stop')
        stream = _GatedSSEStream([wire(first), wire(last) + b'data: [DONE]\n\n'], pause_after=0)
        app, prepared, handler, writer = self.make(stream)
        prepared.session.protected_values.add(secret)
        errors = []
        def run():
            try:
                handler._proxy_stream(prepared)
            except GatewayError as exc:
                errors.append(exc.code)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(stream.paused.wait(2))
            self.assertEqual(b'', writer.snapshot())
        finally:
            stream.release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(['upstream_protected_value'], errors)
        self.assertEqual(b'', writer.snapshot())
        self.assertIsNone(app._current_session)

    def test_provider_sse_error_payload_and_model_are_not_rewritten(self):
        error = {'model': UPSTREAM, 'error': {'code': 'synthetic_error', 'message': 'synthetic provider error'}}
        app, prepared, handler, writer = self.make(httpx.ByteStream(wire(error) + b'data: [DONE]\n\n'))
        handler._proxy_stream(prepared)
        events = _canonical_client_sse_events(writer.snapshot())
        self.assertEqual(error, events[0].payload)
        self.assertTrue(events[-1].done)
        self.assertIsNone(app._current_session)

    def test_provider_http_error_json_is_unchanged(self):
        error = {'model': UPSTREAM, 'error': {'code': 'synthetic_limit', 'message': 'synthetic HTTP error'}}
        app, prepared, handler, writer = self.make(httpx.ByteStream(json.dumps(error).encode()),
                                                   response_status=429, content_type='application/json')
        handler._proxy_stream(prepared)
        self.assertEqual([429], handler.statuses)
        self.assertEqual(error, json.loads(writer.snapshot()))
        self.assertIsNone(app._current_session)

    def test_late_gateway_error_event_keeps_existing_shape_and_done(self):
        first = event({'content': 'safe visible prefix'})
        app, prepared, handler, writer = self.make(httpx.ByteStream(wire(first) + b'data: malformed\n\n'))
        handler._proxy_stream(prepared)
        events = _canonical_client_sse_events(writer.snapshot())
        self.assertEqual({**first, 'model': PUBLIC}, events[0].payload)
        self.assertEqual({'error'}, set(events[1].payload))
        self.assertEqual('upstream_invalid_stream', events[1].payload['error']['code'])
        self.assertTrue(events[-1].done)
        self.assertIsNone(app._current_session)


if __name__ == '__main__':
    unittest.main()
