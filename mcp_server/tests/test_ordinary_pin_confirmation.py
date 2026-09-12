"""Pin confirmation modes: synthetic data, real registered MCP dispatch, no network."""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests import test_emotional_memory as fixtures
from tests import test_onboarding as onboarding_fixtures
from runtime import ModuleOneOnboardingStore
from runtime.emotional_memory import EmotionalMemoryError, EmotionalMemoryStore
from runtime.ordinary_access import authenticated_ordinary_operation
from mcp_server.emotional_service import EmotionalMemoryAccessService


class OrdinaryPinConfirmationTests(unittest.TestCase):
    setUp = fixtures.EmotionalMemoryTests.setUp
    tearDown = fixtures.EmotionalMemoryTests.tearDown
    version = fixtures.EmotionalMemoryTests.version
    _seed_active_self_model = fixtures.EmotionalMemoryTests._seed_active_self_model

    def rows(self, table):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute('SELECT * FROM ' + table).fetchall()

    def context(self):
        return authenticated_ordinary_operation(owner_id=self.owner, model_id=self.model, scope='emotional_memory')

    def request(self, access=None):
        return self.store.manage_pin(owner_id=self.owner, model_id=self.model,
            wake_id=access['wake_id'] if access else 'legacy-wake-10',
            wake_seq=access['wake_seq'] if access else 10,
            expected_row_version=self.version(), action='request', reason='Synthetic request.',
            pin_kind='identity_anchor', display_text='我愿意持续区分自己的判断与外部建议。',
            source_ref=f'self-model-revision://{self.active_revision_id}/core_identity_anchors/0')

    def confirm(self, pin_id, access=None, **overrides):
        args = dict(owner_id=self.owner, model_id=self.model,
            wake_id=access['wake_id'] if access else 'legacy-wake-11',
            wake_seq=access['wake_seq'] if access else 11,
            expected_row_version=self.version(), action='confirm', reason='Synthetic confirmation.',
            pin_id=pin_id, ai_confirmation=True)
        args.update(overrides)
        return self.store.manage_pin(**args)

    def test_ordinary_explicit_confirmation_can_share_operation_without_later_wake(self):
        before = self.rows('self_model_revisions')
        with self.context() as access:
            pending = self.request(access)
            active = self.confirm(pending['pin']['pin_id'], access)
        self.assertEqual('pin_activated', active['decision'])
        self.assertFalse(active['pin']['confirmation_requires_later_real_wake'])
        self.assertEqual(before, self.rows('self_model_revisions'))
        with closing(sqlite3.connect(self.database)) as connection:
            codes = [json.loads(row[0]) for row in connection.execute(
                "SELECT reason_codes_json FROM emotion_audit_events WHERE action IN ('pin_request','pin_confirm') ORDER BY event_seq")]
        self.assertEqual([['author_confirmation_required'], ['author_confirmed']], codes)

    def test_legacy_request_keeps_real_wake_rule_and_rejects_request_sequence(self):
        pending = self.request()
        self.assertTrue(pending['pin']['confirmation_requires_later_real_wake'])
        with self.context() as access, self.assertRaisesRegex(EmotionalMemoryError, 'pin_legacy_confirmation_requires_real_wake'):
            self.confirm(pending['pin']['pin_id'], access)
        with self.assertRaisesRegex(EmotionalMemoryError, 'pin_cross_wake_required'):
            self.confirm(pending['pin']['pin_id'], wake_id='legacy-wake-10', wake_seq=10)
        self.assertEqual('pin_activated', self.confirm(pending['pin']['pin_id'])['decision'])

    def test_ordinary_confirmation_keeps_explicit_intent_cas_and_context_binding(self):
        with self.context() as access:
            pending = self.request(access)
        with self.context() as access:
            for overrides, reason in (({'ai_confirmation': False}, 'pin_ai_confirmation_required'),
                                      ({'expected_row_version': 0}, 'emotion_row_version_conflict'),
                                      ({'wake_id': 'caller-forged'}, 'ordinary_operation_binding_mismatch')):
                before = self.rows('brain_pins')
                with self.subTest(reason=reason), self.assertRaisesRegex(EmotionalMemoryError, reason):
                    self.confirm(pending['pin']['pin_id'], access, **overrides)
                self.assertEqual(before, self.rows('brain_pins'))
            self.assertEqual('pin_activated', self.confirm(pending['pin']['pin_id'], access)['decision'])

    def test_additive_mode_migration_keeps_old_pin_payload_and_legacy_contract(self):
        pending = self.request()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute('ALTER TABLE brain_pins DROP COLUMN requested_context_mode')
            connection.commit()
            before = connection.execute('SELECT * FROM brain_pins').fetchall()
        self.store = EmotionalMemoryStore(self.database)
        self.store = EmotionalMemoryStore(self.database)
        after = self.rows('brain_pins')
        self.assertEqual(before, [row[:-1] for row in after])
        self.assertEqual('legacy_open_wake', after[0][-1])
        with self.context() as access, self.assertRaisesRegex(EmotionalMemoryError, 'pin_legacy_confirmation_requires_real_wake'):
            self.confirm(pending['pin']['pin_id'], access)

    def test_registered_mcp_two_calls_after_activation_without_extra_wake(self):
        allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'USERPROFILE',
                   'LOCALAPPDATA', 'APPDATA', 'PATHEXT', 'SYSTEMDRIVE', 'NUMBER_OF_PROCESSORS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='ordinary-pin-synthetic-') as scratch:
            env.update({'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-pin-token-00000000000000000000',
                'STBRAIN_WAKE_SECRET': 'synthetic-pin-wake-00000000000000000000',
                'STBRAIN_OWNER_ID': 'synthetic-pin-owner', 'STBRAIN_MODEL_ID': 'synthetic-pin-model',
                'STBRAIN_DB_PATH': str(Path(scratch) / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(Path(scratch) / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(Path(scratch) / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1', 'STBRAIN_EXECUTION_EPOCH': 'synthetic-pin-epoch',
                'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'})
            result = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_ordinary_pin_confirmation', '--probe'],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding='utf-8', timeout=40)
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(3, proof['initialization_wakes'])
        self.assertEqual(0, proof['ordinary_wakes_created'])
        self.assertEqual(0, proof['network_attempts'])
        self.assertEqual(['author_confirmation_required', 'author_confirmed'], proof['audit_codes'])


async def registered_probe():
    from mcp_server import server
    database = Path(os.environ['STBRAIN_DB_PATH'])
    fixture = onboarding_fixtures.ModuleOneOnboardingTests()
    fixture.database, fixture.store = database, server.onboarding
    fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
    # Complete the real synthetic three-wake flow. A manually inserted revision
    # is no longer the prerequisite for public ordinary-memory writes.
    active_revision_id = fixture.bootstrap_live()
    source_text = onboarding_fixtures.model_content()['active_identity_capsule']['core_identity_anchors'][0]

    def rows(table):
        with closing(sqlite3.connect(database)) as connection:
            return connection.execute('SELECT * FROM ' + table).fetchall()

    async def call(arguments):
        blocks, result = await server.mcp.call_tool('manage_brain_pin', arguments)
        assert json.loads(blocks[0].text) == result, 'MCP result mismatch'
        return result

    attempts = []
    def no_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('Network forbidden')

    original = rows('self_model_revisions')
    initial_wakes = rows('brain_wake_sessions')
    initial_snapshots = rows('brain_context_snapshots')
    with ExitStack() as guards:
        for target in ('socket.socket.connect', 'socket.socket.connect_ex', 'socket.socket.sendto',
                       'socket.create_connection', 'socket.getaddrinfo'):
            guards.enter_context(patch(target, side_effect=no_network))
        pending = await call({'action': 'request', 'reason': 'Synthetic source projection.',
            'pin_kind': 'identity_anchor', 'display_text': source_text,
            'source_ref': f'self-model-revision://{active_revision_id}/core_identity_anchors/0'})
        assert pending.get('decision') == 'pin_pending', 'Request rejected'
        active = await call({'action': 'confirm', 'pin_id': pending['pin']['pin_id'],
                             'ai_confirmation': True, 'reason': 'Synthetic current author confirmation.'})
        assert active.get('decision') == 'pin_activated', 'Confirmation rejected'
    assert active['pin']['requested_context_mode'] == 'ordinary_authenticated'
    assert active['pin']['confirmation_requires_later_real_wake'] is False
    assert rows('self_model_revisions') == original, 'Core source changed'
    assert rows('brain_wake_sessions') == initial_wakes, 'Ordinary calls changed initialization wakes'
    assert rows('brain_context_snapshots') == initial_snapshots, 'Ordinary calls changed initialization snapshots'
    assert not attempts, 'Network attempt'
    with closing(sqlite3.connect(database)) as connection:
        audits = connection.execute("SELECT reason_codes_json FROM emotion_audit_events WHERE action IN ('pin_request','pin_confirm') ORDER BY event_seq").fetchall()
        modes = connection.execute('SELECT requested_wake_id,confirmed_wake_id,requested_context_mode FROM brain_pins').fetchone()
    assert modes[0].startswith('ordinaryop_') and modes[1].startswith('ordinaryop_') and modes[0] != modes[1]
    return {'decision': 'PASS', 'initialization_wakes': len(initial_wakes),
            'ordinary_wakes_created': len(rows('brain_wake_sessions')) - len(initial_wakes),
            'network_attempts': len(attempts), 'audit_codes': [code for row in audits for code in json.loads(row[0])],
            'source_self_model_unchanged': True, 'registered_calls': 2}


if __name__ == '__main__':
    if sys.argv[1:] == ['--probe']:
        print(json.dumps(asyncio.run(registered_probe())))
    else:
        unittest.main()
