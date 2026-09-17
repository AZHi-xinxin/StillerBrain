"""Delivery-based timeout; synthetic Control, no live data or external network."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import threading
import time
import unittest
from unittest.mock import patch

from rikkahub_gateway.server import GatewayApplication, GatewayConfig, GatewayError
from rikkahub_gateway.tool_execution import NativeToolCall
from rikkahub_gateway.tests import test_gateway as fixtures
from rikkahub_gateway.tests.test_gateway import config
from rikkahub_gateway.tests.test_gateway_execution_recovery import ExecutionControl, st_tool

REAL_TIMER = threading.Timer
REAL_MONOTONIC = time.monotonic


class FakeTimer:
    def __init__(self, interval, function, args=(), kwargs=None):
        self.interval, self.function, self.args = interval, function, args
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # Model the cancellation race: callbacks can already be running when
        # cancel is called, so every callback must verify its captured identity.
        self.function(*self.args, **self.kwargs)


class ToolResultWaitTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket', 'socket.create_connection', 'sqlite3.connect', 'subprocess.Popen'):
            self.enterContext(patch(target, side_effect=AssertionError('external_access_forbidden')))
        self.enterContext(patch('rikkahub_gateway.server.threading.Timer', FakeTimer))
        self.now = 1000.0
        self.enterContext(patch('rikkahub_gateway.server.time.monotonic', side_effect=lambda: self.now))
        self.control = ExecutionControl()
        self.app = GatewayApplication(replace(config(), require_execution_binding=True,
            execution_epoch='synthetic-timeout-epoch'), control=self.control, upstream=object())
        self.headers = {'X-ST-Thread-ID': 'synthetic-timeout-thread'}
        self.user = {'role': 'user', 'content': 'synthetic wait test'}
        self.tools = fixtures.GatewayTests.bound_tools() + [st_tool()]
        self.payload = {'messages': [self.user], 'tools': self.tools}
        self.prepared = self.app.prepare_turn(self.payload, self.headers)
        self.sessions = [self.prepared.session]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for session in self.sessions:
            self.app._cancel_recovery_timer(session)

    def arm(self, *, calls=None, delivered=True, prepared=None):
        prepared = prepared or self.prepared
        authored = calls or [NativeToolCall('synthetic-open', 'stbrain_open', '{}')]
        decorated = self.app.decorate_execution_calls(prepared, authored)
        bindings = self.app.bind_response_tool_calls(prepared, decorated)
        self.app.finish_turn(prepared, keep_for_tools=True,
            tool_call_ids=[call.tool_call_id for call in decorated], tool_call_bindings=bindings)
        if delivered:
            self.app.mark_response_delivered(prepared)
        return decorated

    def results(self, calls, *, prefix=None):
        return {'tools': self.tools, 'messages': [*(prefix or [self.user]),
            {'role': 'assistant', 'content': 'synthetic tool batch',
             'tool_calls': [fixtures.GatewayTests.wire_call(call) for call in calls]},
            *[{'role': 'tool', 'tool_call_id': call.tool_call_id, 'content': '{"synthetic":"result"}'}
              for call in calls]]}

    def expire(self, session=None):
        session = session or self.prepared.session
        self.now = session.tool_result_wait_deadline
        session.tool_result_wait_timer.fire()

    def test_default_is_five_minutes_and_does_not_change_other_timeouts(self):
        self.assertEqual(300.0, self.app.config.tool_result_wait_seconds)
        self.assertEqual(180.0, self.app.config.abnormal_wait_seconds)
        self.assertEqual(120.0, self.app.config.timeout_seconds)

    def test_starts_only_after_delivery_and_repeated_mark_does_not_extend(self):
        self.arm(delivered=False)
        session = self.prepared.session
        self.assertIsNone(session.tool_result_wait_deadline)
        self.assertIsNone(session.tool_result_wait_timer)
        self.now = 2000.0
        self.app.mark_response_delivered(self.prepared)
        timer = session.tool_result_wait_timer
        self.assertEqual(2300.0, session.tool_result_wait_deadline)
        self.assertTrue(timer.started)
        self.assertTrue(timer.daemon)
        self.now = 2200.0
        self.app.mark_response_delivered(self.prepared)
        self.assertEqual(2300.0, session.tool_result_wait_deadline)
        self.assertIs(timer, session.tool_result_wait_timer)

    def test_long_generation_does_not_arm_or_get_ended_by_timer(self):
        session = self.prepared.session
        session.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        self.now += 10000
        self.assertIsNone(session.tool_wait_armed_at)
        self.assertIsNone(session.tool_result_wait_deadline)
        self.assertFalse(self.app._expired_armed_tool_wait(session))
        with self.assertRaises(GatewayError) as raised:
            self.app.prepare_turn(self.payload, self.headers)
        self.assertEqual('human_turn_in_progress', raised.exception.code)
        self.assertFalse(self.control.closed.is_set())

    def test_before_boundary_rearms_remaining_time_instead_of_retiring(self):
        self.arm()
        session = self.prepared.session
        self.now = session.tool_result_wait_deadline - 0.1
        session.tool_result_wait_timer.fire()
        self.assertIs(session, self.app._current_session)
        self.assertAlmostEqual(0.1, session.tool_result_wait_timer.interval)
        self.assertFalse(self.control.closed.is_set())

    def test_exact_boundary_retires_without_another_http_request(self):
        self.arm()
        self.expire()
        self.assertIsNone(self.app._current_session)
        self.assertTrue(self.control.closed.is_set())
        self.assertTrue(self.control.revoked)
        self.assertIn('synthetic-open', self.app._retired_tool_call_ids)
        self.assertIsNone(self.prepared.session.tool_result_wait_timer)
        self.assertIsNone(self.prepared.session.tool_result_wait_deadline)
        self.assertIsNone(self.prepared.session.tool_wait_armed_at)

    def test_full_result_just_before_boundary_is_accepted_and_cancels_deadline(self):
        calls = self.arm()
        session = self.prepared.session
        timer = session.tool_result_wait_timer
        self.now = session.tool_result_wait_deadline - 0.1
        continued = self.app.prepare_turn(self.results(calls), self.headers)
        self.assertTrue(continued.continuation)
        self.assertIsNone(session.tool_result_wait_deadline)
        self.assertIsNone(session.tool_result_wait_timer)
        self.assertTrue(timer.cancelled)
        timer.fire()
        self.assertIs(session, self.app._current_session)
        self.assertFalse(self.control.closed.is_set())

    def test_late_result_request_enforces_deadline_even_if_watchdog_delayed(self):
        calls = self.arm()
        self.now = self.prepared.session.tool_result_wait_deadline
        with self.assertRaisesRegex(GatewayError, 'tool_continuation_context_lost'):
            self.app.prepare_turn(self.results(calls), self.headers)
        self.assertIsNone(self.app._current_session)
        self.assertTrue(self.control.closed.is_set())

    def test_late_callback_cannot_close_new_human_turn_or_replay_tools(self):
        calls = self.arm()
        old_timer = self.prepared.session.tool_result_wait_timer
        self.expire()
        next_turn = self.app.prepare_turn({'messages': [{'role': 'user', 'content': 'synthetic new turn'}],
                                           'tools': self.tools}, self.headers)
        self.sessions.append(next_turn.session)
        before = deepcopy(self.control.calls)
        old_timer.fire()
        self.assertEqual(before, self.control.calls)
        self.assertIs(next_turn.session, self.app._current_session)
        with self.assertRaises(GatewayError):
            self.app.prepare_turn(self.results(calls), self.headers)
        self.assertEqual(1, sum(path.endswith('/issue') for path, _ in self.control.calls))

    def test_new_batch_gets_fresh_deadline_and_old_timer_cannot_close_it(self):
        first = self.arm()
        session = self.prepared.session
        old_timer = session.tool_result_wait_timer
        self.now += 250
        first_history = self.results(first)
        continued = self.app.prepare_turn(first_history, self.headers)
        self.now += 10
        self.arm(prepared=continued, calls=[NativeToolCall('synthetic-second-open', 'stbrain_open', '{}')])
        self.assertEqual(self.now + 300, session.tool_result_wait_deadline)
        before = deepcopy(self.control.calls)
        old_timer.fire()
        self.assertEqual(before, self.control.calls)
        self.assertIs(session, self.app._current_session)

    def test_running_claim_is_never_treated_as_complete_and_retries_in_five_seconds(self):
        self.arm()
        session = self.prepared.session
        self.control.running = 1
        self.expire()
        self.assertIs(session, self.app._current_session)
        self.assertTrue(session.recovery_pending)
        self.assertFalse(self.control.closed.is_set())
        self.assertFalse(session.execution_batch_closed)
        self.assertEqual(5.0, session.tool_result_wait_timer.interval)
        self.assertIsNone(session.abnormal_wait_started)
        self.control.running = 0
        self.now += 5
        session.tool_result_wait_timer.fire()
        self.assertTrue(self.control.closed.is_set())
        self.assertIsNone(self.app._current_session)

    def test_control_failure_never_opens_new_wake_and_cleanup_retries_not_tool(self):
        self.arm()
        original = self.control.post
        failed = True

        def fail(path, payload):
            if failed and path.endswith('/revoke'):
                raise GatewayError(502, 'synthetic_control_unavailable')
            return original(path, payload)

        self.control.post = fail
        self.expire()
        session = self.prepared.session
        self.assertIs(session, self.app._current_session)
        self.assertEqual(5.0, session.tool_result_wait_timer.interval)
        with self.assertRaises(GatewayError) as raised:
            self.app.prepare_turn(self.payload, self.headers)
        self.assertEqual('tool_wait_recovery_in_progress', raised.exception.code)
        self.assertEqual(1, self.control.seq)
        failed = False
        self.now += 5
        session.tool_result_wait_timer.fire()
        self.assertIsNone(self.app._current_session)
        self.assertEqual(1, sum(path.endswith('/issue') for path, _ in self.control.calls))

    def test_close_response_lost_retries_only_exact_close_not_expired_registry(self):
        self.arm()
        original = self.control.post
        lost = False

        def lose(path, payload):
            nonlocal lost
            if lost and path.startswith('/v1/host/tool-executions/'):
                raise GatewayError(409, 'execution_wake_not_current')
            result = original(path, payload)
            if path == '/v1/host/context/close' and not lost:
                lost = True
                raise GatewayError(502, 'synthetic_response_lost')
            return result

        self.control.post = lose
        self.expire()
        session = self.prepared.session
        self.assertTrue(session.execution_batch_closed)
        self.assertTrue(session.recovery_pending)
        self.assertEqual(5.0, session.tool_result_wait_timer.interval)
        self.now += 5
        session.tool_result_wait_timer.fire()
        self.assertIsNone(self.app._current_session)
        self.assertEqual(1, sum(path.endswith('/revoke') for path, _ in self.control.calls))
        self.assertEqual(2, sum(path == '/v1/host/context/close' for path, _ in self.control.calls))

    def test_external_tool_timeout_only_retires_wait_and_requires_confirmed_close(self):
        self.arm(calls=[NativeToolCall('synthetic-external', 'workspace_shell', '{"command":"synthetic only"}')])
        self.assertIsNone(self.prepared.session.execution_batch_id)
        original = self.control.post
        confirmed = False

        def close(path, payload):
            if path == '/v1/host/context/close' and not confirmed:
                return {'decision': 'not_confirmed'}
            return original(path, payload)

        self.control.post = close
        self.expire()
        self.assertIs(self.prepared.session, self.app._current_session)
        self.assertEqual(5.0, self.prepared.session.tool_result_wait_timer.interval)
        confirmed = True
        self.now += 5
        self.prepared.session.tool_result_wait_timer.fire()
        self.assertIsNone(self.app._current_session)
        self.assertIn('synthetic-external', self.app._retired_tool_call_ids)
        self.assertFalse(any(path.startswith('/v1/host/tool-executions/') for path, _ in self.control.calls))

    def test_wall_clock_change_does_not_affect_delivery_deadline(self):
        self.arm()
        session = self.prepared.session
        with patch('rikkahub_gateway.server.time.time', return_value=1e12):
            self.now = session.tool_result_wait_deadline - 1
            self.assertFalse(self.app._expired_armed_tool_wait(session))
        with patch('rikkahub_gateway.server.time.time', return_value=-1e12):
            self.now = session.tool_result_wait_deadline
            self.assertTrue(self.app._expired_armed_tool_wait(session))

    def test_stale_generation_guard_applies_without_managed_execution_revision(self):
        self.app.config = replace(self.app.config, require_execution_binding=False)
        first = self.arm(calls=[NativeToolCall('synthetic-external-one', 'workspace_shell', '{"command":"one"}')])
        session = self.prepared.session
        old_timer = session.tool_result_wait_timer
        continued = self.app.prepare_turn(self.results(first), self.headers)
        # Deliberately identical clock/deadline and execution revision; only the
        # request-generation guard can distinguish the old callback now.
        self.arm(prepared=continued, calls=[NativeToolCall('synthetic-external-two', 'workspace_shell', '{"command":"two"}')])
        self.assertEqual(old_timer.args[1], session.execution_revision)
        self.assertEqual(old_timer.args[-1], session.tool_result_wait_deadline)
        self.now = session.tool_result_wait_deadline
        before = deepcopy(self.control.calls)
        old_timer.fire()
        self.assertEqual(before, self.control.calls)
        self.assertIs(session, self.app._current_session)

    def test_original_shorter_partial_result_recovery_is_not_disabled(self):
        mixed = self.arm(calls=[NativeToolCall('synthetic-shell', 'workspace_shell', '{"command":"synthetic only"}'),
                               NativeToolCall('synthetic-open', 'stbrain_open', '{}')])
        deadline = self.prepared.session.tool_result_wait_deadline
        with self.assertRaises(GatewayError):
            self.app.prepare_turn(self.results(mixed[:1]), self.headers)
        self.assertEqual(180.0, self.prepared.session.recovery_timer.interval)
        self.assertEqual(deadline, self.prepared.session.tool_result_wait_deadline)

    def test_real_watchdog_releases_without_followup_http_or_sleeping_five_minutes(self):
        with patch('rikkahub_gateway.server.threading.Timer', REAL_TIMER), \
             patch('rikkahub_gateway.server.time.monotonic', side_effect=REAL_MONOTONIC):
            self.app.config = replace(self.app.config, tool_result_wait_seconds=0.03)
            self.arm()
            self.assertTrue(self.control.closed.wait(1.5))
            with self.app._lock:
                self.assertIsNone(self.app._current_session)
                self.assertIsNone(self.prepared.session.tool_result_wait_timer)


class ToolResultWaitConfigTests(unittest.TestCase):
    def test_invalid_nonfinite_nonpositive_or_unsupported_values_rejected(self):
        for value in (0, -1, float('nan'), float('inf'), -float('inf'), 1e300, True, '300', None):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                replace(config(), tool_result_wait_seconds=value)

    def test_explicit_env_setting_and_default_do_not_reconfigure_global_wake(self):
        env = {'STBRAIN_GATEWAY_TOKEN': 'g' * 32, 'STBRAIN_HOST_TOKEN': 'h' * 32,
            'STBRAIN_UPSTREAM_API_KEY': 'u' * 32, 'STBRAIN_HUMAN_TOKEN': 'x' * 32,
            'STBRAIN_CONTROL_URL': 'http://control.test', 'STBRAIN_UPSTREAM_BASE_URL': 'http://upstream.test/v1',
            'STBRAIN_GATEWAY_MODEL': 'synthetic-model', 'STBRAIN_UPSTREAM_MODEL': 'synthetic-upstream',
            'STBRAIN_WAKE_TTL_SECONDS': '1800'}
        with patch.dict('os.environ', env, clear=True):
            self.assertEqual(300, GatewayConfig.from_env().tool_result_wait_seconds)
            with patch.dict('os.environ', {'STBRAIN_GATEWAY_TOOL_RESULT_WAIT_SECONDS': '240'}):
                value = GatewayConfig.from_env()
                self.assertEqual(240, value.tool_result_wait_seconds)
                self.assertEqual(180, value.abnormal_wait_seconds)
                self.assertEqual(120, value.timeout_seconds)


if __name__ == '__main__':
    unittest.main()
