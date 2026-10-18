"""Exact host stop protocol, synthetic state only; no socket/database/model I/O."""
from copy import deepcopy
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import http.client
from http.server import ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rikkahub_gateway.server import GatewayApplication, GatewayError, _GatewayHandler
from rikkahub_gateway.tool_execution import NativeToolCall
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tests.test_gateway_execution_recovery import ExecutionControl, st_tool
from rikkahub_gateway.tests.test_tool_result_wait_timeout import FakeTimer


class TurnControlTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket', 'socket.create_connection', 'sqlite3.connect', 'subprocess.Popen'):
            self.enterContext(patch(target, side_effect=AssertionError('external_access_forbidden')))
        self.enterContext(patch('rikkahub_gateway.server.threading.Timer', FakeTimer))
        self.enterContext(patch('rikkahub_gateway.server._FAILURE_LOGGER.warning'))
        self.now = 1000.0
        self.enterContext(patch('rikkahub_gateway.server.time.monotonic', side_effect=lambda: self.now))
        self.control = ExecutionControl()
        self.app = GatewayApplication(replace(fixtures.config(), require_execution_binding=True,
            execution_epoch='synthetic-stop-epoch'), control=self.control, upstream=object())
        self.headers = {'X-ST-Thread-ID': 'orbis:synthetic-call', 'X-ST-Request-ID': 'a' * 32,
                        'Authorization': 'Bearer ' + self.app.config.gateway_token}
        self.user = {'role': 'user', 'content': 'synthetic turn'}
        self.tools = fixtures.GatewayTests.bound_tools() + [st_tool()]
        self.payload = {'model': self.app.default_route.public_model,
                        'messages': [self.user], 'tools': self.tools}
        self.prepared = self.app.prepare_turn(self.payload, self.headers)
        self.addCleanup(lambda: self.app._cancel_recovery_timer(self.prepared.session))

    def query(self):
        return {'model': self.app.default_route.public_model, 'request_id': 'a' * 32}

    def status(self):
        return self.app.control_turn(self.query(), self.headers)

    def stop_request(self):
        return {**self.query(), 'binding': self.status()['binding']}

    def arm(self, managed=True, delivered=True):
        calls = ([NativeToolCall('synthetic-open', 'stbrain_open', '{}')] if managed else
                 [NativeToolCall('synthetic-shell', 'workspace_shell', '{"command":"synthetic never run"}')])
        decorated = self.app.decorate_execution_calls(self.prepared, calls)
        bindings = self.app.bind_response_tool_calls(self.prepared, decorated)
        self.app.finish_turn(self.prepared, keep_for_tools=True,
            tool_call_ids=[call.tool_call_id for call in decorated], tool_call_bindings=bindings)
        if delivered:
            self.app.mark_response_delivered(self.prepared)
        return decorated

    def assert_code(self, code, operation):
        with self.assertRaises(GatewayError) as caught:
            operation()
        self.assertEqual(code, caught.exception.code)

    def test_status_does_not_touch_control_or_model_and_no_internal_values_escape(self):
        self.arm()
        before = len(self.control.calls)
        value = self.status()
        self.assertEqual('waiting_tool_results', value['state'])
        self.assertTrue(value['can_stop'])
        self.assertEqual('unknown', value['external_tool_status'])
        wire = json.dumps(value)
        for protected in [self.prepared.session.wake_id, self.prepared.session.wake_capability,
                          self.prepared.session.execution_batch_id, self.app.config.host_token]:
            self.assertNotIn(protected, wire)
        self.assertEqual(before, len(self.control.calls))

    def test_unauthorized_and_other_thread_nonce_model_never_touch_current_turn(self):
        self.arm()
        before = len(self.control.calls)
        self.assert_code('unauthorized', lambda: self.app.control_turn(self.query(),
                         {**self.headers, 'Authorization': 'Bearer wrong'}))
        for payload, headers in ((self.query(), {**self.headers, 'X-ST-Thread-ID': 'orbis:other'}),
                                 ({**self.query(), 'request_id': 'b' * 32}, self.headers)):
            response = self.app.control_turn(payload, headers)
            self.assertEqual('not_current', response['state'])
            self.assertNotIn('binding', response)
        self.assert_code('invalid_turn_control_request', lambda: self.app.control_turn(
            {**self.query(), 'model': 'unknown-model'}, self.headers))
        self.assertEqual(before, len(self.control.calls))
        self.assertIs(self.prepared.session, self.app._current_session)

    def test_generating_and_not_delivered_are_never_released(self):
        for arm in (False, True):
            if arm:
                self.arm(delivered=False)
            response = self.status()
            self.assertEqual('generating', response['state'])
            self.assertFalse(response['can_stop'])
            before = len(self.control.calls)
            self.assert_code('turn_control_not_delivered', lambda: self.app.control_turn(
                self.stop_request(), self.headers, stop=True))
            self.assertEqual(before, len(self.control.calls))

    def test_exact_stop_confirms_drain_close_and_is_idempotent(self):
        self.arm()
        request = self.stop_request()
        response = self.app.control_turn(request, self.headers, stop=True)
        self.assertEqual({'protocol': 'st-turn-control/1', 'state': 'retired',
                          'can_stop': False, 'external_tool_status': 'unknown'}, response)
        self.assertIsNone(self.app._current_session)
        self.assertTrue(self.control.revoked)
        self.assertTrue(self.control.closed.is_set())
        before = len(self.control.calls)
        self.assertEqual(response, self.app.control_turn(request, self.headers, stop=True))
        self.assertEqual(before, len(self.control.calls))

    def test_old_stop_receipt_does_not_close_new_turn(self):
        self.arm()
        request = self.stop_request()
        old_timer = self.prepared.session.tool_result_wait_timer
        self.app.control_turn(request, self.headers, stop=True)
        new = self.app.prepare_turn(self.payload, {**self.headers, 'X-ST-Request-ID': 'b' * 32})
        before = len(self.control.calls)
        self.assertEqual('retired', self.app.control_turn(request, self.headers, stop=True)['state'])
        self.assertEqual('not_current', self.status()['state'])
        old_timer.fire()
        self.assertIs(new.session, self.app._current_session)
        self.assertEqual(before, len(self.control.calls))
        self.assert_code('turn_control_binding_mismatch', lambda: self.app.control_turn(
            {**request, 'request_id': 'b' * 32}, self.headers, stop=True))

    def test_expired_receipt_never_guesses_a_completed_stop(self):
        self.arm()
        request = self.stop_request()
        self.app.control_turn(request, self.headers, stop=True)
        self.now += 601
        self.assert_code('turn_control_binding_mismatch', lambda: self.app.control_turn(
            request, self.headers, stop=True))

    def test_same_session_continuation_invalidates_old_nonce_and_binding(self):
        calls = self.arm()
        request = self.stop_request()
        continuation = {**self.payload, 'messages': [self.user,
            {'role': 'assistant', 'tool_calls': [fixtures.GatewayTests.wire_call(call) for call in calls]},
            *[{'role': 'tool', 'tool_call_id': call.tool_call_id, 'content': 'synthetic'} for call in calls]]}
        newer = self.app.prepare_turn(continuation, {**self.headers, 'X-ST-Request-ID': 'b' * 32})
        self.assertIs(newer.session, self.prepared.session)
        before = len(self.control.calls)
        self.assertEqual('not_current', self.status()['state'])
        self.assert_code('turn_control_binding_mismatch', lambda: self.app.control_turn(
            request, self.headers, stop=True))
        self.assert_code('turn_control_binding_mismatch', lambda: self.app.control_turn(
            {**request, 'request_id': 'b' * 32}, self.headers, stop=True))
        self.assertEqual(before, len(self.control.calls))
        self.assertEqual(2, newer.session.request_generation)

    def test_removed_managed_alias_preserves_captured_stop_and_exact_receipt(self):
        self.app.finish_turn(self.prepared, keep_for_tools=False)
        route = replace(self.app.default_route, public_model='synthetic-managed', managed=True)
        selected = [route]
        self.app.route_store = SimpleNamespace(snapshot=lambda: selected, protected=lambda: frozenset())
        self.app.refresh_managed_routes()
        self.prepared = self.app.prepare_turn({**self.payload, 'model': route.public_model}, self.headers)
        self.arm()
        query = {**self.query(), 'model': route.public_model}
        status = self.app.control_turn(query, self.headers)
        request = {**query, 'binding': status['binding']}
        selected.clear()
        self.app.refresh_managed_routes()
        self.assertNotIn(route.public_model, self.app.routes)
        self.assertEqual(status, self.app.control_turn(query, self.headers))
        self.assertEqual('retired', self.app.control_turn(request, self.headers, stop=True)['state'])
        before = len(self.control.calls)
        self.assertEqual('retired', self.app.control_turn(request, self.headers, stop=True)['state'])
        self.assertEqual(before, len(self.control.calls))

    def test_every_binding_dimension_is_checked_and_bool_is_not_an_integer(self):
        self.arm()
        request = self.stop_request()
        before = len(self.control.calls)
        for field, value in [('session_id', 'f' * 32), ('generation', 2), ('revision', 2),
                             ('batch_id', 'f' * 64)]:
            wrong = {**request, 'binding': {**request['binding'], field: value}}
            self.assert_code('turn_control_binding_mismatch', lambda: self.app.control_turn(
                wrong, self.headers, stop=True))
        self.assert_code('invalid_turn_control_request', lambda: self.app.control_turn(
            {**request, 'binding': {**request['binding'], 'generation': True}}, self.headers, stop=True))
        self.assertEqual(before, len(self.control.calls))

    def test_running_call_retains_turn_and_only_cleanup_is_retried(self):
        calls = self.arm()
        request = self.stop_request()
        self.control.running = 1
        response = self.app.control_turn(request, self.headers, stop=True)
        self.assertEqual('cleanup_pending', response['state'])
        self.assertFalse(self.control.closed.is_set())
        self.assertIs(self.prepared.session, self.app._current_session)
        timer = self.prepared.session.recovery_timer
        self.assertEqual(5.0, timer.interval)
        self.assertTrue(self.status()['can_stop'])
        continuation = {**self.payload, 'messages': [self.user,
            {'role': 'assistant', 'tool_calls': [fixtures.GatewayTests.wire_call(call) for call in calls]},
            *[{'role': 'tool', 'tool_call_id': call.tool_call_id, 'content': 'synthetic'} for call in calls]]}
        self.assert_code('turn_control_stop_pending', lambda: self.app.prepare_turn(
            continuation, {**self.headers, 'X-ST-Request-ID': 'b' * 32}))
        self.control.running = 0
        timer.fire()
        self.assertIsNone(self.app._current_session)
        self.assertEqual('retired', self.app.control_turn(request, self.headers, stop=True)['state'])

    def test_unconfirmed_close_never_reports_retired(self):
        self.arm()
        request = self.stop_request()
        original = self.control.post
        def fail_close(path, payload):
            if path == '/v1/host/context/close':
                raise GatewayError(502, 'st_control_unavailable')
            return original(path, payload)
        self.control.post = fail_close
        response = self.app.control_turn(request, self.headers, stop=True)
        self.assertEqual('cleanup_pending', response['state'])
        self.assertIs(self.prepared.session, self.app._current_session)
        self.control.post = original
        self.assertEqual('retired', self.app.control_turn(request, self.headers, stop=True)['state'])

    def test_external_tool_only_retires_transport_and_rejects_late_result(self):
        calls = self.arm(managed=False)
        request = self.stop_request()
        response = self.app.control_turn(request, self.headers, stop=True)
        self.assertEqual('retired', response['state'])
        self.assertEqual('unknown', response['external_tool_status'])
        self.assertIn(calls[0].tool_call_id, self.app._retired_tool_call_ids)
        new = self.app.prepare_turn(self.payload, {**self.headers, 'X-ST-Request-ID': 'b' * 32})
        with self.assertRaises(GatewayError):
            self.app.prepare_turn({**self.payload, 'messages': [self.user,
                {'role': 'assistant', 'tool_calls': [fixtures.GatewayTests.wire_call(c) for c in calls]},
                {'role': 'tool', 'tool_call_id': calls[0].tool_call_id, 'content': 'late synthetic'}]}, self.headers)
        self.assertIs(new.session, self.app._current_session)

    def test_external_tool_wait_without_managed_batch_uses_confirmed_close(self):
        calls = [NativeToolCall('synthetic-external', 'workspace_shell', '{"command":"synthetic never run"}')]
        # No ST execution batch was issued. The exact delivered transport still
        # requires an explicit successful Control close, never local UI evidence.
        self.app.finish_turn(self.prepared, keep_for_tools=True, tool_call_ids=[calls[0].tool_call_id])
        self.app.mark_response_delivered(self.prepared)
        self.assertIsNone(self.status()['binding']['batch_id'])
        response = self.app.control_turn(self.stop_request(), self.headers, stop=True)
        self.assertEqual('retired', response['state'])
        self.assertEqual('unknown', response['external_tool_status'])
        self.assertTrue(self.control.closed.is_set())

    def test_cleanup_response_loss_only_retries_the_confirmed_close(self):
        self.arm()
        request = self.stop_request()
        original = self.control.post
        failed_once = [False]
        def lost_response(path, payload):
            result = original(path, payload)
            if path == '/v1/host/context/close' and not failed_once[0]:
                failed_once[0] = True
                raise GatewayError(502, 'st_control_unavailable')
            return result
        self.control.post = lost_response
        self.assertEqual('cleanup_pending', self.app.control_turn(request, self.headers, stop=True)['state'])
        before = len(self.control.calls)
        self.assertEqual('retired', self.app.control_turn(request, self.headers, stop=True)['state'])
        self.assertEqual(['/v1/host/context/close'], [path for path, _ in self.control.calls[before:]])

    def expire_pending_stop(self, *, managed):
        if managed:
            self.arm()
        else:
            self.app.finish_turn(self.prepared, keep_for_tools=True, tool_call_ids=['synthetic-external'])
            self.app.mark_response_delivered(self.prepared)
        old = self.prepared.session
        request = self.stop_request()
        original = self.control.post
        def unconfirmed(path, payload):
            if path == '/v1/host/context/close':
                return {'decision': 'not_confirmed'}
            return original(path, payload)
        self.control.post = unconfirmed
        self.assertEqual('cleanup_pending', self.app.control_turn(request, self.headers, stop=True)['state'])
        self.assertIsNone(old.tool_result_wait_deadline)
        old.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertFalse(self.control.closed.is_set())
        new = self.app.prepare_turn(self.payload, {**self.headers, 'X-ST-Request-ID': 'b' * 32})
        self.assertIsNot(old, new.session)
        before = len(self.control.calls)
        self.assertEqual('retired', self.app.control_turn(request, self.headers, stop=True)['state'])
        self.assertEqual(before, len(self.control.calls))
        self.assertIs(new.session, self.app._current_session)

    def test_pending_external_stop_and_expired_wake_do_not_hold_fresh_human(self):
        self.expire_pending_stop(managed=False)

    def test_pending_managed_stop_and_expired_wake_preserve_exact_receipt(self):
        self.expire_pending_stop(managed=True)

    def test_invalid_nonce_is_rejected_before_new_wake(self):
        self.app.finish_turn(self.prepared, keep_for_tools=False)
        before = len(self.control.calls)
        for nonce in ('', 'X' * 32, 'a' * 31, 'a' * 33):
            self.assert_code('invalid_turn_control_request', lambda: self.app.prepare_turn(
                self.payload, {**self.headers, 'X-ST-Request-ID': nonce}))
        self.assertEqual(before, len(self.control.calls))

    def test_legacy_request_remains_supported_but_not_discoverable(self):
        self.app.finish_turn(self.prepared, keep_for_tools=False)
        headers = {k: v for k, v in self.headers.items() if k != 'X-ST-Request-ID'}
        self.app.prepare_turn(self.payload, headers)
        self.assertEqual('not_current', self.status()['state'])

    def wire(self, path='/v1/turns/status', body=None, changes=(), omit=()):
        raw = json.dumps(self.query()).encode() if body is None else body
        headers = Message()
        for key, value in [('Authorization', self.headers['Authorization']),
                           ('X-ST-Thread-ID', self.headers['X-ST-Thread-ID']),
                           ('Content-Type', 'application/json'), ('Content-Length', str(len(raw)))]:
            if key not in omit:
                headers[key] = value
        for key, value in changes:
            headers[key] = value
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=self.app)
        handler.headers, handler.path, handler.rfile = headers, path, io.BytesIO(raw)
        handler.close_connection = False
        answers = []
        handler._json = lambda status, value: answers.append((status, value))
        handler.do_POST()
        return answers[-1], handler

    def test_http_roundtrip_and_stop_body_contract(self):
        self.arm()
        (status, response), handler = self.wire()
        self.assertEqual(200, status)
        self.assertTrue(handler.close_connection)
        request = {**self.query(), 'binding': response['binding']}
        (status, response), _ = self.wire('/v1/turns/stop', json.dumps(request).encode())
        self.assertEqual(200, status)
        self.assertEqual('retired', response['state'])

    def test_http_auth_and_framing_rejected_without_body_read_or_control_io(self):
        before = len(self.control.calls)
        cases = [({'omit': ('Authorization',)}, 401),
                 ({'changes': [('Authorization', 'Bearer duplicate')]}, 401),
                 ({'changes': [('X-ST-Thread-ID', 'other')]}, 400),
                 ({'changes': [('Content-Length', '1')]}, 400),
                 ({'changes': [('Transfer-Encoding', 'chunked')]}, 400),
                 ({'changes': [('X-Session-ID', 'other')]}, 400),
                 ({'path': '/v1/turns/status?unsafe=1'}, 400)]
        for args, expected in cases:
            (status, _), handler = self.wire(**args)
            self.assertEqual(expected, status)
            self.assertEqual(0, handler.rfile.tell())
            self.assertTrue(handler.close_connection)
        self.assertEqual(before, len(self.control.calls))

    def test_http_oversize_duplicate_json_unknown_field_and_truncated_body(self):
        before = len(self.control.calls)
        cases = [(b' ' * 2049, (), (), 413),
                 (b'{"model":"stiller-rikka","model":"stiller-rikka","request_id":"' + b'a' * 32 + b'"}', (), (), 400),
                 (json.dumps({**self.query(), 'unexpected': True}).encode(), (), (), 400),
                 (b'{}', [('Content-Length', '5')], ('Content-Length',), 400)]
        for raw, changes, omit, expected in cases:
            (status, _), _ = self.wire(body=raw, changes=changes, omit=omit)
            self.assertEqual(expected, status)
        self.assertEqual(before, len(self.control.calls))


class TurnControlLoopbackTests(unittest.TestCase):
    def test_real_http_roundtrip_uses_only_synthetic_control_no_upstream(self):
        control = ExecutionControl()
        app = GatewayApplication(replace(fixtures.config(), require_execution_binding=True,
            execution_epoch='synthetic-loopback-stop'), control=control, upstream=object())
        headers = {'Authorization': 'Bearer ' + app.config.gateway_token,
                   'X-ST-Thread-ID': 'orbis:loopback', 'X-ST-Request-ID': 'c' * 32}
        prepared = app.prepare_turn({'model': app.default_route.public_model,
            'messages': [{'role': 'user', 'content': 'synthetic'}],
            'tools': fixtures.GatewayTests.bound_tools()}, headers)
        app.finish_turn(prepared, keep_for_tools=True, tool_call_ids=['synthetic-external-call'])
        app.mark_response_delivered(prepared)
        server = ThreadingHTTPServer(('127.0.0.1', 0), _GatewayHandler)
        server.application = app
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            def post(path, payload):
                with closing(http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)) as client:
                    client.request('POST', path, body=json.dumps(payload),
                                   headers={**headers, 'Content-Type': 'application/json'})
                    response = client.getresponse()
                    body = json.loads(response.read(4096))
                    self.assertEqual(200, response.status)
                    self.assertEqual('no-store', response.getheader('Cache-Control'))
                    return body
            query = {'model': app.default_route.public_model, 'request_id': 'c' * 32}
            status = post('/v1/turns/status', query)
            self.assertEqual('waiting_tool_results', status['state'])
            result = post('/v1/turns/stop', {**query, 'binding': status['binding']})
            self.assertEqual('retired', result['state'])
            self.assertTrue(control.closed.is_set())
            self.assertIsNone(app._current_session)
        finally:
            app._cancel_recovery_timer(prepared.session)
            server.shutdown()
            server.server_close()
            worker.join(5)


if __name__ == '__main__':
    unittest.main()
