"""Fresh input takes ownership; old results are never a gate or a receipt."""
from dataclasses import replace
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp_server.control_server import ControlApplication
from runtime.onboarding import ModuleOneOnboardingStore
from runtime.execution_binding import ExecutionBindingError, ExecutionStore
from rikkahub_gateway.server import GatewayApplication, GatewayError
from rikkahub_gateway.tests.test_gateway import config
from rikkahub_gateway.tests.test_gateway_interruption_recovery import ManualTimer, InterruptionControl


class FreshHumanReplacementTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('socket.socket', side_effect=AssertionError('network forbidden')))
        self.enterContext(patch('rikkahub_gateway.server.threading.Timer', ManualTimer))
        self.enterContext(patch('rikkahub_gateway.server._FAILURE_LOGGER.warning'))
        self.config = replace(config(), require_execution_binding=True, execution_epoch='synthetic-epoch')
        self.control = InterruptionControl()
        self.app = GatewayApplication(self.config, control=self.control, upstream=object())
        self.payload = {'model': self.config.public_model, 'messages': [{'role': 'user', 'content': 'hello'}]}
        self.headers = {'X-ST-Thread-ID': 'synthetic-thread'}
        self.addCleanup(lambda: self.app._cancel_recovery_timer(self.app._current_session)
                        if self.app._current_session else None)

    def test_fresh_window_missing_old_results_and_failed_close_never_wait(self):
        for thread in ('synthetic-thread', 'another-window'):
            old = self.app.prepare_turn(self.payload, self.headers)
            self.app.finish_turn(old, keep_for_tools=True, tool_call_ids=['old-call'])
            self.app.mark_response_delivered(old)
            old_timer = old.session.tool_result_wait_timer
            self.control.close_failures = 99
            self.control.running = 3
            before = len(self.control.calls)
            new = self.app.prepare_turn(self.payload, {'X-ST-Thread-ID': thread})
            self.assertIsNot(old.session, new.session)
            self.assertEqual(['/v1/host/wakes', '/v1/host/context/prepare', '/v1/host/context/confirm'],
                             [p for p, _ in self.control.calls[before:]])
            self.assertTrue(old_timer.cancelled)
            old_timer.fire()
            self.app.finish_turn(old, keep_for_tools=False)
            self.assertIs(new.session, self.app._current_session)
            with self.assertRaises(GatewayError):
                self.app.assert_response_current(old)

    def test_same_transport_nonce_cannot_interrupt_its_own_active_request(self):
        headers = {**self.headers, 'X-ST-Request-ID': 'a' * 32}
        old = self.app.prepare_turn(self.payload, headers)
        before = len(self.control.calls)
        with self.assertRaisesRegex(GatewayError, 'human_event_already_accepted'):
            self.app.prepare_turn(self.payload, headers)
        self.assertIs(old.session, self.app._current_session)
        self.assertEqual(before, len(self.control.calls))
        self.app.assert_response_current(old)

    def test_automatic_input_cannot_supersede_human(self):
        old = self.app.prepare_turn(self.payload, self.headers)
        with self.assertRaisesRegex(GatewayError, '上一轮仍在生成'):
            self.app.prepare_turn(self.payload, {**self.headers, 'X-ST-Source-Kind': 'consultation-wake',
                'X-Request-ID': 'synthetic-notification', 'X-ST-Client-ID': 'orbis'})
        self.assertIs(old.session, self.app._current_session)

    def test_old_control_cannot_silently_claim_replacement_support(self):
        original = self.control.post
        def old_control(path, body):
            response = original(path, body)
            response.pop('human_replacement', None)
            return response
        self.control.post = old_control
        with self.assertRaisesRegex(GatewayError, 'human_replacement_unconfirmed'):
            self.app.prepare_turn(self.payload, self.headers)
        self.assertIsNone(self.app._current_session)

    def test_real_control_replaces_running_registry_even_after_gateway_lost_session(self):
        folder = self.enterContext(tempfile.TemporaryDirectory(prefix='fresh-human-synthetic-'))
        database = Path(folder) / 'synthetic.db'
        secret = b'synthetic-fresh-human-secret-00000000000'
        common = {'owner_id': 'synthetic-owner', 'model_id': 'synthetic-model'}
        onboarding = ModuleOneOnboardingStore(database, capability_secret=secret)
        executions = ExecutionStore(database, deployment_epoch=self.config.execution_epoch, capability_secret=secret)
        control = ControlApplication(onboarding, **common, host_token=self.config.host_token,
            human_token='synthetic-human-token-000000000000000000', human_actor_id='synthetic-human',
            execution_store=executions)
        class Transport:
            def post(inner, path, payload):
                status, body = control.handle('POST', path, {'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + self.config.host_token}, json.dumps(payload).encode())
                if status != 200:
                    raise AssertionError((status, body))
                return body
        self.app.control = Transport()
        props = {'execution_ref': {'type': 'string', 'pattern': '^stexec_[A-Za-z0-9_-]{43}$',
            'x-stbrain-execution-tool': 'stbrain_open', 'x-stbrain-execution-contract': 'st-execution/1'}}
        payload = {**self.payload, 'tools': [{'type': 'function', 'function': {'name': 'stbrain_open',
            'parameters': {'type': 'object', 'properties': props}}}]}
        old = self.app.prepare_turn(payload, self.headers)
        from rikkahub_gateway.tool_execution import NativeToolCall
        decorated = self.app.decorate_execution_calls(old, [NativeToolCall('old-managed', 'stbrain_open', '{}')])
        ref = json.loads(decorated[0].arguments_text)['execution_ref']
        claim = executions.claim(**common, execution_ref=ref, tool_name='stbrain_open', arguments={})
        # Lost transport state must not resurrect the five-minute owner/model hold.
        self.app._current_session = None
        new = self.app.prepare_turn(payload, {'X-ST-Thread-ID': 'new-window'})
        with executions.bind(claim):
            with self.assertRaisesRegex(ExecutionBindingError, 'execution_claim_not_current'):
                onboarding.open_brain_context(**common, present_details=False)
        executions.finish(claim)
        fresh_calls = self.app.decorate_execution_calls(new, [NativeToolCall('fresh-managed', 'stbrain_open', '{}')])
        fresh = executions.claim(**common,
            execution_ref=json.loads(fresh_calls[0].arguments_text)['execution_ref'], tool_name='stbrain_open', arguments={})
        with executions.bind(fresh):
            self.assertTrue(onboarding.open_brain_context(**common, present_details=False)['write_context_available'])
        executions.finish(fresh)
