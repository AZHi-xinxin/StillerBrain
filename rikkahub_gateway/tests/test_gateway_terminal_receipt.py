"""Synthetic host-lifecycle completion; no production data or real model/tools."""
from contextlib import closing
from dataclasses import replace
import http.client
from http.server import ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from rikkahub_gateway.server import GatewayApplication, GatewayError, _GatewayHandler
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tests.test_gateway_execution_recovery import ExecutionControl
from rikkahub_gateway.tests import test_gateway_turn_control as control_fixtures
from rikkahub_gateway.tests.test_tool_result_wait_timeout import FakeTimer


class TerminalReceiptTests(unittest.TestCase):
    # Reuse synthetic setup/helpers, without rerunning inherited test methods.
    setUp = control_fixtures.TurnControlTests.setUp
    query = control_fixtures.TurnControlTests.query
    status = control_fixtures.TurnControlTests.status
    arm = control_fixtures.TurnControlTests.arm
    assert_code = control_fixtures.TurnControlTests.assert_code
    wire = control_fixtures.TurnControlTests.wire

    def receipt(self, reason='failed'):
        return {**self.query(), 'binding': self.status()['binding'], 'terminal_reason': reason}

    def test_terminal_receipt_does_not_need_reconstructed_chat_or_invent_results(self):
        self.arm(managed=False)
        before_receipts = list(self.prepared.session.host_receipts)
        self.now += 0.01
        receipt = self.receipt()
        result = self.app.control_turn(receipt, self.headers, finish=True)
        self.assertEqual('retired', result['state'])
        self.assertEqual('unknown', result['external_tool_status'])
        self.assertEqual(before_receipts, self.prepared.session.host_receipts)
        self.assertTrue(self.control.closed.is_set())
        self.assertIsNone(self.app._current_session)
        self.assertEqual('failed', self.prepared.session.client_terminal_reason)
        self.assertEqual(300, self.app.config.tool_result_wait_seconds)

    def test_finish_generating_and_undelivered_fail_closed(self):
        for delivered in (False, True):
            if delivered:
                self.arm(delivered=False)
            before = len(self.control.calls)
            self.assert_code('turn_control_not_delivered', lambda: self.app.control_turn(
                self.receipt(), self.headers, finish=True))
            self.assertEqual(before, len(self.control.calls))
            self.assertIs(self.prepared.session, self.app._current_session)

    def test_receipt_requires_exact_nonce_thread_binding_and_authorization(self):
        self.arm()
        receipt = self.receipt()
        before = len(self.control.calls)
        self.assert_code('unauthorized', lambda: self.app.control_turn(receipt,
            {**self.headers, 'Authorization': 'Bearer wrong'}, finish=True))
        for payload, headers in (
            ({**receipt, 'request_id': 'b' * 32}, self.headers),
            (receipt, {**self.headers, 'X-ST-Thread-ID': 'orbis:other'}),
            ({**receipt, 'binding': {**receipt['binding'], 'generation': 2}}, self.headers),
        ):
            self.assert_code('turn_control_binding_mismatch', lambda: self.app.control_turn(
                payload, headers, finish=True))
        self.assertEqual(before, len(self.control.calls))

    def test_invalid_reasons_and_unknown_fields_never_touch_control(self):
        self.arm()
        receipt = self.receipt()
        before = len(self.control.calls)
        for reason in ('success', 'pending_approval', 'still_running', '', True, None, [], {}):
            self.assert_code('invalid_turn_control_request', lambda: self.app.control_turn(
                {**receipt, 'terminal_reason': reason}, self.headers, finish=True))
        for payload in ({**receipt, 'tool_result': 'synthetic'}, self.query()):
            self.assert_code('invalid_turn_control_request', lambda: self.app.control_turn(
                payload, self.headers, finish=True))
        self.assert_code('invalid_turn_control_request', lambda: self.app.control_turn(
            receipt, self.headers, stop=True, finish=True))
        self.assertEqual(before, len(self.control.calls))

    def test_running_managed_tool_must_drain_before_terminal_receipt_retires(self):
        self.arm()
        receipt = self.receipt('cancelled')
        self.control.running = 1
        response = self.app.control_turn(receipt, self.headers, finish=True)
        self.assertEqual('cleanup_pending', response['state'])
        self.assertEqual(receipt['binding'], response['binding'])
        self.assertFalse(self.control.closed.is_set())
        self.assertIs(self.prepared.session, self.app._current_session)
        self.control.running = 0
        self.prepared.session.recovery_timer.fire()
        self.assertIsNone(self.app._current_session)
        self.assertEqual('retired', self.app.control_turn(receipt, self.headers, finish=True)['state'])

    def test_unconfirmed_close_never_marks_failure_receipt_retired(self):
        self.arm(managed=False)
        receipt = self.receipt()
        original = self.control.post
        def uncertain(path, payload):
            if path == '/v1/host/context/close':
                return {'decision': 'unknown'}
            return original(path, payload)
        self.control.post = uncertain
        self.assertEqual('cleanup_pending', self.app.control_turn(receipt, self.headers, finish=True)['state'])
        self.assertIs(self.prepared.session, self.app._current_session)
        self.control.post = original
        self.prepared.session.recovery_timer.fire()
        self.assertIsNone(self.app._current_session)

    def test_old_finish_and_timer_cannot_close_replacement_turn(self):
        self.arm()
        receipt = self.receipt('budget_exhausted')
        old_timer = self.prepared.session.tool_result_wait_timer
        self.app.control_turn(receipt, self.headers, finish=True)
        newer = self.app.prepare_turn(self.payload, {**self.headers, 'X-ST-Request-ID': 'b' * 32})
        before = len(self.control.calls)
        self.assertEqual('retired', self.app.control_turn(receipt, self.headers, finish=True)['state'])
        old_timer.fire()
        self.assertIs(newer.session, self.app._current_session)
        self.assertEqual(before, len(self.control.calls))
        self.assert_code('turn_control_binding_mismatch', lambda: self.app.control_turn(
            {**receipt, 'request_id': 'b' * 32}, self.headers, finish=True))

    def test_http_finish_rejects_ambiguous_headers_and_duplicate_json(self):
        self.arm()
        raw = json.dumps(self.receipt()).encode()
        for changes in ([('Authorization', self.headers['Authorization'])],
                        [('X-ST-Thread-ID', self.headers['X-ST-Thread-ID'])],
                        [('Transfer-Encoding', 'chunked')]):
            (status, _), _ = self.wire(path='/v1/turns/finish', body=raw, changes=changes)
            self.assertIn(status, (400, 401))
        duplicated = raw[:-1] + b',"terminal_reason":"failed"}'
        (status, _), _ = self.wire(path='/v1/turns/finish', body=duplicated)
        self.assertEqual(400, status)
        (status, result), _ = self.wire(path='/v1/turns/finish', body=raw)
        self.assertEqual(200, status)
        self.assertEqual('retired', result['state'])

    def test_simultaneous_manual_stop_and_terminal_finish_close_exact_owner_only_once(self):
        self.arm()
        receipt = self.receipt('cancelled')
        stop = {key: value for key, value in receipt.items() if key != 'terminal_reason'}
        entered_close, release_close, second_started = (threading.Event() for _ in range(3))
        original = self.control.post
        results, failures = [], []

        def blocked_close(path, payload):
            if path == '/v1/host/context/close':
                entered_close.set()
                if not release_close.wait(2):
                    raise AssertionError('synthetic close was not released')
            return original(path, payload)

        def invoke(payload, **action):
            try:
                if action.get('finish'):
                    second_started.set()
                results.append(self.app.control_turn(payload, self.headers, **action))
            except BaseException as error:
                failures.append(error)

        self.control.post = blocked_close
        first = threading.Thread(target=invoke, args=(stop,), kwargs={'stop': True}, daemon=True)
        second = threading.Thread(target=invoke, args=(receipt,), kwargs={'finish': True}, daemon=True)
        before = len(self.control.calls)
        try:
            first.start()
            self.assertTrue(entered_close.wait(2))
            second.start()
            self.assertTrue(second_started.wait(2))
        finally:
            release_close.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], failures)
        self.assertEqual(2, len(results))
        self.assertTrue(all(result['state'] == 'retired' and
                            result['external_tool_status'] == 'unknown' for result in results))
        paths = [path for path, _ in self.control.calls[before:]]
        self.assertEqual(1, paths.count('/v1/host/context/close'))
        self.assertFalse(any(path.endswith('/issue') for path in paths))
        self.assertIsNone(self.app._current_session)

    def test_manual_stop_then_finish_while_running_does_not_replay_late_tool_results(self):
        calls = self.arm()
        receipt = self.receipt('failed')
        stop = {key: value for key, value in receipt.items() if key != 'terminal_reason'}
        self.control.running = 1
        before = len(self.control.calls)
        self.assertEqual('cleanup_pending', self.app.control_turn(stop, self.headers, stop=True)['state'])
        self.assertEqual('cleanup_pending', self.app.control_turn(receipt, self.headers, finish=True)['state'])
        self.assertFalse(self.control.closed.is_set())
        continuation = {**self.payload, 'messages': [self.user,
            {'role': 'assistant', 'tool_calls': [fixtures.GatewayTests.wire_call(call) for call in calls]},
            *[{'role': 'tool', 'tool_call_id': call.tool_call_id, 'content': 'late synthetic result'} for call in calls]]}
        self.assert_code('turn_control_stop_pending', lambda: self.app.prepare_turn(
            continuation, {**self.headers, 'X-ST-Request-ID': 'b' * 32}))
        self.assertIs(self.prepared.session, self.app._current_session)
        self.control.running = 0
        self.prepared.session.recovery_timer.fire()
        self.assertEqual('retired', self.app.control_turn(receipt, self.headers, finish=True)['state'])
        self.assertEqual('retired', self.app.control_turn(stop, self.headers, stop=True)['state'])
        paths = [path for path, _ in self.control.calls[before:]]
        self.assertFalse(any(path.endswith('/issue') or path.endswith('/open') for path in paths))
        self.assertEqual(1, paths.count('/v1/host/context/close'))
        self.assertTrue(all(call.tool_call_id in self.app._retired_tool_call_ids for call in calls))

    def test_fast_host_terminal_receipt_waits_for_atomic_delivery_marker(self):
        calls = self.arm(delivered=False)
        bindings = dict(self.prepared.session.expected_tool_calls)
        handler = object.__new__(_GatewayHandler)
        handler.server = SimpleNamespace(application=self.app)
        attempted, finished = threading.Event(), threading.Event()
        results, failures = [], []
        def host_terminal():
            attempted.set()
            try:
                results.append(self.app.control_turn(self.receipt(), self.headers, finish=True))
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()
        worker = threading.Thread(target=host_terminal, daemon=True)
        def complete_tail_write():
            # Simulate the client receiving a complete tool tail and failing its
            # local loop before this server thread has set the delivered marker.
            worker.start()
            self.assertTrue(attempted.wait(2))
            self.assertFalse(finished.is_set())
            self.assertIsNone(self.prepared.session.delivered_at_monotonic)
        handler._commit_final_delivery(self.prepared, None, complete_tail_write, True,
            [call.tool_call_id for call in calls], bindings)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], failures)
        self.assertEqual('retired', results[0]['state'])
        self.assertIsNone(self.app._current_session)


class TerminalReceiptLoopbackTests(unittest.TestCase):
    def roundtrip(self, stream, *, nonce=True, upstream_error=False):
        """Actual client HTTP -> gateway -> synthetic upstream, then finish HTTP."""
        self.enterContext(patch('rikkahub_gateway.server.threading.Timer', FakeTimer))
        self.enterContext(patch('rikkahub_gateway.server._FAILURE_LOGGER.warning'))
        control = ExecutionControl()
        sent = []
        def reply(request):
            sent.append(json.loads(request.content))
            if upstream_error:
                return httpx.Response(503, json={'error': {'message': 'synthetic unavailable'}})
            first = len(sent) == 1
            call = {'id': 'synthetic-external', 'type': 'function', 'function': {
                'name': 'workspace_shell', 'arguments': '{"command":"synthetic never executed"}'}}
            message = {'tool_calls': [call]} if first else {'content': 'synthetic final response'}
            if stream:
                if first:
                    message = {'tool_calls': [{'index': 0, **call}]}
                body = {'choices': [{'index': 0, 'delta': message,
                                    'finish_reason': 'tool_calls' if first else 'stop'}]}
                return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                    content=('data: ' + json.dumps(body) + '\n\ndata: [DONE]\n\n').encode())
            return httpx.Response(200, json={'choices': [{'index': 0, 'message': message,
                'finish_reason': 'tool_calls' if first else 'stop'}]})
        upstream = httpx.Client(transport=httpx.MockTransport(reply), trust_env=False)
        self.addCleanup(upstream.close)
        app = GatewayApplication(replace(fixtures.config(), require_execution_binding=True,
            execution_epoch='synthetic-terminal-http'), control=control, upstream=upstream)
        server = ThreadingHTTPServer(('127.0.0.1', 0), _GatewayHandler)
        server.application = app
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        headers = {'Authorization': 'Bearer ' + app.config.gateway_token,
            'X-ST-Thread-ID': 'orbis:synthetic-terminal', 'Content-Type': 'application/json'}
        if nonce:
            headers['X-ST-Request-ID'] = 'd' * 32
        def post(path, payload, extra=None):
            with closing(http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)) as client:
                client.request('POST', path, body=json.dumps(payload), headers={**headers, **(extra or {})})
                response = client.getresponse()
                return response.status, dict(response.getheaders()), response.read()
        try:
            original = [{'role': 'system', 'content': 'synthetic original prefix'},
                        {'role': 'user', 'content': 'synthetic first question'}]
            payload = {'model': app.default_route.public_model, 'messages': original,
                       'tools': fixtures.GatewayTests.bound_tools(), 'stream': stream}
            status, response_headers, _ = post('/v1/chat/completions', payload)
            if upstream_error or not nonce:
                self.assertNotIn('X-ST-Turn-Control', response_headers)
                self.assertNotIn('X-ST-Request-ID', response_headers)
                return
            self.assertEqual(200, status)
            self.assertEqual('st-turn-control/1', response_headers['X-ST-Turn-Control'])
            self.assertEqual('d' * 32, response_headers['X-ST-Request-ID'])
            self.assertEqual(1, len(sent))
            waiting = app._current_session
            self.assertIsNotNone(waiting)
            self.assertTrue(waiting.expected_tool_call_ids)
            deadline = waiting.tool_result_wait_deadline
            old_timer = waiting.tool_result_wait_timer
            # Model response has completed, but local tool execution/serialization
            # fails and its owning generation unwinds without a continuation.
            # Another request has a changed prefix and an extra local status tail;
            # the strict legacy history heuristic deliberately cannot release it.
            next_messages = [{'role': 'system', 'content': 'synthetic updated prefix'},
                original[-1], {'role': 'assistant', 'content': 'synthetic local failure'},
                {'role': 'user', 'content': 'synthetic next question'}]
            next_payload = {**payload, 'messages': next_messages}
            blocked_status, blocked_headers, blocked_raw = post('/v1/chat/completions', next_payload,
                {'X-ST-Request-ID': 'e' * 32})
            self.assertEqual(409, blocked_status)
            self.assertEqual('human_turn_in_progress', json.loads(blocked_raw)['error']['code'])
            self.assertNotIn('X-ST-Turn-Control', blocked_headers)
            self.assertEqual(1, len(sent))
            query = {'model': app.default_route.public_model, 'request_id': 'd' * 32}
            _, _, status_raw = post('/v1/turns/status', query)
            snapshot = json.loads(status_raw)
            self.assertEqual('waiting_tool_results', snapshot['state'])
            receipt = {**query, 'binding': snapshot['binding'], 'terminal_reason': 'failed'}
            finished, finish_headers, finish_raw = post('/v1/turns/finish', receipt)
            self.assertEqual(200, finished)
            self.assertEqual('retired', json.loads(finish_raw)['state'])
            self.assertNotIn('X-ST-Turn-Control', finish_headers)
            self.assertEqual([], waiting.host_receipts)
            self.assertTrue(control.closed.is_set())
            self.assertIsNone(app._current_session)
            import time
            self.assertLess(time.monotonic(), deadline)
            status, _, _ = post('/v1/chat/completions', next_payload, {'X-ST-Request-ID': 'e' * 32})
            self.assertEqual(200, status)
            self.assertEqual(2, len(sent))
            # No tool replay, no fabricated result, no model call from finish.
            self.assertEqual(next_messages, sent[1]['messages'][1:])
            old_timer.fire()
            _, _, repeated = post('/v1/turns/finish', receipt)
            self.assertEqual('retired', json.loads(repeated)['state'])
            self.assertEqual(2, len(sent))
        finally:
            if app._current_session is not None:
                app._cancel_recovery_timer(app._current_session)
            server.shutdown()
            server.server_close()
            worker.join(5)

    def test_json_orphaned_wait_closes_before_timeout_without_changed_history_guess(self):
        self.roundtrip(False)

    def test_sse_orphaned_wait_closes_before_timeout_without_changed_history_guess(self):
        self.roundtrip(True)

    def test_legacy_client_response_does_not_advertise_automatic_finish(self):
        self.roundtrip(False, nonce=False)

    def test_upstream_error_response_does_not_advertise_automatic_finish(self):
        self.roundtrip(False, upstream_error=True)


if __name__ == '__main__':
    unittest.main()
