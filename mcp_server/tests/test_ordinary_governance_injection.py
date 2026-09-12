"""Synthetic current-author operations; no server, sockets, or production data."""
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from mcp_server.injection_control_service import InjectionControlAccessService
from mcp_server.tests.test_governance_service import GovernanceServiceTests
from runtime.ordinary_access import authenticated_ordinary_operation


class OrdinaryGovernanceInjectionTests(unittest.TestCase):
    setUp = GovernanceServiceTests.setUp
    tearDown = GovernanceServiceTests.tearDown
    wake = GovernanceServiceTests.wake
    bootstrap_live = GovernanceServiceTests.bootstrap_live
    advance = GovernanceServiceTests.advance

    def rows(self, table):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute('SELECT * FROM ' + table).fetchall()

    def injection(self):
        return InjectionControlAccessService(self.onboarding.injection_control_store,
            onboarding=self.onboarding, owner_id=self.owner, model_id=self.model)

    def context(self, scope, **overrides):
        return authenticated_ordinary_operation(owner_id=overrides.get('owner_id', self.owner),
            model_id=overrides.get('model_id', self.model), scope=scope)

    def gov_set(self, access, **overrides):
        args = dict(action='set', scope='global', write_context_ref=access['write_context_ref'],
            expected_profile_version=0, text='该 AI 选择自己的表达方式。',
            trigger_mode='scene_relevant', scene_tags=['test-scene'], expected_active_revision=None)
        args.update(overrides)
        return self.service.governance.manage(**args)

    def inj_set(self, access, **overrides):
        args = dict(action='set', scope='self_model', write_context_ref=access['write_context_ref'],
            expected_control_version=0, target_mode='paused', expected_active_revision=None)
        args.update(overrides)
        return self.injection().manage(**args)

    def test_new_profile_two_separate_governance_operations_need_no_wake(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')):
            with self.context('self_governance') as first_access:
                first = self.gov_set(first_access)
            with self.context('self_governance') as second_access:
                second = self.gov_set(second_access, text='第二次仍由这个 AI 自主决定。',
                    expected_profile_version=1, expected_active_revision=first['revision_id'])
        self.assertEqual('committed', first['decision'])
        self.assertEqual('committed', second['decision'])
        self.assertNotEqual(first_access['wake_id'], second_access['wake_id'])
        self.assertTrue(first_access['wake_id'].startswith('ordinaryop_'))
        self.assertEqual([], self.rows('brain_wake_sessions'))
        self.assertEqual([], self.rows('brain_context_snapshots'))
        self.assertEqual([], self.rows('self_governance_candidates'))
        self.assertEqual(2, len(self.rows('self_governance_revisions')))

    def test_new_profile_two_separate_injection_operations_do_not_write_self_definition(self):
        with self.context('injection_control') as access:
            first = self.inj_set(access)
        with self.context('injection_control') as access:
            second = self.inj_set(access, target_mode='enabled', expected_control_version=1,
                expected_active_revision=first['revision_id'])
        self.assertEqual('committed', second['decision'])
        self.assertEqual([], self.rows('brain_wake_sessions'))
        self.assertEqual([], self.rows('injection_control_candidates'))
        self.assertFalse(second['review_performed'])
        self.assertFalse(second['external_permission_changed'])
        self.assertIsNone(self.service.store.active_revision(self.model))

    def test_scope_owner_wrong_ref_and_stale_active_do_not_write(self):
        with self.context('learning_memory') as access:
            self.assertEqual('rejected', self.gov_set(access)['decision'])
        with self.context('self_governance', owner_id='another-owner') as access:
            self.assertEqual('rejected', self.gov_set(access)['decision'])
        with self.context('self_governance') as access:
            wrong = self.gov_set(access, write_context_ref='ordinaryctx_wrong')
            self.assertEqual(['brain_open_required'], wrong['reason_codes'])
            first = self.gov_set(access)
        with self.context('self_governance') as access:
            wrong = self.gov_set(access, expected_profile_version=1)
            self.assertEqual(['active_governance_revision_conflict'], wrong['reason_codes'])
        self.assertEqual(1, len(self.rows('self_governance_revisions')))

    def test_vault_every_action_refuses_ordinary_authorization(self):
        for action in ('set', 'clear', 'rollback', 'propose_mode', 'propose_rollback', 'activate', 'withdraw'):
            with self.subTest(action=action), self.context('injection_control') as access:
                result = self.inj_set(access, action=action, scope='hallucination_vault', ai_confirmation=True)
                self.assertEqual(['vault_control_requires_legacy_authorization'], result['reason_codes'])
        self.assertEqual([], self.rows('injection_control_revisions'))
        self.assertEqual([], self.rows('injection_control_candidates'))

    def test_legacy_review_cannot_treat_an_ordinary_operation_as_a_real_wake(self):
        with self.context('self_governance') as access:
            result = self.gov_set(access, action='propose_set')
            self.assertEqual(['legacy_candidate_requires_open_wake'], result['reason_codes'])
        with self.context('injection_control') as access:
            result = self.inj_set(access, action='activate', ai_confirmation=True)
            self.assertEqual(['legacy_candidate_requires_open_wake'], result['reason_codes'])
        self.assertEqual([], self.rows('brain_wake_sessions'))

    def test_injected_snapshot_is_unchanged_after_both_direct_revisions(self):
        self.bootstrap_live()
        wake, prepared = self.wake('snapshot-current', query_text='test-scene')
        original = self.rows('brain_context_snapshots')
        with self.context('self_governance') as access:
            self.assertEqual('committed', self.gov_set(access)['decision'])
        with self.context('injection_control') as access:
            self.assertEqual('committed', self.inj_set(access, scope='global', target_mode='hard_off')['decision'])
        repeated = self.onboarding.build_pre_generation_context(
            owner_id=self.owner, model_id=self.model, wake_id=wake['wake_id'],
            wake_capability=wake['wake_capability'], source_digest='source:snapshot-current',
            host_contract_digest='host-contract:v1', source_frame={'query_text': 'test-scene'})
        self.assertEqual(prepared['context_hash'], repeated['context_hash'])
        self.assertEqual(original, self.rows('brain_context_snapshots'))
        _, following = self.wake('snapshot-next', query_text='test-scene')
        self.assertNotEqual(prepared['context_hash'], following['context_hash'])


if __name__ == '__main__':
    unittest.main()
