"""Scoped process-restart recovery: synthetic state/HTTP, never production I/O."""
from dataclasses import replace
from email.message import Message
import io
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rikkahub_gateway.server import GatewayError, _GatewayHandler
from rikkahub_gateway.tests import test_gateway_turn_control as controls


class ThreadRecoveryTests(unittest.TestCase):
    # Reuse the established synthetic execution fixture, not its test cases.
    setUp = controls.TurnControlTests.setUp
    arm = controls.TurnControlTests.arm
    query = controls.TurnControlTests.query
    status = controls.TurnControlTests.status
    assert_code = controls.TurnControlTests.assert_code

    def thread_headers(self):
        return {key: value for key, value in self.headers.items() if key != 'X-ST-Request-ID'}

    def thread_query(self):
        return {'model': self.app.default_route.public_model, 'challenge': 'c' * 32}

    def snapshot(self, payload=None, headers=None):
        return self.app.thread_turn_status(payload or self.thread_query(),
                                           headers or self.thread_headers())

    def expected_base(self):
        return {'protocol': 'st-thread-recovery/1', **self.thread_query(),
                'thread_id': self.headers['X-ST-Thread-ID'], 'state': 'busy',
                'can_stop': False, 'external_tool_status': 'unknown'}

    def test_generating_returns_real_nonce_and_same_v1_binding_without_stop(self):
        before = len(self.control.calls)
        result = self.snapshot()
        self.assertEqual({**self.expected_base(), 'state': 'owned_busy',
                          'request_id': 'a' * 32, 'binding': self.status()['binding'],
                          'turn_state': 'generating'}, result)
        self.assertFalse(result['can_stop'])
        self.assertNotEqual(result['challenge'], result['request_id'])
        self.assertEqual(before, len(self.control.calls))
        self.assertIs(self.prepared.session, self.app._current_session)
        self.assertEqual(1, self.prepared.session.request_generation)
        self.assertFalse(self.control.closed.is_set())

    def test_delivered_wait_can_be_reattached_then_stopped_only_by_existing_exact_protocol(self):
        self.arm()
        before = len(self.control.calls)
        recovered = self.snapshot()
        self.assertEqual('owned_busy', recovered['state'])
        self.assertEqual('waiting_tool_results', recovered['turn_state'])
        self.assertTrue(recovered['can_stop'])
        self.assertEqual(before, len(self.control.calls))
        self.assertFalse(self.control.closed.is_set())
        exact = {'model': recovered['model'], 'request_id': recovered['request_id'],
                 'binding': recovered['binding']}
        self.assertEqual('retired', self.app.control_turn(exact, self.thread_headers(), stop=True)['state'])
        self.assertIsNone(self.app._current_session)
        idle = self.snapshot()
        self.assertEqual({**self.expected_base(), 'state': 'gateway_lane_idle'}, idle)

    def test_not_delivered_wait_and_live_generation_cannot_be_stopped_after_reattachment(self):
        for arm in (False, True):
            if arm:
                self.arm(delivered=False)
            recovered = self.snapshot()
            self.assertFalse(recovered['can_stop'])
            self.assertEqual('generating', recovered['turn_state'])
            before = len(self.control.calls)
            self.assert_code('turn_control_not_delivered', lambda: self.app.control_turn(
                {'model': recovered['model'], 'request_id': recovered['request_id'],
                 'binding': recovered['binding']}, self.thread_headers(), stop=True))
            self.assertEqual(before, len(self.control.calls))

    def test_cleanup_pending_remains_busy_and_does_not_close_or_retry_control(self):
        self.arm()
        self.prepared.session.recovery_pending = True
        before = len(self.control.calls)
        recovered = self.snapshot()
        self.assertEqual('owned_busy', recovered['state'])
        self.assertEqual('cleanup_pending', recovered['turn_state'])
        self.assertTrue(recovered['can_stop'])
        self.assertEqual(before, len(self.control.calls))
        self.assertFalse(self.control.closed.is_set())

    def test_foreign_thread_is_busy_without_disclosing_current_identifiers(self):
        self.arm()
        headers = {**self.thread_headers(), 'X-ST-Thread-ID': 'orbis:other'}
        before = len(self.control.calls)
        result = self.snapshot(headers=headers)
        self.assertEqual({**self.expected_base(), 'thread_id': 'orbis:other'}, result)
        wire = json.dumps(result)
        for value in (self.prepared.session.thread_id, self.prepared.session.client_request_id,
                      self.prepared.session.control_session_id, self.prepared.session.wake_id,
                      self.prepared.session.wake_capability, self.prepared.session.execution_batch_id):
            self.assertNotIn(value, wire)
        self.assertEqual(before, len(self.control.calls))

    def test_foreign_known_model_is_busy_not_idle_and_does_not_disclose_binding(self):
        other = replace(self.app.default_route, public_model='synthetic-other')
        self.app.routes = {**self.app.routes, other.public_model: other}
        result = self.snapshot(payload={**self.thread_query(), 'model': other.public_model})
        self.assertEqual({**self.expected_base(), 'model': other.public_model}, result)
        self.assertNotIn('request_id', result)
        self.assertNotIn('binding', result)

    def test_matching_legacy_turn_without_a_real_nonce_is_only_busy(self):
        for nonce in ('', 'not-a-real-client-nonce', 'A' * 32, None, 123):
            self.prepared.session.client_request_id = nonce
            self.assertEqual(self.expected_base(), self.snapshot())

    def test_idle_reflects_only_requested_scope_and_never_guesses_external_effects(self):
        self.app.finish_turn(self.prepared, keep_for_tools=False)
        before = len(self.control.calls)
        payload = {**self.thread_query(), 'challenge': 'd' * 32}
        headers = {**self.thread_headers(), 'X-ST-Thread-ID': 'orbis:another-client-scope'}
        result = self.snapshot(payload, headers)
        self.assertEqual({**self.expected_base(), **payload,
                          'thread_id': headers['X-ST-Thread-ID'], 'state': 'gateway_lane_idle'}, result)
        self.assertEqual(before, len(self.control.calls))
        self.assertNotIn('request_id', result)
        self.assertEqual('unknown', result['external_tool_status'])

    def test_challenge_has_no_retirement_or_authority_semantics(self):
        self.arm()
        before = len(self.control.calls)
        for challenge in ('a' * 32, 'b' * 32, 'f' * 32):
            result = self.snapshot(payload={**self.thread_query(), 'challenge': challenge})
            self.assertEqual(challenge, result['challenge'])
            self.assertEqual('a' * 32, result['request_id'])
            self.assertEqual('owned_busy', result['state'])
        self.assertEqual(before, len(self.control.calls))
        self.assertEqual({}, self.app._client_stop_receipts)

    def test_reattached_snapshot_cannot_stop_replacement_or_changed_revision(self):
        self.arm()
        old = self.snapshot()
        exact = {'model': old['model'], 'request_id': old['request_id'], 'binding': old['binding']}
        self.prepared.session.execution_revision += 1
        self.assert_code('turn_control_binding_mismatch', lambda: self.app.control_turn(
            exact, self.thread_headers(), stop=True))
        self.app.finish_turn(self.prepared, keep_for_tools=False)
        new = self.app.prepare_turn(self.payload, {**self.headers, 'X-ST-Request-ID': 'b' * 32})
        before = len(self.control.calls)
        self.assert_code('turn_control_binding_mismatch', lambda: self.app.control_turn(
            exact, self.thread_headers(), stop=True))
        self.assertIs(new.session, self.app._current_session)
        self.assertEqual(before, len(self.control.calls))
        self.assertEqual('b' * 32, self.snapshot()['request_id'])

    def test_idle_snapshot_is_not_a_lease_and_existing_409_still_blocks_another_turn(self):
        self.app.finish_turn(self.prepared, keep_for_tools=False)
        self.assertEqual('gateway_lane_idle', self.snapshot()['state'])
        other = self.app.prepare_turn(self.payload,
            {**self.headers, 'X-ST-Thread-ID': 'orbis:new-owner', 'X-ST-Request-ID': 'b' * 32})
        self.assertEqual('busy', self.snapshot()['state'])
        self.assert_code('human_turn_in_progress', lambda: self.app.prepare_turn(self.payload, self.headers))
        self.assertIs(other.session, self.app._current_session)

    def test_removed_alias_allows_reattachment_only_to_its_existing_immutable_route(self):
        self.app.routes = {}
        self.assertEqual('owned_busy', self.snapshot()['state'])
        self.app.finish_turn(self.prepared, keep_for_tools=False)
        self.assert_code('invalid_thread_status_request', self.snapshot)

    def test_bad_auth_scope_nonce_and_payload_never_touch_control(self):
        before = len(self.control.calls)
        self.assert_code('unauthorized', lambda: self.snapshot(headers={**self.thread_headers(),
                         'Authorization': 'Bearer synthetic-wrong'}))
        for payload in ({}, {'model': self.thread_query()['model']},
                        {**self.thread_query(), 'challenge': 'a' * 31},
                        {**self.thread_query(), 'challenge': 'A' * 32},
                        {**self.thread_query(), 'challenge': None},
                        {**self.thread_query(), 'model': 'unknown-route'},
                        {**self.thread_query(), 'model': 'bad\nmodel'},
                        {**self.thread_query(), 'request_id': 'a' * 32},
                        {**self.thread_query(), 'binding': {}},
                        {**self.thread_query(), 'stop': True}):
            self.assert_code('invalid_thread_status_request', lambda payload=payload:
                             self.app.thread_turn_status(payload, self.thread_headers()))
        for thread in ('', 'a' * 257, 'bad thread', 'bad\nthread'):
            self.assert_code('invalid_thread_status_request', lambda thread=thread:
                             self.snapshot(headers={**self.thread_headers(), 'X-ST-Thread-ID': thread}))
        for extra in ({'X-Session-ID': 'orbis:legacy'}, {'X-ST-Request-ID': 'a' * 32}):
            self.assert_code('invalid_thread_status_request', lambda extra=extra:
                             self.snapshot(headers={**self.thread_headers(), **extra}))
        self.assertEqual(before, len(self.control.calls))

    def test_lock_wait_is_bounded_and_cannot_run_a_stop_as_fallback(self):
        held, release = threading.Event(), threading.Event()
        def occupy():
            with self.app._lock:
                held.set()
                release.wait(5)
        worker = threading.Thread(target=occupy)
        worker.start()
        self.assertTrue(held.wait(1))
        before = len(self.control.calls)
        started = time.perf_counter()
        try:
            self.assert_code('thread_status_unavailable', self.snapshot)
            self.assertLess(time.perf_counter() - started, 3.5)
            self.assertEqual(before, len(self.control.calls))
        finally:
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())

    def handler(self, raw=None, *, extra=(), omit=(), path='/v1/turns/thread-status'):
        raw = json.dumps(self.thread_query()).encode() if raw is None else raw
        headers = Message()
        for key, value in {**self.thread_headers(), 'Content-Type': 'application/json',
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

    def test_http_reattachment_is_no_store_and_contains_no_control_capabilities(self):
        self.arm()
        before = len(self.control.calls)
        handler = self.handler()
        handler.do_POST()
        self.assertEqual([200], handler.statuses)
        self.assertEqual('no-store', handler.response_headers['Cache-Control'])
        self.assertTrue(handler.close_connection)
        self.assertEqual(self.snapshot(), json.loads(handler.wfile.getvalue()))
        wire = handler.wfile.getvalue().decode()
        for value in (self.prepared.session.wake_id, self.prepared.session.wake_capability,
                      self.prepared.session.execution_batch_id, self.app.config.host_token):
            self.assertNotIn(value, wire)
        self.assertEqual(before, len(self.control.calls))

    def test_http_rejects_ambiguous_auth_or_framing_before_reading_body(self):
        cases = [({'omit': ('Authorization',)}, 401),
                 ({'extra': [('Authorization', 'Bearer duplicate')]}, 401),
                 ({'extra': [('Content-Length', '1')]}, 400),
                 ({'extra': [('Content-Type', 'application/json')]}, 400),
                 ({'extra': [('X-ST-Thread-ID', 'orbis:other')]}, 400),
                 ({'omit': ('X-ST-Thread-ID',)}, 400),
                 ({'extra': [('X-Session-ID', 'orbis:other')]}, 400),
                 ({'extra': [('Transfer-Encoding', 'chunked')]}, 400),
                 ({'path': '/v1/turns/thread-status?model=other'}, 400)]
        before = len(self.control.calls)
        for arguments, expected in cases:
            handler = self.handler(**arguments)
            handler.do_POST()
            self.assertEqual([expected], handler.statuses, arguments)
            self.assertEqual(0, handler.rfile.tell(), arguments)
            self.assertEqual('no-store', handler.response_headers['Cache-Control'])
        self.assertEqual(before, len(self.control.calls))

    def test_http_rejects_oversize_duplicate_unknown_and_malformed_json(self):
        model = self.thread_query()['model']
        invalid = [(b'x' * 2049, 413), (b'{', 400), (b'[]', 400),
                   (json.dumps({**self.thread_query(), 'stop': True}).encode(), 400),
                   (('{"model":' + json.dumps(model) + ',"challenge":"' + 'c' * 32 +
                     '","challenge":"' + 'd' * 32 + '"}').encode(), 400)]
        for raw, expected in invalid:
            handler = self.handler(raw)
            handler.do_POST()
            self.assertEqual([expected], handler.statuses)
            self.assertTrue(handler.close_connection)


if __name__ == '__main__':
    unittest.main()
