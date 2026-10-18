"""A silent completion is a result, not a reason to recover/replay a request."""
from __future__ import annotations

import io
import json
import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import GatewayApplication, GatewayError, _GatewayHandler, _RequestPerformance
from rikkahub_gateway.tests.test_gateway import FakeControl, config

DONE = b'data: [DONE]\n\n'


def event(delta=None, finish=None, **extras):
    choice = {"index": 0, "delta": delta or {}}
    if finish is not None:
        choice["finish_reason"] = finish
    return b'data: ' + json.dumps({"choices": [choice], **extras}).encode() + b'\n\n'


def usage(prompt=100, completion=3):
    return b'data: ' + json.dumps({"choices": [], "usage": {
        "prompt_tokens": prompt, "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }}).encode() + b'\n\n'


EMPTY = event({"reasoning_content": "first thinking"}) + event({}, "stop") + usage() + DONE
ANSWER = event({"content": "real answer"}, "stop") + usage(101, 5) + DONE


class TailActionStream(httpx.SyncByteStream):
    def __init__(self, raw, action):
        self.raw, self.action = raw, action
    def __iter__(self):
        yield self.raw
        self.action()


class SilentCompletionTests(unittest.TestCase):
    def invoke(self, responses, *, continuation=False, custom_config=None, protected=False,
               tail_action=None, writer=None):
        calls, holder = [], {}
        def upstream(request):
            calls.append(request)
            item = responses[len(calls) - 1]
            if isinstance(item, Exception):
                raise item
            if isinstance(item, httpx.SyncByteStream):
                return httpx.Response(200, stream=item)
            if isinstance(item, tuple):
                return httpx.Response(item[0], content=item[1])
            if tail_action:
                return httpx.Response(200, stream=TailActionStream(item, lambda: tail_action(holder['app'])))
            return httpx.Response(200, content=item)
        client = httpx.Client(transport=httpx.MockTransport(upstream))
        self.addCleanup(client.close)
        control = FakeControl()
        app = GatewayApplication(custom_config or config(), control=control, upstream=client)
        holder['app'] = app
        payload = {"model": config().public_model, "stream": True,
            "messages": [{"role": "user", "content": "capability-1" if protected else "synthetic input"}],
            "tools": [{"type": "function", "function": {"name": "host_lookup", "parameters": {"type": "object", "properties": {}}}}]}
        prepared = app.prepare_turn(payload, {})
        if continuation:
            app.finish_turn(prepared, keep_for_tools=True, tool_call_ids=['prior-call'])
            payload['messages'].extend([
                {"role": "assistant", "tool_calls": [{"id": "prior-call", "type": "function", "function": {"name": "host_lookup", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "prior-call", "content": "already executed receipt"},
            ])
            prepared = app.prepare_turn(payload, {})
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=app)
        handler.wfile = writer or io.BytesIO()
        headers = []
        handler.send_response = headers.append
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.close_connection = False
        handler._request_performance = _RequestPerformance()
        code = None
        with patch('rikkahub_gateway.server._PERFORMANCE_LOGGER.info') as logger:
            try:
                handler._proxy_stream(prepared)
            except GatewayError as error:
                code = error.code
        raw = handler.wfile.getvalue()
        data = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b'data: {')]
        errors = [row['error']['code'] for row in data if 'error' in row]
        return SimpleNamespace(calls=calls, raw=raw, data=data, code=code or (errors[-1] if errors else None),
            app=app, headers=headers, control=control, handler=handler,
            logs=[json.loads(call.args[0]) for call in logger.call_args_list])

    def test_silence_is_preserved_without_second_request_or_recovery_log(self):
        for protected in (False, True):
            with self.subTest(protected=protected):
                result = self.invoke([EMPTY, ANSWER], continuation=True, protected=protected)
                self.assertIsNone(result.code)
                self.assertEqual(1, len(result.calls))
                self.assertEqual([200], result.headers)
                self.assertEqual(1, result.raw.count(DONE))
                self.assertIn(b'first thinking', result.raw)
                self.assertNotIn(b'real answer', result.raw)
                self.assertNotIn(b'st_gateway_retry', result.raw)
                self.assertFalse(any(row.get('event') == 'gateway_empty_completion_recovery' for row in result.logs))
                self.assertIsNone(result.app._current_session)
                self.assertEqual(1, sum(path == '/v1/host/wakes' for path, _ in result.control.calls))
                self.assertEqual(100, result.handler._request_performance.prompt_tokens)
                self.assertEqual(3, result.handler._request_performance.completion_tokens)

    def test_silence_does_not_consume_another_response_even_if_that_response_would_fail(self):
        for second in (EMPTY, httpx.ReadTimeout('synthetic'), (503, b'provider error')):
            result = self.invoke([EMPTY, second])
            self.assertIsNone(result.code)
            self.assertEqual(1, len(result.calls))

    def test_no_inference_from_length_filter_or_unknown_output(self):
        for finish, done in [('length', DONE), ('content_filter', DONE), ('future', DONE), ('stop', b'')]:
            result = self.invoke([event({'reasoning_content': 'think', 'future_output': 'unknown'}, finish) + done])
            self.assertIsNone(result.code)
            self.assertEqual(1, len(result.calls))

    def test_partial_content_tools_and_actual_network_errors_are_not_replayed(self):
        for delta in ({'content': 'partial'}, {'refusal': 'no'}, {'audio': {'id': 'clip'}},
                      {'tool_calls': [{'index': 0, 'function': {'arguments': '{'}}]}):
            result = self.invoke([event({'reasoning_content': 'think', **delta})])
            self.assertEqual('upstream_incomplete_stream', result.code)
            self.assertEqual(1, len(result.calls))
        result = self.invoke([httpx.ReadTimeout('synthetic')])
        self.assertEqual('upstream_unavailable', result.code)
        self.assertEqual(1, len(result.calls))

    def test_explicit_provider_error_preserved(self):
        result = self.invoke([event({'reasoning_content': 'think'}) + b'data: {"error":{"code":"provider_error"}}\n\n' + DONE])
        self.assertEqual('provider_error', result.code)
        self.assertEqual(1, len(result.calls))

    def test_invalid_trailing_output_and_duplicate_done_still_rejected(self):
        for tail in (event({'content': 'after done'}), DONE):
            result = self.invoke([EMPTY + tail])
            self.assertEqual('upstream_invalid_stream', result.code)
            self.assertEqual(1, len(result.calls))
            self.assertEqual(1, result.raw.count(DONE))

    def test_replaced_session_cannot_publish_late_completion(self):
        def replace_generation(app):
            app._current_session.request_generation += 1
            app._current_session = None
        result = self.invoke([EMPTY], tail_action=replace_generation)
        self.assertNotIn(DONE, result.raw)
        self.assertNotIn(b'"finish_reason":"stop"', result.raw)
        self.assertEqual(1, len(result.calls))

    def test_request_deadline_still_bounds_stalled_transport(self):
        class BlockingStream(httpx.SyncByteStream):
            def __init__(self): self.closed = threading.Event()
            def __iter__(self):
                self.closed.wait(1.5)
                if False: yield b''
            def close(self): self.closed.set()
        stream = BlockingStream()
        started = time.perf_counter()
        result = self.invoke([stream], custom_config=replace(config(), timeout_seconds=0.03))
        self.assertEqual(1, len(result.calls))
        self.assertTrue(stream.closed.is_set())
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual('upstream_unavailable', result.code)

    def test_stream_byte_limit_is_not_a_silence_check(self):
        result = self.invoke([event({'reasoning_content': 't' * 2000}, 'stop') + DONE],
            custom_config=replace(config(), max_stream_bytes=1024))
        self.assertEqual('upstream_stream_limit_reached', result.code)
        self.assertEqual(1, len(result.calls))

    def test_client_disconnect_releases_turn_without_replay(self):
        class Writer(io.BytesIO):
            def write(self, data): raise BrokenPipeError()
        result = self.invoke([EMPTY], writer=Writer())
        self.assertEqual(1, len(result.calls))
        self.assertIsNone(result.app._current_session)

    def test_successful_tool_receipt_survives_silence_and_next_human_turn(self):
        result = self.invoke([EMPTY], continuation=True)
        wire = json.loads(result.calls[0].content)
        history = wire['messages'][1:]
        history.extend([{'role': 'assistant', 'reasoning_content': 'thinking only', 'content': ''},
                        {'role': 'user', 'content': 'new independent event'}])
        prepared = result.app.prepare_turn({'model': config().public_model, 'stream': True,
            'messages': history, 'tools': wire['tools']}, {})
        self.assertFalse(prepared.continuation)
        self.assertEqual(2, result.control.seq)
        receipts = [message for message in prepared.payload['messages'] if message.get('role') == 'tool']
        self.assertEqual(1, len(receipts))
        self.assertEqual('already executed receipt', receipts[0]['content'])

    def test_silence_does_not_fabricate_usage_or_tool_calls(self):
        for raw in (event({'reasoning_content': 'think'}, 'stop') + DONE,
                    event({'content': ' '}, 'stop') + DONE):
            result = self.invoke([raw])
            self.assertIsNone(result.code)
            self.assertFalse(any('usage' in item or 'st_gateway_retry' in item for item in result.data))
            self.assertIsNone(result.app._current_session)

    def test_complete_tool_batch_still_binds_once(self):
        raw = event({'tool_calls': [{'index': 0, 'id': 'new-call', 'type': 'function',
            'function': {'name': 'host_lookup', 'arguments': '{}'}}]}, 'tool_calls') + DONE
        result = self.invoke([raw], continuation=True)
        self.assertIsNone(result.code)
        self.assertEqual(1, len(result.calls))
        self.assertEqual({'new-call'}, result.app._current_session.expected_tool_call_ids)
        self.assertEqual(1, result.raw.count(b'new-call'))

    def test_protected_value_across_channels_is_still_blocked(self):
        for tail in (event({'content': 'ility-1'}, 'stop'),
                     event({'tool_calls': [{'index': 0, 'id': 'new-call', 'type': 'function',
                         'function': {'name': 'host_lookup', 'arguments': 'ility-1'}}]}, 'tool_calls')):
            result = self.invoke([event({'reasoning_content': 'capab'}) + tail + DONE])
            self.assertEqual('upstream_protected_value', result.code)
            self.assertNotIn(b'ility-1', result.raw)
            self.assertEqual(1, len(result.calls))

    def test_progress_does_not_wait_for_terminal_or_usage(self):
        writer, checks = io.BytesIO(), []
        class ObservedStream(httpx.SyncByteStream):
            def __iter__(self):
                yield event({'reasoning_content': 'synthetic progress'}, usage=None)
                checks.append(writer.getvalue())
                yield event({}, 'stop') + usage() + DONE
        result = self.invoke([ObservedStream()], writer=writer)
        self.assertIsNone(result.code)
        self.assertEqual(1, len(checks))
        self.assertIn(b'synthetic progress', checks[0])
        self.assertEqual(1, len(result.calls))


if __name__ == '__main__': unittest.main()
