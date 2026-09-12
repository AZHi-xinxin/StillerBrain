"""Password/gateway boundary with real leases and only synthetic temporary data.

Business functions are deliberately tiny here; the separate registered-server
integration suite covers the complete module-one candidate/review workflow.
"""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from mcp_server.execution_guard import install_execution_guard
from mcp_server.self_password import (
    SelfPasswordAuthority, encode_password, install_self_password_guard,
)
from runtime.execution_binding import ExecutionStore, canonical_hash, current_execution_claim
from runtime.onboarding import ModuleOneOnboardingStore


class SelfPasswordGatewayGuardTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('network_forbidden')))
        self.enterContext(patch('subprocess.Popen', side_effect=AssertionError('process_forbidden')))
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix='synthetic-self-boundary-'))
        self.directory = Path(directory)
        self.common = {'owner_id': 'synthetic-owner', 'model_id': 'synthetic-model'}
        self.principal = 'synthetic-direct-client'
        self.database = self.directory / 'synthetic.sqlite3'
        secret = 'synthetic-execution-secret-at-least-32-bytes'
        self.onboarding = ModuleOneOnboardingStore(self.database, capability_secret=secret)
        self.store = ExecutionStore(self.database, deployment_epoch='synthetic-epoch', capability_secret=secret)
        self.authority = SelfPasswordAuthority(None)
        self.calls = []
        self.sequence = 0
        self.mcp, self.password_dispatch = self.build_dispatch()
        entries = [{'canonical_name': t.name, 'schema_hash': canonical_hash(t.parameters)}
                   for t in self.mcp._tool_manager.list_tools()]
        self.entries = {entry['canonical_name']: entry for entry in entries}
        self.catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                        'catalog_hash': canonical_hash(entries), 'entries': entries}
        self.wake = self.onboarding.issue_wake(**self.common, host_id='synthetic-host',
            thread_id='synthetic-thread', source_kind='human_message', source_event_id='synthetic-A')
        prepared = self.onboarding.build_pre_generation_context(**self.common,
            wake_id=self.wake['wake_id'], wake_capability=self.wake['wake_capability'],
            source_digest='synthetic-source', host_contract_digest='synthetic-contract',
            advertised_tools=self.catalog)
        self.onboarding.confirm_context_injected(**self.common, wake_id=self.wake['wake_id'],
            wake_capability=self.wake['wake_capability'], context_hash=prepared['context_hash'])
        self.gateway_context = self.onboarding.open_brain_context(
            **self.common, expected_wake_id=self.wake['wake_id'])
        self.assertTrue(self.gateway_context['write_context_available'])

    def build_dispatch(self, *, required=True, execution_store=True):
        mcp = FastMCP('synthetic-self-boundary')

        @mcp.tool()
        async def submit_self_model_candidate(
            write_context_ref: str | None = None, intent: str = 'submit', payload: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(('submit', current_execution_claim()))
            return {'decision': 'business_reached', 'bound': current_execution_claim() is not None, 'intent': intent}

        @mcp.tool()
        async def activate_self_model_candidate(
            write_context_ref: str | None = None, intent: str = 'activate', payload: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            self.calls.append(('activate', current_execution_claim()))
            return {'decision': 'business_reached', 'bound': current_execution_claim() is not None, 'intent': intent}

        @mcp.tool()
        async def query_self_model() -> dict[str, Any]:
            return {'decision': 'read_available'}

        install_self_password_guard(mcp, self.authority,
            execution_store=self.store if execution_store else None,
            onboarding=self.onboarding, **self.common)
        inner = mcp._tool_manager.call_tool
        if execution_store:
            install_execution_guard(mcp, store=self.store, onboarding=self.onboarding,
                required=required, ordinary_authenticated=True, **self.common)
        return mcp, inner

    def issue(self, name='submit_self_model_candidate', args=None):
        self.sequence += 1
        batch = {**self.common, 'wake_id': self.wake['wake_id'],
                 'wake_capability': self.wake['wake_capability'],
                 'batch_id': f'synthetic-batch-{self.sequence}', 'revision': 1}
        result = self.store.issue_batch(**batch, calls=[{
            'call_id': f'synthetic-call-{self.sequence}', 'canonical_tool': name, 'advertised_name': name,
            'schema_hash': self.entries[name]['schema_hash'], 'catalog_hash': self.catalog['catalog_hash'],
            'arguments_hash': canonical_hash(args or {})}])
        return result['executions'][0]['execution_ref'], batch

    def run_async(self, coroutine):
        async def blocked():
            # Windows creates its private event-loop socketpair before this
            # block; every connection attempted by the tested call is blocked.
            with patch('socket.socket.connect', side_effect=AssertionError('network_forbidden')):
                return await coroutine
        return asyncio.run(blocked())

    def call(self, name, args, *, mcp=None):
        blocks, structured = self.run_async((mcp or self.mcp).call_tool(name, args))
        self.assertEqual(structured, json.loads(blocks[0].text))
        return structured

    def configure_password(self):
        path = self.directory / 'synthetic-verifier.json'
        # Fixtures contain synthetic data only; no deployment config is loaded.
        path.write_text(json.dumps(encode_password('synthetic-password', salt=b'0123456789abcdef')), encoding='utf-8')
        self.authority.path = path

    def direct_context(self, *, password=False, scopes=None):
        if password:
            self.configure_password()
            grant = self.authority.issue('synthetic-password', onboarding=self.onboarding,
                client_principal=self.principal, **self.common)
            self.assertEqual('authorized', grant['decision'])
        else:
            grant = self.onboarding.issue_direct_grant(**self.common, actor_id='synthetic-human',
                client_principal=self.principal, request_id='synthetic-direct',
                requested_scopes=scopes or ['self_revision'])
        opened = self.onboarding.open_brain_context(**self.common, direct_grant_ref=grant['grant_ref'],
            direct_client_principal=self.principal)
        self.assertTrue(opened['write_context_available'])
        return opened

    def test_gateway_claims_skip_password_for_both_writes_and_do_not_authorize_direct(self):
        with patch.object(self.authority, 'allowed', side_effect=AssertionError('gateway_must_not_check_password')):
            for name in ('submit_self_model_candidate', 'activate_self_model_candidate'):
                args = {'write_context_ref': self.gateway_context['write_context_ref']}
                ref, batch = self.issue(name, args)
                result = self.call(name, {**args, 'execution_ref': ref})
                self.assertEqual('business_reached', result['decision'])
                self.assertTrue(result['bound'])
                self.assertEqual(1, self.store.batch_status(**batch)['counts']['completed'])
                self.assertIsNone(current_execution_claim())
        self.assertFalse(self.authority.allowed())
        self.assertEqual(0, self.authority.allowed_until)

    def test_unsigned_null_and_fabricated_refs_fail_before_business(self):
        args = {'write_context_ref': self.gateway_context['write_context_ref']}
        missing = self.call('submit_self_model_candidate', args)
        self.assertEqual('execution_binding_required', missing['reason_code'])
        self.assertIn('authorize_self_model', missing['next_action'])
        self.assertIn('stbrain_open_direct', missing['next_action'])
        for invalid in (None, '', 'stexec_' + 'A' * 43, 123, {'verified': True}):
            result = self.call('submit_self_model_candidate', {**args, 'execution_ref': invalid})
            self.assertEqual('execution_binding_invalid_or_finished', result['reason_code'])
        self.assertEqual([], self.calls)

    def test_exact_tool_arguments_and_single_use_are_preserved(self):
        args = {'write_context_ref': self.gateway_context['write_context_ref'], 'payload': {'text': 'synthetic'}}
        ref, batch = self.issue(args=args)
        for name, changed in (
            ('activate_self_model_candidate', args),
            ('submit_self_model_candidate', {**args, 'payload': {'text': 'changed'}}),
            ('submit_self_model_candidate', {**args, 'model_id': 'gateway'}),
        ):
            self.assertEqual('reject', self.call(name, {**changed, 'execution_ref': ref})['decision'])
        self.assertEqual([], self.calls)
        self.assertEqual(1, self.store.batch_status(**batch)['counts']['issued'])
        self.assertEqual('business_reached', self.call('submit_self_model_candidate', {**args, 'execution_ref': ref})['decision'])
        self.assertEqual('reject', self.call('submit_self_model_candidate', {**args, 'execution_ref': ref})['decision'])
        self.assertEqual(1, len(self.calls))

    def test_revoked_old_wake_cannot_gain_gateway_exemption(self):
        ref, batch = self.issue()
        self.store.revoke_batch(**batch)
        self.onboarding.close_context_snapshot(**self.common, wake_id=self.wake['wake_id'],
            wake_capability=self.wake['wake_capability'])
        self.onboarding.issue_wake(**self.common, host_id='synthetic-host', thread_id='synthetic-thread',
            source_kind='human_message', source_event_id='synthetic-B')
        self.assertEqual('reject', self.call('submit_self_model_candidate', {'execution_ref': ref})['decision'])
        self.assertEqual([], self.calls)

    def test_live_guard_rechecks_claim_identity_raw_hash_and_registry(self):
        args = {'write_context_ref': self.gateway_context['write_context_ref']}
        ref, _ = self.issue(args=args)
        claim = self.store.claim(execution_ref=ref, tool_name='submit_self_model_candidate', arguments=args, **self.common)
        try:
            for changes in ({'owner_id': 'other'}, {'model_id': 'other'}, {'wake_id': 'other'},
                            {'batch_id': 'other'}, {'call_id': 'other'}, {'tool_name': 'activate_self_model_candidate'},
                            {'claim_id': 'other'}, {'execution_ref': 'stexec_' + 'A' * 43}):
                with self.subTest(field=next(iter(changes))), self.store.bind(replace(claim, **changes)):
                    result = self.run_async(self.password_dispatch('submit_self_model_candidate', args))
                    self.assertEqual('execution_binding_invalid_or_finished', result['reason_code'])
            with self.store.bind(claim):
                result = self.run_async(self.password_dispatch('submit_self_model_candidate', {**args, 'intent': 'changed'}))
                self.assertEqual('execution_binding_invalid_or_finished', result['reason_code'])
                self.assertEqual('business_reached', self.run_async(self.password_dispatch('submit_self_model_candidate', args))['decision'])
        finally:
            self.store.finish(claim, failed=False)
        with self.store.bind(claim):
            self.assertEqual('execution_binding_invalid_or_finished',
                self.run_async(self.password_dispatch('submit_self_model_candidate', args))['reason_code'])
        self.assertEqual(1, len(self.calls))

    def test_live_guard_rechecks_wake_expiry_and_injection(self):
        args = {'write_context_ref': self.gateway_context['write_context_ref']}
        ref, _ = self.issue(args=args)
        claim = self.store.claim(execution_ref=ref, tool_name='submit_self_model_candidate', arguments=args, **self.common)
        try:
            # Invariant-corruption tests modify only this synthetic temp DB.
            with self.store._connect() as connection:
                connection.execute("UPDATE brain_wake_sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE wake_id=?", (claim.wake_id,))
            with self.store.bind(claim):
                self.assertEqual('execution_binding_invalid_or_finished',
                    self.run_async(self.password_dispatch('submit_self_model_candidate', args))['reason_code'])
            with self.store._connect() as connection:
                connection.execute("UPDATE brain_wake_sessions SET expires_at='2099-01-01T00:00:00+00:00',injected_at=NULL WHERE wake_id=?", (claim.wake_id,))
            with self.store.bind(claim):
                self.assertEqual('execution_binding_invalid_or_finished',
                    self.run_async(self.password_dispatch('submit_self_model_candidate', args))['reason_code'])
        finally:
            self.store.finish(claim, failed=True)
        self.assertEqual([], self.calls)

    def test_password_alone_never_converts_gateway_artifact_into_direct_authority(self):
        self.configure_password()
        self.assertEqual('authorized', self.authority.authorize('synthetic-password')['decision'])
        args = {'write_context_ref': self.gateway_context['write_context_ref']}
        self.assertEqual('execution_binding_required', self.call('submit_self_model_candidate', args)['reason_code'])
        optional, _ = self.build_dispatch(required=False)
        no_execution, _ = self.build_dispatch(required=False, execution_store=False)
        for mcp in (optional, no_execution):
            for attempt in (args, {}, {'write_context_ref': 'invented'}):
                self.assertEqual('direct_grant_required', self.call('submit_self_model_candidate', attempt, mcp=mcp)['reason_code'])
        self.assertEqual([], self.calls)

    def test_real_direct_grant_without_password_remains_denied(self):
        opened = self.direct_context()
        args = {'write_context_ref': opened['write_context_ref']}
        self.assertEqual('self_password_required', self.call('submit_self_model_candidate', args)['reason_code'])
        self.configure_password()
        self.assertEqual('reject', self.authority.authorize('wrong-synthetic-password')['decision'])
        self.assertEqual('self_password_required', self.call('submit_self_model_candidate', args)['reason_code'])
        self.assertEqual([], self.calls)

    def test_real_password_direct_grant_allows_both_writes_until_revoked(self):
        opened = self.direct_context(password=True)
        args = {'write_context_ref': opened['write_context_ref']}
        for name in ('submit_self_model_candidate', 'activate_self_model_candidate'):
            result = self.call(name, args)
            self.assertEqual('business_reached', result['decision'])
            self.assertFalse(result['bound'])
        self.authority.revoke()
        self.assertEqual('self_password_required', self.call('submit_self_model_candidate', args)['reason_code'])
        self.assertEqual(2, len(self.calls))

    def test_wrong_scope_direct_grant_does_not_authorize_self_write(self):
        opened = self.direct_context(scopes=['learning_memory'])
        self.configure_password()
        self.authority.authorize('synthetic-password')
        args = {'write_context_ref': opened['write_context_ref']}
        self.assertEqual('execution_binding_required', self.call('submit_self_model_candidate', args)['reason_code'])
        optional, _ = self.build_dispatch(required=False)
        self.assertEqual('direct_grant_required', self.call('submit_self_model_candidate', args, mcp=optional)['reason_code'])
        self.assertEqual([], self.calls)

    def test_plain_self_read_needs_neither_password_nor_gateway(self):
        self.assertEqual('read_available', self.call('query_self_model', {})['decision'])


if __name__ == '__main__':
    unittest.main()
