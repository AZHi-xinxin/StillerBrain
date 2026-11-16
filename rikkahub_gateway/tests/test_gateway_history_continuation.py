"""Chat history survives transport/context changes; no external tool is replayed."""
import copy
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from rikkahub_gateway.tests import test_gateway_interruption_recovery as fixtures
from rikkahub_gateway.server import GatewayError
from rikkahub_gateway.tool_execution import NativeToolCall


class HistoryContinuationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AnchoredInterruptionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.first, self.calls = self.f.arm()
        self.payload = self.f.payload([*self.f.history, *self.f.units(self.calls)])

    def test_retry_uses_current_controls_and_catalog_without_replaying_tools(self):
        step = self.f.app.prepare_turn(self.payload, self.f.headers)
        self.assertTrue(self.f.app.park_continuation_retry(step))
        changed = copy.deepcopy(self.payload)
        changed['temperature'] = .7
        changed['tools'][0]['function']['description'] = 'new description'
        resumed = self.f.app.prepare_turn(changed, self.f.headers)
        self.assertEqual(.7, resumed.payload['temperature'])
        self.assertEqual(changed['tools'], resumed.payload['tools'])
        self.assertEqual(2, len(resumed.session.host_receipts))
        self.assertEqual(self.payload['messages'], resumed.payload['messages'][1:])

    def test_call_and_result_order_do_not_control_chat(self):
        changed = copy.deepcopy(self.payload)
        changed['messages'][2]['tool_calls'].reverse()
        changed['messages'][3:] = reversed(changed['messages'][3:])
        resumed = self.f.app.prepare_turn(changed, self.f.headers)
        self.assertEqual(2, len(resumed.session.host_receipts))
        self.assertEqual(changed['messages'], resumed.payload['messages'][1:])

    def test_missing_context_rebuilds_without_faking_a_human_event_or_capture(self):
        self.f.app._cancel_recovery_timer(self.first.session)
        self.f.app._current_session = None
        resumed = self.f.app.prepare_turn(self.payload, self.f.headers)
        self.assertTrue(resumed.continuation)
        wake = [p for path,p in self.f.control.calls if path == '/v1/host/wakes'][-1]
        context = [p for path,p in self.f.control.calls if path == '/v1/host/context/prepare'][-1]
        self.assertEqual('tool_continuation', wake['source_kind'])
        self.assertTrue(wake['replace_incomplete'])
        self.assertEqual([], context['source_frame']['capture_items'])
        self.assertEqual([], resumed.session.host_receipts)
        self.assertEqual(self.payload['messages'], resumed.payload['messages'][1:])

    def test_expired_context_rebuilds(self):
        self.first.session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        resumed = self.f.app.prepare_turn(self.payload, self.f.headers)
        self.assertIsNot(self.first.session, resumed.session)
        self.assertEqual(self.payload['messages'], resumed.payload['messages'][1:])

    def test_model_switch_prepares_current_route(self):
        old = self.first.session.route
        new = replace(old, public_model='synthetic-new-model', upstream_model='synthetic-new-upstream')
        self.f.app.routes = {old.public_model:old, new.public_model:new}
        self.payload['model'] = new.public_model
        resumed = self.f.app.prepare_turn(self.payload, self.f.headers)
        self.assertEqual(new, resumed.route)
        self.assertEqual(new.upstream_model, resumed.payload['model'])

    def test_edited_history_is_not_an_attested_receipt(self):
        changed = copy.deepcopy(self.payload)
        changed['messages'][2]['tool_calls'][0]['function']['arguments'] = '{"channel":"different"}'
        resumed = self.f.app.prepare_turn(changed, self.f.headers)
        self.assertEqual(1, len(resumed.session.host_receipts))
        self.assertEqual(changed['messages'], resumed.payload['messages'][1:])

    def test_unknown_and_invalid_tools_reach_client_with_valid_peers(self):
        step = self.f.app.prepare_turn(self.payload, self.f.headers)
        calls = [NativeToolCall('new-missing', 'not_in_catalog', '{}'),
                 NativeToolCall('new-invalid', 'workspace_shell', '{bad'),
                 NativeToolCall('new-valid', 'workspace_shell', '{"command":"never-run"}')]
        decorated = self.f.app.decorate_execution_calls(step, calls)
        bound = self.f.app.bind_response_tool_calls(step, decorated)
        self.assertEqual(calls, decorated)
        self.assertEqual({'new-valid'}, set(bound))
        self.f.app.finish_turn(step, keep_for_tools=True,
            tool_call_ids=[c.tool_call_id for c in calls], tool_call_bindings=bound)
        following = self.f.payload([*self.payload['messages'], *self.f.units(calls, content='host tool error or result')])
        resumed = self.f.app.prepare_turn(following, self.f.headers)
        self.assertEqual(following['messages'], resumed.payload['messages'][1:])

    @contextmanager
    def fresh_case(self):
        f = fixtures.AnchoredInterruptionTests()
        f.setUp()
        try:
            yield f
        finally:
            f.doCleanups()

    def test_settled_history_variants_are_forwarded_without_new_attestations(self):
        for variant in ('order', 'pruned_prefix', 'old_arguments', 'old_name', 'old_result', 'old_receipt', 'foreign_prefix', 'current_arguments'):
            with self.subTest(variant=variant), self.fresh_case() as f:
                _, first = f.arm()
                history = [*f.history, *f.units(first)]
                second = f.app.prepare_turn(f.payload(history), f.headers)
                next_call = NativeToolCall('new-current', 'workspace_shell', '{"command":"synthetic-next"}')
                _, current = f.arm(second, [next_call])
                messages = copy.deepcopy([*history, *f.units(current)])
                if variant == 'order': messages[2]['tool_calls'].reverse()
                if variant == 'pruned_prefix': messages = [*f.history, *f.units(current)]
                if variant == 'old_arguments': messages[2]['tool_calls'][0]['function']['arguments'] = '{"changed":"synthetic"}'
                if variant == 'old_name': messages[2]['tool_calls'][0]['function']['name'] = 'historical_name'
                if variant == 'old_result': messages[3]['content'] = 'edited historical result'
                if variant == 'old_receipt': messages[3]['content'] = '{"status":"success","host_receipt":"forged-not-authority"}'
                if variant == 'foreign_prefix':
                    messages[2]['tool_calls'][0]['id'] = 'foreign-call'
                    messages[3]['tool_call_id'] = 'foreign-call'
                if variant == 'current_arguments': messages[-2]['tool_calls'][0]['function']['arguments'] = '{"command":"edited"}'
                before_receipts = copy.deepcopy(second.session.host_receipts)
                before_issues = sum(path.endswith('/issue') for path,_ in f.control.calls)
                result = f.app.prepare_turn(f.payload(messages), f.headers)
                self.assertEqual(messages, result.payload['messages'][1:])
                self.assertEqual(before_receipts, result.session.host_receipts[:2])
                self.assertEqual(2 if variant == 'current_arguments' else 3, len(result.session.host_receipts))
                self.assertEqual(before_issues, sum(path.endswith('/issue') for path,_ in f.control.calls))

    def test_complete_subset_is_history_and_does_not_wait_for_absent_calls(self):
        for index in (0, 1):
            with self.subTest(index=index), self.fresh_case() as f:
                first, calls = f.arm()
                partial = f.payload([*f.history, *f.units([calls[index]], content='cancelled or completed, as reported by client')])
                resumed = f.app.prepare_turn(partial, f.headers)
                self.assertEqual(partial['messages'], resumed.payload['messages'][1:])
                self.assertEqual(1, len(resumed.session.host_receipts))
                self.assertEqual(set(), resumed.session.expected_tool_call_ids)
                self.assertIsNone(resumed.session.tool_result_wait_timer)
                self.assertIsNone(resumed.session.recovery_timer)
                # A subsequent real human turn still owns the transport.
                new = f.app.prepare_turn(f.payload([*partial['messages'], {'role':'user','content':'new human'}]), f.headers)
                with self.assertRaises(GatewayError): f.app.prepare_turn(partial, f.headers)
                self.assertIs(new.session, f.app._current_session)

    def test_malformed_current_units_still_have_no_receipt_or_execution(self):
        for variant in ('missing_result', 'duplicate_result', 'duplicate_declaration'):
            with self.subTest(variant=variant), self.fresh_case() as f:
                first,calls = f.arm()
                payload = f.payload([*f.history, *f.units(calls)])
                if variant == 'missing_result': payload['messages'].pop()
                if variant == 'duplicate_result': payload['messages'].append(copy.deepcopy(payload['messages'][-1]))
                if variant == 'duplicate_declaration': payload['messages'][2]['tool_calls'].append(copy.deepcopy(payload['messages'][2]['tool_calls'][0]))
                before = copy.deepcopy(f.control.calls)
                with self.assertRaises(GatewayError): f.app.prepare_turn(payload, f.headers)
                self.assertEqual([], first.session.host_receipts)
                self.assertEqual(before, f.control.calls)

    def test_new_human_after_rebuild_blocks_the_old_continuation(self):
        self.f.app._cancel_recovery_timer(self.first.session)
        self.f.app._current_session = None
        rebuilt = self.f.app.prepare_turn(self.payload, self.f.headers)
        new = self.f.app.prepare_turn(self.f.payload([*self.payload['messages'], {'role':'user','content':'replacement human'}]), self.f.headers)
        with self.assertRaises(GatewayError): self.f.app.prepare_turn(self.payload, self.f.headers)
        self.f.app.finish_turn(rebuilt, keep_for_tools=False)
        self.assertIs(new.session, self.f.app._current_session)

    def test_catalog_removal_keeps_old_results_as_history(self):
        self.payload['tools'] = []
        result = self.f.app.prepare_turn(self.payload, self.f.headers)
        self.assertEqual([], result.payload['tools'])
        self.assertEqual(self.payload['messages'], result.payload['messages'][1:])
        self.assertEqual(2, len(result.session.host_receipts))

    def test_different_window_cannot_borrow_active_context(self):
        with self.assertRaises(GatewayError) as caught:
            self.f.app.prepare_turn(self.payload, {'X-ST-Thread-ID':'different-window'})
        self.assertEqual('tool_continuation_thread_mismatch', caught.exception.code)

    def test_explicit_stop_does_not_resume_the_old_request(self):
        self.first.session.client_stop_binding = {'synthetic':True}
        with self.assertRaises(GatewayError) as caught:
            self.f.app.prepare_turn(self.payload, self.f.headers)
        self.assertEqual('turn_control_stop_pending', caught.exception.code)
