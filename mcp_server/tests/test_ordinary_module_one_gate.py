"""Read-first ordinary authoring policy: real temp activation/lease records.

Only business tool bodies are replaced by observation functions. The policy,
execution authentication and initial/replacement module-one transitions are real.
"""
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
import unittest
from unittest.mock import patch

from mcp.server.fastmcp import FastMCP
from mcp_server.execution_guard import install_execution_guard
from mcp_server.ordinary_access_policy import install_ordinary_access_policy
from runtime.execution_binding import ExecutionStore, canonical_hash, current_execution_claim
from runtime.ordinary_access import (
    ORDINARY_READ_TOOLS, ORDINARY_TOOLS, current_ordinary_access, ordinary_write_scope,
)
from tests import test_onboarding as fixtures


class OrdinaryModuleOneGateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ModuleOneOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.onboarding = self.fixture.store
        self.database = self.fixture.database
        self.common = {'owner_id': self.fixture.owner, 'model_id': self.fixture.model}
        self.store = ExecutionStore(self.database, deployment_epoch='synthetic-read-first',
            capability_secret=b'module-one-test-secret-32-bytes-minimum!!')
        self.calls = []
        self.mcp, self.policy_dispatch = self.build_dispatch()
        self.sequence = 0
        self.enterContext(patch('socket.create_connection', side_effect=AssertionError('network_forbidden')))
        self.enterContext(patch('subprocess.Popen', side_effect=AssertionError('process_forbidden')))

    def build_dispatch(self, **identity):
        mcp = FastMCP('synthetic-read-first')
        common = identity or self.common
        for name in set(ORDINARY_TOOLS) | {
            'remember_memory', 'revise_memory', 'submit_self_model_candidate',
            'activate_self_model_candidate', 'hold_hallucination_record',
        }:
            def body(tool_name):
                async def observed(module: str | None = None, target_ref: str | None = None,
                    scope: str | None = None, action: str | None = None,
                    write_context_ref: str | None = None, expected_learning_version: int | None = None,
                    payload: dict[str, Any] | None = None) -> dict[str, Any]:
                    access = current_ordinary_access(**common)
                    self.calls.append(tool_name)
                    return {'decision': 'business_reached', 'ordinary': access is not None,
                            'bound': current_execution_claim() is not None}
                return observed
            mcp.tool(name=name)(body(name))
        install_ordinary_access_policy(mcp, onboarding=self.onboarding, services={}, **common)
        policy = mcp._tool_manager.call_tool
        install_execution_guard(mcp, store=self.store, onboarding=self.onboarding,
            ordinary_authenticated=True, **common)
        return mcp, policy

    @staticmethod
    def write_cases():
        cases = [(name, {}) for name in sorted(set(ORDINARY_TOOLS) - ORDINARY_READ_TOOLS)]
        cases += [('remember_memory', {'module': module})
                  for module in ('emotional_memory', 'learning_memory', 'planning_memory')]
        cases += [('revise_memory', {'target_ref': prefix + '://synthetic@1'})
                  for prefix in ('emotion', 'learning', 'plan')]
        for name in ('manage_self_governance_profile', 'manage_injection_control'):
            cases.extend((name, {'action': action, 'scope': 'self_model' if 'injection' in name else 'global'})
                         for action in ('set', 'clear', 'propose_set', 'propose_clear',
                                        'propose_mode', 'propose_rollback', 'activate', 'withdraw'))
        return cases

    def run_async(self, coroutine):
        async def blocked():
            with patch('socket.socket.connect', side_effect=AssertionError('network_forbidden')):
                return await coroutine
        return asyncio.run(blocked())

    def call(self, name, args, *, policy=False):
        if policy:
            return self.run_async(self.policy_dispatch(name, args))
        blocks, result = self.run_async(self.mcp.call_tool(name, args))
        self.assertEqual(result, json.loads(blocks[0].text))
        return result

    def business_state(self):
        with closing(sqlite3.connect(self.database)) as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
                if row[0] not in {'brain_execution_calls', 'brain_execution_batches'}]
            return canonical_hash({table: connection.execute('SELECT * FROM "' + table + '"').fetchall()
                                   for table in tables})

    def prepare_gateway(self):
        self.sequence += 1
        entries = [{'canonical_name': t.name, 'schema_hash': canonical_hash(t.parameters)}
                   for t in self.mcp._tool_manager.list_tools()]
        self.entries = {entry['canonical_name']: entry for entry in entries}
        self.catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                        'catalog_hash': canonical_hash(entries), 'entries': entries}
        self.wake = self.onboarding.issue_wake(**self.common, host_id='synthetic-host',
            thread_id='synthetic-thread', source_kind='human_message', source_event_id=f'synthetic-{self.sequence}')
        prepared = self.onboarding.build_pre_generation_context(**self.common,
            wake_id=self.wake['wake_id'], wake_capability=self.wake['wake_capability'],
            source_digest='synthetic-source', host_contract_digest='synthetic-contract',
            advertised_tools=self.catalog)
        self.onboarding.confirm_context_injected(**self.common, wake_id=self.wake['wake_id'],
            wake_capability=self.wake['wake_capability'], context_hash=prepared['context_hash'])

    def signed(self, name, args):
        self.sequence += 1
        batch = {**self.common, 'wake_id': self.wake['wake_id'],
            'wake_capability': self.wake['wake_capability'], 'batch_id': f'synthetic-{self.sequence}', 'revision': 1}
        ref = self.store.issue_batch(**batch, calls=[{
            'call_id': f'synthetic-call-{self.sequence}', 'canonical_tool': name, 'advertised_name': name,
            'schema_hash': self.entries[name]['schema_hash'], 'catalog_hash': self.catalog['catalog_hash'],
            'arguments_hash': canonical_hash(args)}])['executions'][0]['execution_ref']
        result = self.call(name, {**args, 'execution_ref': ref})
        self.assertIsNone(current_execution_claim())
        return result, batch

    def assert_locked(self, result):
        self.assertEqual('reject', result['decision'])
        self.assertEqual('module_one_required', result['reason_code'])
        self.assertFalse(result['state_changed'])
        self.assertIn('只读', result['message'])
        self.assertIn('激活', result['next_action'])
        self.assertIsNone(current_ordinary_access(**self.common))

    def test_every_classified_write_blocks_before_context_or_any_fresh_db_initialization(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with patch('mcp_server.ordinary_access_policy.authenticated_ordinary_operation',
                   side_effect=AssertionError('blocked_write_created_context')):
            for name, args in self.write_cases():
                with self.subTest(tool=name, action=args.get('action')):
                    self.assertIsNotNone(ordinary_write_scope(name, args))
                    # Includes advanced legacy-bound actions at the inner policy
                    # boundary; their outer execution requirements remain intact.
                    self.assert_locked(self.call(name, args, policy=True))
        self.assertEqual([], self.calls)
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_official_ordinary_and_valid_gateway_writes_share_locked_policy(self):
        self.prepare_gateway()
        before = self.business_state()
        for name, args in self.write_cases():
            with self.subTest(tool=name, action=args.get('action')):
                result, batch = self.signed(name, args)
                self.assert_locked(result)
                self.assertEqual(1, self.store.batch_status(**batch)['counts']['failed'])
        for name, args in (('remember_memory', {'module': 'learning_memory'}),
                           ('remember_planning_memory', {}), ('preview_person_reference_rewrite', {})):
            self.assert_locked(self.call(name, args))
        self.assertEqual(before, self.business_state())
        self.assertEqual([], self.calls)

    def test_all_read_operations_stay_available_without_module_one(self):
        for name in ORDINARY_READ_TOOLS:
            result = self.call(name, {})
            self.assertEqual('business_reached', result['decision'])
        self.assertEqual(len(ORDINARY_READ_TOOLS), len(self.calls))

    def test_module_one_and_vault_keep_their_separate_authorization_paths(self):
        for name, args in (('submit_self_model_candidate', {}), ('activate_self_model_candidate', {}),
                           ('hold_hallucination_record', {}),
                           ('manage_injection_control', {'scope': 'hallucination_vault', 'action': 'activate'})):
            self.assertIsNone(ordinary_write_scope(name, args))
            self.assertEqual('business_reached', self.call(name, args, policy=True)['decision'])
        # This checks delegation only, never claims the outer vault/self guard
        # grants authorization. Their real dispatcher tests remain independent.

    def test_initial_candidate_and_accepted_review_stay_read_only_until_activation(self):
        self.fixture.bootstrap_to_wait()
        self.assert_locked(self.call('remember_memory', {'module': 'emotional_memory'}))
        wake, _ = self.fixture.wake('synthetic-review')
        self.fixture.open_brain()
        reviewed = self.fixture.advance(wake, 'accept_candidate_review', {'ai_confirmation': True})
        self.assertEqual('review_accepted', reviewed['decision'])
        self.assert_locked(self.call('remember_memory', {'module': 'emotional_memory'}))
        self.assertEqual([], self.calls)

    def test_real_first_activation_opens_all_ordinary_writes_on_both_paths(self):
        self.fixture.bootstrap_live()
        for name, args in self.write_cases():
            self.assertEqual('business_reached', self.call(name, args, policy=True)['decision'])
        self.prepare_gateway()
        for name, args in self.write_cases():
            result, batch = self.signed(name, args)
            self.assertEqual('business_reached', result['decision'])
            self.assertEqual(1, self.store.batch_status(**batch)['counts']['completed'])
        self.assertEqual('business_reached', self.call('remember_memory', {'module': 'planning_memory'})['decision'])

    def test_new_self_candidate_keeps_the_established_active_baseline_unlocked(self):
        revision = self.fixture.bootstrap_live()
        wake, _ = self.fixture.wake('synthetic-edit')
        challenge = self.fixture.advance(wake, 'begin_edit')
        self.assertEqual('challenge_issued', challenge['decision'])
        self.assertEqual('business_reached', self.call('remember_memory', {'module': 'learning_memory'})['decision'])
        confirmed = self.fixture.advance(wake, 'confirm_edit', {
            'challenge_id': challenge['challenge_id'], 'challenge_response': challenge['challenge_response']})
        self.assertEqual('confirmed', confirmed['decision'])
        submitted = self.fixture.advance(wake, 'submit_candidate', self.fixture.candidate_payload('v2', expected=revision))
        self.assertEqual('pending', submitted['decision'])
        self.assertEqual('business_reached', self.call('remember_memory', {'module': 'learning_memory'})['decision'])

    def test_request_flags_and_another_owners_activation_are_not_unlock_evidence(self):
        self.assert_locked(self.call('remember_memory', {
            'module': 'learning_memory', 'module_one_unlocked': True,
            'model_id': 'deepseek-v4-flash-vision-exp', 'context_mode': 'gateway_execution'}))
        self.fixture.bootstrap_live()
        _, other_policy = self.build_dispatch(owner_id='synthetic-other-owner', model_id=self.fixture.model)
        self.assert_locked(self.run_async(other_policy('remember_memory', {'module': 'learning_memory'})))

    def test_unlocked_bit_without_active_revision_and_mismatched_basis_fail_closed(self):
        self.onboarding.ensure_state(**self.common)
        with self.store._connect() as connection:
            connection.execute("UPDATE brain_module_unlocks SET unlocked=1 WHERE owner_id=? AND model_id=?", tuple(self.common.values()))
        self.assert_locked(self.call('remember_memory', {'module': 'learning_memory'}))
        self.fixture.bootstrap_live()
        with self.store._connect() as connection:
            connection.execute("UPDATE brain_module_unlocks SET basis_revision_id='synthetic-wrong' WHERE owner_id=? AND model_id=?", tuple(self.common.values()))
        self.assert_locked(self.call('remember_memory', {'module': 'learning_memory'}))


if __name__ == '__main__':
    unittest.main()
