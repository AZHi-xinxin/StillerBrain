"""Preview3 image/auxiliary/atlas regressions with exact host turn control.

All HTTP handlers run in memory with synthetic Control and MockTransport.
Sockets, subprocesses and database access are forbidden in every test.
"""
from copy import deepcopy
from dataclasses import replace
from email.message import Message
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from rikkahub_gateway.atlas import CAPABILITIES
from rikkahub_gateway.server import GatewayApplication, GatewayError, _GatewayHandler
from rikkahub_gateway.tool_execution import NativeToolCall, canonical_hash
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tests.test_gateway_execution_recovery import ExecutionControl
from rikkahub_gateway.tests.test_gateway_image_transport_projection import stage, store
from rikkahub_gateway.tests.test_tool_result_wait_timeout import FakeTimer
from runtime.atlas_device_grants import capabilities


class Preview3TurnControlTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket', 'socket.create_connection', 'socket.getaddrinfo',
                       'sqlite3.connect', 'subprocess.Popen'):
            self.enterContext(patch(target, side_effect=AssertionError('external_access_forbidden')))
        self.enterContext(patch('rikkahub_gateway.server.threading.Timer', FakeTimer))
        self.enterContext(patch('rikkahub_gateway.server._FAILURE_LOGGER.warning'))
        self.control = ExecutionControl()
        self.sent = []

        def respond(request):
            payload = json.loads(request.content)
            self.sent.append(payload)
            if payload.get('stream'):
                event = {'choices': [{'index': 0, 'delta': {'content': 'synthetic title'},
                                      'finish_reason': 'stop'}]}
                return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                    content=('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode())
            return httpx.Response(200, json={'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': 'synthetic title'}, 'finish_reason': 'stop'}]})

        upstream = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
        self.addCleanup(upstream.close)
        self.app = GatewayApplication(replace(fixtures.config(), require_execution_binding=True,
            execution_epoch='synthetic-preview3-turn-control'), control=self.control, upstream=upstream)
        self.addCleanup(self.app.close_short_term)
        self.headers = {'Authorization': 'Bearer ' + self.app.config.gateway_token,
                        'X-ST-Thread-ID': 'orbis:synthetic-preview3', 'X-ST-Request-ID': 'a' * 32}
        self.payload = {'model': self.app.default_route.public_model,
                        'messages': [{'role': 'user', 'content': 'synthetic selected image'}]}

    def handler(self, payload=None, *, path='/v1/chat/completions', extra=(), omit=()):
        raw = b'' if payload is None else json.dumps(payload).encode()
        headers = Message()
        for key, value in {**self.headers, 'Content-Type': 'application/json',
                           'Content-Length': str(len(raw))}.items():
            if key not in omit:
                headers[key] = value
        for key, value in extra:
            headers[key] = value
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=self.app)
        handler.headers, handler.path = headers, path
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.close_connection = False
        handler.statuses, handler.response_headers = [], {}
        handler.send_response = handler.statuses.append
        handler.send_header = lambda key, value: handler.response_headers.update({key: value})
        handler.end_headers = lambda: None
        return handler

    def assert_no_protocol(self, handler):
        self.assertNotIn('X-ST-Turn-Control', handler.response_headers)
        self.assertNotIn('X-ST-Request-ID', handler.response_headers)

    def waiting(self):
        prepared = self.app.prepare_turn(self.payload, self.headers)
        self.addCleanup(self.app._cancel_recovery_timer, prepared.session)
        self.app.finish_turn(prepared, keep_for_tools=True, tool_call_ids=['synthetic-external'])
        self.app.mark_response_delivered(prepared)
        return prepared

    def test_nonce_image_stage_stays_host_only_store_binding_survives_exact_finish(self):
        image_tool = store()
        stage_name = 'mcp__StillerBrain__stage_memory_image_upload'
        payload = {**self.payload, 'tools': [stage(stage_name), image_tool]}
        original = deepcopy(payload)
        prepared = self.app.prepare_turn(payload, self.headers)
        self.addCleanup(self.app._cancel_recovery_timer, prepared.session)
        self.assertEqual(original, payload)
        self.assertNotIn(stage_name, json.dumps(prepared.session.advertised_tools))
        self.assertNotIn(stage_name, json.dumps(self.control.calls))
        with self.assertRaisesRegex(GatewayError, 'tool_not_advertised'):
            self.app.decorate_execution_calls(prepared, [NativeToolCall('synthetic-stage', stage_name, '{}')])
        args = {'target_ref': 'emotion:synthetic', 'description': 'synthetic PNG',
                'mime_type': 'image/png', 'upload_ref': 'stimgup_' + 'b' * 64}
        call = NativeToolCall('synthetic-store', image_tool['function']['name'], json.dumps(args))
        decorated = self.app.decorate_execution_calls(prepared, [call])
        bindings = self.app.bind_response_tool_calls(prepared, decorated)
        self.assertEqual(canonical_hash(image_tool['function']['parameters']), bindings[call.tool_call_id].schema_hash)
        bound_args = json.loads(decorated[0].arguments_text)
        self.assertRegex(bound_args.pop('execution_ref'), r'^stexec_[A-Za-z0-9_-]{43}$')
        self.assertEqual(args, bound_args)
        wire = json.loads(self.app.encode_upstream_payload(prepared.payload))
        self.assertEqual([image_tool['function']['name']], [tool['function']['name'] for tool in wire['tools']])
        visible = wire['tools'][0]['function']['parameters']
        self.assertNotIn('execution_ref', visible['properties'])
        self.assertNotIn('data_base64', visible['properties'])
        self.assertIn('upload_ref', visible['required'])
        self.app.finish_turn(prepared, keep_for_tools=True, tool_call_ids=[call.tool_call_id],
                             tool_call_bindings=bindings)
        self.app.mark_response_delivered(prepared)
        query = {'model': self.payload['model'], 'request_id': self.headers['X-ST-Request-ID']}
        snapshot = self.app.control_turn(query, self.headers)
        self.assertEqual('waiting_tool_results', snapshot['state'])
        self.assertIsNotNone(snapshot['binding']['batch_id'])
        before = len(self.control.calls)
        receipts = deepcopy(prepared.session.host_receipts)
        receipt = {**query, 'binding': snapshot['binding'], 'terminal_reason': 'failed'}
        handler = self.handler(receipt, path='/v1/turns/finish')
        handler.do_POST()
        self.assertEqual([200], handler.statuses)
        result = json.loads(handler.wfile.getvalue())
        self.assertEqual('retired', result['state'])
        self.assertEqual('unknown', result['external_tool_status'])
        self.assert_no_protocol(handler)
        self.assertIsNone(self.app._current_session)
        self.assertTrue(self.control.closed.is_set())
        self.assertEqual(receipts, prepared.session.host_receipts)
        self.assertEqual([], self.sent)
        self.assertFalse(any(path.endswith('/issue') or path.endswith('/wakes')
                             for path, _ in self.control.calls[before:]))
        after = len(self.control.calls)
        self.assertEqual('retired', self.app.control_turn(receipt, self.headers, finish=True)['state'])
        self.assertEqual(after, len(self.control.calls))

    def assert_chat_identity_rejected(self, extra):
        # A malformed request must not read its body or disturb an existing wait.
        prepared = self.waiting()
        before = deepcopy(self.control.calls)
        timer = prepared.session.tool_result_wait_timer
        for path in ('/chat/completions', '/v1/chat/completions'):
            with self.subTest(path=path):
                handler = self.handler(self.payload, path=path, extra=extra)
                handler.do_POST()
                self.assertEqual([400], handler.statuses)
                self.assertEqual('invalid_turn_control_request', json.loads(handler.wfile.getvalue())['error']['code'])
                self.assertEqual(0, handler.rfile.tell())
                self.assertTrue(handler.close_connection)
                self.assert_no_protocol(handler)
        self.assertEqual(before, self.control.calls)
        self.assertEqual([], self.sent)
        self.assertIs(prepared.session, self.app._current_session)
        self.assertIs(timer, prepared.session.tool_result_wait_timer)
        self.assertFalse(timer.cancelled)

    def test_chat_duplicate_nonce_has_no_control_or_upstream_io(self):
        self.assert_chat_identity_rejected([('x-st-request-id', 'b' * 32)])

    def test_chat_duplicate_thread_has_no_control_or_upstream_io(self):
        self.assert_chat_identity_rejected([('x-st-thread-id', 'orbis:other')])

    def test_chat_duplicate_auth_has_no_control_or_upstream_io(self):
        self.assert_chat_identity_rejected([('authorization', self.headers['Authorization'])])

    def test_chat_legacy_session_alias_has_no_control_or_upstream_io(self):
        self.assert_chat_identity_rejected([('X-Session-ID', 'synthetic-other-session')])

    def test_nonce_auxiliary_json_and_sse_never_advertise_turn_control_or_touch_wait(self):
        prepared = self.waiting()
        before = deepcopy(self.control.calls)
        timer = prepared.session.tool_result_wait_timer
        for stream in (False, True):
            with self.subTest(stream=stream):
                payload = {**self.payload, 'model': self.app.auxiliary_model, 'stream': stream}
                handler = self.handler(payload)
                handler.do_POST()
                self.assertEqual([200], handler.statuses)
                self.assertIn(b'synthetic title', handler.wfile.getvalue())
                self.assert_no_protocol(handler)
        self.assertEqual(2, len(self.sent))
        self.assertEqual(before, self.control.calls)
        self.assertIs(prepared.session, self.app._current_session)
        self.assertIs(timer, prepared.session.tool_result_wait_timer)
        self.assertFalse(timer.cancelled)

    def test_nonce_atlas_discovery_never_advertises_turn_control_or_touches_wait(self):
        prepared = self.waiting()
        before = deepcopy(self.control.calls)
        timer = prepared.session.tool_result_wait_timer
        with patch.object(self.app.atlas, 'request', return_value=capabilities(True)) as atlas:
            handler = self.handler(path=CAPABILITIES)
            handler.do_GET()
            atlas.assert_called_once_with(CAPABILITIES, {})
        self.assertEqual([200], handler.statuses)
        self.assertEqual(capabilities(True), json.loads(handler.wfile.getvalue()))
        self.assert_no_protocol(handler)
        self.assertEqual(before, self.control.calls)
        self.assertEqual([], self.sent)
        self.assertIs(prepared.session, self.app._current_session)
        self.assertIs(timer, prepared.session.tool_result_wait_timer)
        self.assertFalse(timer.cancelled)


if __name__ == '__main__':
    unittest.main()
