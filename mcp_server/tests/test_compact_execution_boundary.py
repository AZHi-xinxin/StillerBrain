"""Compact routing keeps real inner leases and direct module-one authority.

Synthetic temporary databases only. No live memories, accounts or network.
"""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp_server.compact_tool_dispatch import install_compact_tool_dispatch
from mcp_server.execution_guard import install_execution_guard
from mcp_server.person_reference_surface import install_person_reference_advisory_surface
from mcp_server.self_password import SelfPasswordAuthority, encode_password, install_self_password_guard
from runtime.compact_tool_routes import ACTION_CATEGORIES, CompactRouteError, resolve_compact_action
from runtime.execution_binding import ExecutionStore, canonical_hash, current_execution_claim
from runtime.onboarding import ModuleOneOnboardingStore
from rikkahub_gateway.server import GatewayApplication, GatewayError
from rikkahub_gateway.tool_execution import NativeToolCall
from rikkahub_gateway.tests.test_gateway import config
from rikkahub_gateway.tests.test_gateway_execution_recovery import ExecutionControl


class CompactAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('network_forbidden')))
        self.enterContext(patch('subprocess.Popen', side_effect=AssertionError('process_forbidden')))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='synthetic-compact-authority-')))
        self.common = {'owner_id': 'synthetic-owner', 'model_id': 'synthetic-model'}
        secret = 'synthetic-compact-secret-with-at-least-32-bytes'
        db = self.directory / 'synthetic.sqlite3'
        self.onboarding = ModuleOneOnboardingStore(db, capability_secret=secret)
        self.store = ExecutionStore(db, deployment_epoch='synthetic-compact', capability_secret=secret)
        self.authority = SelfPasswordAuthority(None)
        self.calls = []
        self.sequence = 0
        self.advisory = {'enabled': True, 'message': '合成默认人称提醒'}
        self.mcp = FastMCP('synthetic-compact')

        @self.mcp.tool()
        async def submit_self_model_candidate(write_context_ref: str | None = None,
                                               intent: str = 'submit') -> dict[str, Any]:
            claim = current_execution_claim()
            self.calls.append(('submit', claim))
            return {'decision': 'business_reached', 'bound': claim is not None, 'intent': intent}

        @self.mcp.tool()
        async def activate_self_model_candidate(write_context_ref: str | None = None,
                                                 intent: str = 'activate') -> dict[str, Any]:
            claim = current_execution_claim()
            self.calls.append(('activate', claim))
            return {'decision': 'business_reached', 'bound': claim is not None, 'intent': intent}

        @self.mcp.tool()
        async def query_self_model() -> dict[str, Any]:
            return {'decision': 'read_available'}

        @self.mcp.tool()
        async def stbrain_health() -> dict[str, Any]:
            self.calls.append(('health', current_execution_claim()))
            return {'decision': 'healthy'}

        @self.mcp.tool()
        async def remember_memory(module: str, content: str) -> dict[str, Any]:
            self.calls.append(('remember', current_execution_claim()))
            return {'decision': 'stored', 'stored': True}

        self.mcp._tool_manager.get_tool('submit_self_model_candidate').parameters['additionalProperties'] = False
        install_person_reference_advisory_surface(self.mcp, SimpleNamespace(advisory_status=lambda: self.advisory))
        install_self_password_guard(self.mcp, self.authority, execution_store=self.store,
                                    onboarding=self.onboarding, **self.common)
        install_execution_guard(self.mcp, store=self.store, onboarding=self.onboarding,
                                ordinary_authenticated=True, **self.common)
        install_compact_tool_dispatch(self.mcp)
        # The wake advertises ONLY the two stable facade names. No hidden
        # native operation appears in this catalog or earns a lease by its name.
        entries = [{'canonical_name': name, 'schema_hash': canonical_hash(self.mcp._tool_manager.get_tool(name).parameters)}
                   for name in ('stbrain_tools', 'stbrain_manage')]
        self.entries = {entry['canonical_name']: entry for entry in entries}
        self.catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                        'catalog_hash': canonical_hash(entries), 'entries': entries}
        self.wake = self.onboarding.issue_wake(**self.common, host_id='synthetic-host',
            thread_id='synthetic-thread', source_kind='human_message', source_event_id='synthetic-A')
        prepared = self.onboarding.build_pre_generation_context(**self.common,
            wake_id=self.wake['wake_id'], wake_capability=self.wake['wake_capability'],
            source_digest='synthetic-source', host_contract_digest='synthetic-contract', advertised_tools=self.catalog)
        self.onboarding.confirm_context_injected(**self.common, wake_id=self.wake['wake_id'],
            wake_capability=self.wake['wake_capability'], context_hash=prepared['context_hash'])
        self.opened = self.onboarding.open_brain_context(**self.common, expected_wake_id=self.wake['wake_id'])

    def call(self, action, arguments=None, *, execution_ref=None, include_ref=False):
        envelope = {'action': action, 'arguments': arguments or {}}
        if include_ref:
            envelope['execution_ref'] = execution_ref
        return self.raw_call('stbrain_manage', envelope)

    def raw_call(self, name, args):
        async def run():
            with patch('socket.socket.connect', side_effect=AssertionError('network_forbidden')):
                return await self.mcp.call_tool(name, args)
        blocks, structured = asyncio.run(run())
        self.assertEqual(structured, json.loads(blocks[0].text))
        return structured

    def issue(self, action, args):
        self.sequence += 1
        batch = {**self.common, 'wake_id': self.wake['wake_id'], 'wake_capability': self.wake['wake_capability'],
                 'batch_id': f'synthetic-compact-batch-{self.sequence}', 'revision': 1}
        result = self.store.issue_batch(**batch, calls=[{
            'call_id': f'synthetic-compact-call-{self.sequence}', 'canonical_tool': action,
            'advertised_name': 'stbrain_manage', 'schema_hash': self.entries['stbrain_manage']['schema_hash'],
            'catalog_hash': self.catalog['catalog_hash'], 'arguments_hash': canonical_hash(args)}])
        return result['executions'][0]['execution_ref'], batch

    def direct_context(self, password):
        verifier = self.directory / 'synthetic-verifier.json'
        verifier.write_text(json.dumps(encode_password('synthetic-password', salt=b'0123456789abcdef')), encoding='utf-8')
        self.authority.path = verifier
        if password:
            grant = self.authority.issue('synthetic-password', onboarding=self.onboarding,
                client_principal='synthetic-client', **self.common)
        else:
            grant = self.onboarding.issue_direct_grant(**self.common, actor_id='synthetic-human',
                client_principal='synthetic-client', request_id='synthetic-direct', requested_scopes=['self_revision'])
        return self.onboarding.open_brain_context(**self.common, direct_grant_ref=grant['grant_ref'],
            direct_client_principal='synthetic-client')

    def test_hidden_self_writes_use_exact_inner_claim_and_no_password(self):
        with patch.object(self.authority, 'allowed', side_effect=AssertionError('gateway_does_not_need_password')):
            for action in ('submit_self_model_candidate', 'activate_self_model_candidate'):
                args = {'write_context_ref': self.opened['write_context_ref']}
                ref, batch = self.issue(action, args)
                result = self.call(action, args, execution_ref=ref, include_ref=True)
                self.assertEqual('business_reached', result['decision'])
                self.assertTrue(result['bound'])
                self.assertEqual(action, self.calls[-1][1].tool_name)
                self.assertEqual(1, self.store.batch_status(**batch)['counts']['completed'])
                self.assertIsNone(current_execution_claim())
        self.assertFalse(self.authority.allowed())

    def test_action_drift_argument_drift_and_replay_cannot_mutate(self):
        args = {'intent': 'submit'}
        ref, batch = self.issue('submit_self_model_candidate', args)
        for action, changed in [('activate_self_model_candidate', args), ('submit_self_model_candidate', {'intent': 'changed'})]:
            self.assertEqual('reject', self.call(action, changed, execution_ref=ref, include_ref=True)['decision'])
        self.assertEqual([], self.calls)
        self.assertEqual(1, self.store.batch_status(**batch)['counts']['issued'])
        self.assertEqual('business_reached', self.call('submit_self_model_candidate', args, execution_ref=ref, include_ref=True)['decision'])
        self.assertEqual('reject', self.call('submit_self_model_candidate', args, execution_ref=ref, include_ref=True)['decision'])
        self.assertEqual(1, len(self.calls))

    def test_missing_fabricated_null_bindings_do_not_unlock_self(self):
        self.assertEqual('reject', self.call('submit_self_model_candidate')['decision'])
        for ref in (None, '', 'stexec_' + 'A' * 43, 123):
            self.assertEqual('reject', self.call('submit_self_model_candidate', execution_ref=ref, include_ref=True)['decision'])
        self.assertEqual([], self.calls)

    def test_direct_module_one_still_needs_password_and_direct_context(self):
        opened = self.direct_context(False)
        args = {'write_context_ref': opened['write_context_ref']}
        self.assertEqual('self_password_required', self.call('submit_self_model_candidate', args)['reason_code'])
        self.authority.authorize('synthetic-password')
        self.assertEqual('business_reached', self.call('submit_self_model_candidate', args)['decision'])
        self.authority.revoke()
        self.assertEqual('self_password_required', self.call('submit_self_model_candidate', args)['reason_code'])

    def test_real_password_direct_flow_stays_unbound(self):
        opened = self.direct_context(True)
        for action in ('submit_self_model_candidate', 'activate_self_model_candidate'):
            result = self.call(action, {'write_context_ref': opened['write_context_ref']})
            self.assertEqual('business_reached', result['decision'])
            self.assertFalse(result['bound'])

    def test_unknown_nested_or_extra_envelope_is_not_an_executor(self):
        for envelope in ({'action': 'subprocess.Popen', 'arguments': {}},
                         {'action': 'stbrain_manage', 'arguments': {}},
                         {'action': 'submit_self_model_candidate', 'arguments': [],},
                         {'action': 'submit_self_model_candidate', 'arguments': {'execution_ref': 'fake'}},
                         {'action': 'stbrain_health', 'arguments': {}, 'sudo': True}):
            self.assertEqual('reject', self.raw_call('stbrain_manage', envelope)['decision'])
        self.assertEqual([], self.calls)

    def test_nonexecution_target_does_not_discard_supplied_reference(self):
        self.assertEqual('compact_execution_ref_unexpected', self.call('stbrain_health', execution_ref='fake', include_ref=True)['reason_code'])
        self.assertEqual([], self.calls)
        self.assertEqual('healthy', self.call('stbrain_health')['decision'])

    def test_revoked_lease_cannot_be_reused_through_facade(self):
        ref, batch = self.issue('submit_self_model_candidate', {})
        self.store.revoke_batch(**batch)
        self.assertEqual('reject', self.call('submit_self_model_candidate', execution_ref=ref, include_ref=True)['decision'])
        self.assertEqual([], self.calls)

    def test_discovery_and_read_only_self_do_not_require_writes(self):
        first = self.raw_call('stbrain_tools', {})
        self.assertFalse(first['state_changed'])
        self.assertIn('self', [item['category'] for item in first['categories']])
        self.assertIn('submit_self_model_candidate', self.raw_call('stbrain_tools', {'category': 'self'})['actions'])
        one = self.raw_call('stbrain_tools', {'action': 'submit_self_model_candidate'})
        self.assertNotIn('execution_ref', one['arguments_schema']['properties'])
        self.assertFalse(one['manual_required_before_call'])
        self.assertEqual('read_available', self.call('query_self_model')['decision'])
        self.assertEqual([], self.calls)

    def test_inner_type_constraints_remain_before_business(self):
        self.assertEqual('compact_action_arguments_invalid', self.call('submit_self_model_candidate', {'intent': []})['reason_code'])
        self.assertEqual([], self.calls)

    def protocol_call(self, envelope):
        request = types.CallToolRequest.model_validate({'method': 'tools/call',
            'params': {'name': 'stbrain_manage', 'arguments': envelope}})
        result = asyncio.run(self.mcp._mcp_server.request_handlers[types.CallToolRequest](request))
        self.assertFalse(result.root.isError)
        self.assertEqual(result.root.structuredContent, json.loads(result.root.content[0].text))
        return result.root.structuredContent

    def test_real_sdk_cold_cache_and_protocol_handler_keep_inner_gateway_claim(self):
        self.mcp._mcp_server._tool_cache.clear()
        inner = {'intent': 'submit'}
        ref, _ = self.issue('submit_self_model_candidate', inner)
        result = self.protocol_call({'action': 'submit_self_model_candidate',
                                     'arguments': inner, 'execution_ref': ref})
        self.assertEqual('business_reached', result['decision'])
        self.assertTrue(result['bound'])
        self.assertIn('stbrain_manage', self.mcp._mcp_server._tool_cache)

    def test_real_sdk_free_form_inner_payload_still_passes_explicit_inner_validation(self):
        result = self.protocol_call({'action': 'submit_self_model_candidate', 'arguments': {'intent': []}})
        self.assertEqual('compact_action_arguments_invalid', result['reason_code'])
        self.assertEqual([], self.calls)

    def test_unexpected_inner_property_has_no_business_effect_and_does_not_consume_lease(self):
        inner = {'intent': 'submit', 'synthetic_unknown_field': 'synthetic-value'}
        ref, batch = self.issue('submit_self_model_candidate', inner)
        result = self.protocol_call({'action': 'submit_self_model_candidate', 'arguments': inner,
                                     'execution_ref': ref})
        self.assertEqual('compact_action_arguments_invalid', result['reason_code'])
        self.assertFalse(result['state_changed'])
        self.assertNotIn('synthetic_unknown_field', json.dumps(result))
        self.assertNotIn('synthetic-value', json.dumps(result))
        self.assertEqual([], self.calls)
        self.assertEqual(1, self.store.batch_status(**batch)['counts']['issued'])

    def test_discovered_save_schema_has_current_optional_prewrite_advisory(self):
        initial = self.raw_call('stbrain_tools', {'action': 'remember_memory'})
        self.assertIn('存入前·可选提醒', initial['description'])
        self.assertIn('合成默认人称提醒', initial['description'])
        self.assertIn('原词', initial['description'])
        self.advisory = {'enabled': True, 'message': '我的合成人称自选提醒\n保留这一行'}
        authored = self.raw_call('stbrain_tools', {'action': 'remember_memory'})
        self.assertIn(self.advisory['message'], authored['description'])
        self.assertNotIn('合成默认人称提醒', authored['description'])
        self.advisory = {'enabled': False, 'message': 'CLOSED_BODY_MUST_NOT_LEAK'}
        disabled = self.raw_call('stbrain_tools', {'action': 'remember_memory'})
        self.assertIn('人称提醒已关闭', disabled['description'])
        self.assertNotIn('CLOSED_BODY_MUST_NOT_LEAK', disabled['description'])
        self.assertIn('原词', disabled['description'])
        self.assertIn('合成默认人称提醒', initial['description'])
        self.assertEqual([], self.calls)

    def test_rejected_inner_args_complete_signed_continuation_and_close_without_hang(self):
        case = self

        class RealExecutionControl(ExecutionControl):
            actual_wake = None

            def post(self, path, payload):
                if path.startswith('/v1/host/tool-executions/'):
                    self.calls.append((path, deepcopy(payload)))
                    arguments = {key: value for key, value in payload.items() if key != 'deployment_epoch'}
                    if path.endswith('/issue'):
                        return case.store.issue_batch(**case.common, **arguments)
                    if path.endswith('/revoke'):
                        return case.store.revoke_batch(**case.common, **arguments)
                    return case.store.batch_status(**case.common, **arguments)
                if path == '/v1/host/context/close' and payload.get('wake_id') == self.actual_wake:
                    self.calls.append((path, deepcopy(payload)))
                    return case.onboarding.close_context_snapshot(**case.common, **payload)
                return super().post(path, payload)

        control = RealExecutionControl()
        app = GatewayApplication(replace(config(), require_execution_binding=True,
            execution_epoch='synthetic-compact'), control=control, upstream=object())
        tool = {'type': 'function', 'function': {'name': 'stbrain_manage',
                'parameters': deepcopy(self.mcp._tool_manager.get_tool('stbrain_manage').parameters)}}
        user = {'role': 'user', 'content': 'synthetic compact rejected-call continuation'}
        payload = {'messages': [user], 'tools': [tool]}
        headers = {'X-ST-Thread-ID': 'synthetic-compact-registry-thread'}
        prepared = app.prepare_turn(payload, headers)
        self.addCleanup(app._cancel_recovery_timer, prepared.session)
        self.onboarding.close_context_snapshot(**self.common, wake_id=self.wake['wake_id'],
                                               wake_capability=self.wake['wake_capability'])
        wake = self.onboarding.issue_wake(**self.common, host_id='synthetic-host',
            thread_id='synthetic-thread', source_kind='human_message', source_event_id='synthetic-facade-round')
        context = self.onboarding.build_pre_generation_context(**self.common, wake_id=wake['wake_id'],
            wake_capability=wake['wake_capability'], source_digest='synthetic', host_contract_digest='synthetic',
            advertised_tools=prepared.session.advertised_tools)
        self.onboarding.confirm_context_injected(**self.common, wake_id=wake['wake_id'],
            wake_capability=wake['wake_capability'], context_hash=context['context_hash'])
        prepared.session.wake_id = wake['wake_id']
        prepared.session.wake_capability = wake['wake_capability']
        control.actual_wake = wake['wake_id']

        envelope = {'action': 'submit_self_model_candidate',
                    'arguments': {'intent': 'submit', 'synthetic_unknown_field': 'synthetic-value'}}
        authored = NativeToolCall('synthetic-failed-inner', 'stbrain_manage', json.dumps(envelope))
        decorated = app.decorate_execution_calls(prepared, [authored])
        bindings = app.bind_response_tool_calls(prepared, decorated)
        app.finish_turn(prepared, keep_for_tools=True, tool_call_ids=['synthetic-failed-inner'], tool_call_bindings=bindings)
        app.mark_response_delivered(prepared)
        rejected = self.raw_call('stbrain_manage', json.loads(decorated[0].arguments_text))
        self.assertEqual('compact_action_arguments_invalid', rejected['reason_code'])
        batch = next(value for path, value in control.calls if path.endswith('/issue'))
        status_args = {key: value for key, value in batch.items() if key not in {'calls', 'deployment_epoch'}}
        self.assertEqual(1, self.store.batch_status(**self.common, **status_args)['counts']['issued'])
        call = {'id': decorated[0].tool_call_id, 'type': 'function', 'function': {
            'name': decorated[0].tool_name, 'arguments': decorated[0].arguments_text}}
        continued = app.prepare_turn({'tools': [tool], 'messages': [user,
            {'role': 'assistant', 'content': 'synthetic attempt', 'tool_calls': [call]},
            {'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(rejected)}]}, headers)
        self.assertTrue(continued.continuation)
        self.assertFalse(continued.session.expected_tool_call_ids)
        self.assertIsNone(continued.session.tool_wait_armed_at)
        app.finish_turn(continued, keep_for_tools=False)
        self.assertIsNone(app._current_session)
        newer = self.onboarding.issue_wake(**self.common, host_id='synthetic-host', thread_id='synthetic-thread',
            source_kind='human_message', source_event_id='synthetic-after-rejected-call')
        self.assertNotEqual(wake['wake_id'], newer['wake_id'])
        replay = self.raw_call('stbrain_manage', json.loads(decorated[0].arguments_text))
        self.assertEqual('reject', replay['decision'])
        self.assertEqual([], self.calls)


class CompactGatewayProjectionTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket', 'socket.create_connection', 'sqlite3.connect'):
            self.enterContext(patch(target, side_effect=AssertionError('external_access_forbidden')))
        self.control = ExecutionControl()
        self.app = GatewayApplication(replace(config(), require_execution_binding=True,
            execution_epoch='synthetic-compact'), control=self.control, upstream=object())
        self.public = 'mcp__StillerBrain__stbrain_manage'
        self.tool = {'type': 'function', 'function': {'name': self.public, 'parameters': {
            'type': 'object', 'required': ['action'], 'additionalProperties': False,
            'properties': {'action': {'type': 'string'}, 'arguments': {'type': 'object'},
                'execution_ref': {'type': 'string', 'x-stbrain-execution-tool': 'stbrain_manage',
                    'x-stbrain-execution-contract': 'st-execution/1'}}}}}
        self.payload = {'messages': [{'role': 'user', 'content': 'synthetic'}], 'tools': [self.tool]}
        self.prepared = self.app.prepare_turn(self.payload, {'X-ST-Thread-ID': 'synthetic-compact-thread'})
        self.addCleanup(self.app._cancel_recovery_timer, self.prepared.session)

    def test_gateway_lease_binds_inner_while_wire_keeps_outer_envelope(self):
        envelope = {'action': 'submit_self_model_candidate', 'arguments': {'intent': 'submit'}}
        text = json.dumps(envelope)
        call = NativeToolCall('synthetic-compact-call', self.public, text)
        decorated = self.app.decorate_execution_calls(self.prepared, [call])
        issued = [value for path, value in self.control.calls if path.endswith('/issue')][-1]['calls'][0]
        self.assertEqual('submit_self_model_candidate', issued['canonical_tool'])
        self.assertEqual(self.public, issued['advertised_name'])
        self.assertEqual(canonical_hash(envelope['arguments']), issued['arguments_hash'])
        self.assertEqual(canonical_hash(self.tool['function']['parameters']), issued['schema_hash'])
        self.assertEqual(self.public, decorated[0].tool_name)
        delivered = json.loads(decorated[0].arguments_text)
        ref = delivered.pop('execution_ref')
        self.assertTrue(ref.startswith('stexec_'))
        self.assertEqual(envelope, delivered)
        self.assertEqual(text, call.arguments_text)
        bindings = self.app.bind_response_tool_calls(self.prepared, decorated)
        self.assertEqual(canonical_hash(json.loads(decorated[0].arguments_text)), bindings['synthetic-compact-call'].arguments_hash)

    def test_nonexecution_targets_get_no_spurious_lease(self):
        call = NativeToolCall('synthetic-health', self.public, '{"action":"stbrain_health"}')
        self.assertEqual([call], self.app.decorate_execution_calls(self.prepared, [call]))
        self.assertFalse(any(path.endswith('/issue') for path, _ in self.control.calls))

    def test_invalid_routes_and_reserved_inner_ref_never_issue(self):
        for envelope in ({'action': 'os.system'}, {'action': 'stbrain_manage'},
                         {'action': 'submit_self_model_candidate', 'arguments': {'execution_ref': 'fake'}},
                         {'action': 'stbrain_health', 'extra': True}):
            with self.assertRaises(GatewayError):
                self.app.decorate_execution_calls(self.prepared, [NativeToolCall('synthetic-invalid', self.public, json.dumps(envelope))])
        self.assertFalse(any(path.endswith('/issue') for path, _ in self.control.calls))

    def test_upstream_projection_hides_host_ref_not_action_or_arguments(self):
        source = deepcopy(self.payload)
        projected = json.loads(self.app.encode_upstream_payload(source))
        self.assertEqual(self.payload, source)
        fields = projected['tools'][0]['function']['parameters']['properties']
        self.assertNotIn('execution_ref', fields)
        self.assertIn('action', fields)
        self.assertIn('arguments', fields)

    def test_route_mapping_has_no_recursive_facade_or_external_executor(self):
        self.assertNotIn('stbrain_manage', ACTION_CATEGORIES)
        self.assertNotIn('stbrain_tools', ACTION_CATEGORIES)
        for name in ('os.system', 'subprocess.Popen', 'http://example.invalid'):
            with self.assertRaises(CompactRouteError):
                resolve_compact_action({'action': name, 'arguments': {}})


if __name__ == '__main__':
    unittest.main()
