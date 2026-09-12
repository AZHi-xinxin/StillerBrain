"""AI-authored confidence and optional expiry through the real offline dispatcher.

Each subprocess owns three temporary synthetic stores and completes the real
module-one bootstrap. Network and service startup are blocked. Runtime source
validation, the credential detector and ordinary access policy remain real.
This is not an HTTP authentication, gateway-host or model-generation test.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


CEILINGS = dict.fromkeys(('ai_firsthand', 'external_document', 'human_reported', 'ai_inferred'), 100)
MODES = ('create_over', 'revise_over', 'boundaries', 'defaults',
         'error_priority', 'permissions', 'expiry')


class ToolConfidenceLimitGuidanceTests(unittest.TestCase):
    def run_probe(self, mode):
        allowed = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATH',
                   'PATHEXT', 'TEMP', 'TMP', 'OS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        with tempfile.TemporaryDirectory(prefix='tool-confidence-synthetic-') as directory:
            root = Path(directory).resolve()
            env.update({
                'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-confidence-mcp-' + 'm' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-confidence-wake-' + 'w' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-confidence-owner',
                'STBRAIN_MODEL_ID': 'synthetic-confidence-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1',
                'STBRAIN_EXECUTION_EPOCH': 'synthetic-confidence-epoch',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1',
                'PYTHONIOENCODING': 'utf-8',
            })
            completed = subprocess.run(
                [sys.executable, '-B', '-m',
                 'mcp_server.tests.test_tool_confidence_limit_guidance', '--probe', mode],
                cwd=Path(__file__).resolve().parents[2], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=90,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(mode, proof['mode'])
        self.assertGreater(proof['registered_calls'], 0)
        self.assertEqual(0, proof['network_attempts'])
        self.assertTrue(proof['input_arguments_unchanged'])
        self.assertTrue(proof['rejections_left_all_three_databases_unchanged'])
        self.assertFalse(proof['live_instance_used'])

    def test_each_source_above_one_hundred_creation_is_rejected_without_persistence(self):
        self.run_probe('create_over')

    def test_each_source_above_one_hundred_revision_preserves_existing_version(self):
        self.run_probe('revise_over')

    def test_all_sources_accept_author_confidence_one_hundred(self):
        self.run_probe('boundaries')

    def test_default_source_and_partial_revision_keep_authored_confidence(self):
        self.run_probe('defaults')

    def test_types_sources_credentials_and_source_evidence_keep_their_own_errors(self):
        self.run_probe('error_priority')

    def test_module_one_execution_binding_cas_and_risk_permissions_are_unchanged(self):
        self.run_probe('permissions')

    def test_optional_expiry_keep_clear_past_future_and_restore_preserve_versions(self):
        self.run_probe('expiry')


class ToolCardConfidenceRuntimeHintTests(unittest.TestCase):
    def test_service_runtime_range_hint_is_fixed_and_includes_reported_experience(self):
        # Exercise the real runtime fallback behind SDK validation. The fixture
        # supplies only a synthetic owner/wake binding and a temporary store.
        from mcp_server.tests.test_tool_guidance_service import ToolGuidanceAccessServiceTests
        fixture = ToolGuidanceAccessServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.onboarding.allowed = True
        fixture.onboarding.add_wake('ref:one', 'wake:one', 1)

        def snapshot():
            with closing(sqlite3.connect(fixture.database)) as connection:
                return hashlib.sha256('\n'.join(connection.iterdump()).encode()).hexdigest()

        def assert_card_hint(callback):
            before = snapshot()
            result = callback()
            self.assertEqual(['invalid_confidence'], result['reason_codes'])
            self.assertEqual(before, snapshot())
            self.assertFalse(result['state_changed'])
            hint = result['repair_guidance']
            self.assertEqual('confidence', hint['field'])
            self.assertEqual('integer', hint['expected_type'])
            self.assertEqual((0, 100), (hint['minimum'], hint['maximum']))
            self.assertNotIn('maximum_by_source_type', hint)
            self.assertNotIn('SYNTHETIC_PRIVATE_', json.dumps(result))

        assert_card_hint(lambda: fixture.remember(confidence=101))
        stored = fixture.remember()
        self.assertEqual('stored', stored['decision'])
        card_id = stored['card']['card_id']
        assert_card_hint(lambda: fixture.service.revise(
            write_context_ref='ref:one', expected_tool_row_version=1,
            card_id=card_id, expected_card_version=1, confidence=101,
            reason='SYNTHETIC_PRIVATE_RANGE_TEST',
        ))
        before = snapshot()
        experience = fixture.service.record_experience(
            write_context_ref='ref:one', expected_tool_row_version=1,
            card_id=card_id, outcome='unknown', reason_code='synthetic_no_result',
            attempt_summary='Synthetic attempt with no target execution.',
            lesson='Use the current tool result.', confidence=101,
        )
        self.assertEqual(['invalid_confidence'], experience['reason_codes'])
        self.assertEqual(before, snapshot())
        self.assertEqual((0, 100), (experience['repair_guidance']['minimum'],
                                   experience['repair_guidance']['maximum']))
        recorded = fixture.service.record_experience(
            write_context_ref='ref:one', expected_tool_row_version=1,
            card_id=card_id, outcome='unknown', reason_code='synthetic_no_result',
            attempt_summary='Synthetic attempt with no target execution.',
            lesson='Use the current tool result.', confidence=100,
        )
        self.assertEqual('recorded', recorded['decision'])
        self.assertEqual(100, recorded['experience']['confidence'])
        self.assertEqual('ai_reported', recorded['experience']['provenance'])
        self.assertFalse(recorded['experience']['verified'])
        manual = fixture.service.manual()
        self.assertIn('0–100', manual['authoring_constraints']['confidence'])
        self.assertNotIn('0–80', manual['authoring_constraints']['confidence'])


async def probe(mode):
    assert mode in MODES
    root = Path(os.environ['STBRAIN_DB_PATH']).resolve().parent
    databases = {root / name for name in ('main.db', 'ideas.db', 'vault.db')}
    real_connect = sqlite3.connect
    attempts = []

    def only_synthetic(path, *args, **kwargs):
        assert Path(path).resolve() in databases, 'outside_synthetic_database'
        return real_connect(path, *args, **kwargs)

    def no_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('network_or_child_service_forbidden')

    with ExitStack() as guards:
        for target in ('socket.create_connection', 'socket.socket.connect',
                       'socket.socket.connect_ex', 'socket.socket.sendto',
                       'socket.socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
            guards.enter_context(patch(target, side_effect=no_network))
        guards.enter_context(patch('sqlite3.connect', side_effect=only_synthetic))
        from mcp_server import server
        from tests.test_onboarding import ModuleOneOnboardingTests

        calls = 0
        rejections = 0

        def rows(sql, values=()):
            with closing(sqlite3.connect(root / 'main.db')) as connection:
                return connection.execute(sql, values).fetchall()

        def snapshot():
            digests = {}
            for path in sorted(databases):
                with closing(sqlite3.connect(path)) as connection:
                    logical = '\n'.join(connection.iterdump())
                digests[path.name] = hashlib.sha256(logical.encode()).hexdigest()
            return digests

        async def call(name, arguments):
            nonlocal calls
            original = copy.deepcopy(arguments)
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert json.loads(blocks[0].text) == result, 'mcp_conversion_mismatch'
            assert arguments == original, 'caller_arguments_mutated'
            calls += 1
            return result

        async def reject(name, arguments, reason):
            nonlocal rejections
            before = snapshot()
            result = await call(name, arguments)
            assert result.get('decision') == 'reject', 'expected_rejection'
            assert result.get('state_changed') is False, 'rejection_claimed_state_change'
            reasons = result.get('reason_codes', [result.get('reason_code')])
            assert reason in reasons, 'wrong_rejection_reason:' + str(reasons)
            assert snapshot() == before, 'rejection_persisted_state'
            hint = result.get('repair_guidance', {})
            assert 'maximum_by_source_type' not in hint, 'obsolete_source_ceiling_hint'
            serialized = json.dumps(result, ensure_ascii=False)
            assert 'SYNTHETIC_PRIVATE_' not in serialized, 'input_content_echoed'
            rejections += 1
            return result

        def fields(source=None, confidence=None):
            result = {
                'tool_name': '合成工具_' + str(calls),
                'purpose': 'SYNTHETIC_PRIVATE_PURPOSE：整理合成条目。',
            }
            if source is not None:
                result['source_type'] = source
                if source in ('external_document', 'human_reported'):
                    result['source_ref'] = 'Synthetic evidence fixture for ' + source
            if confidence is not None:
                result['confidence'] = confidence
            return result

        async def create(source=None, confidence=None, **changes):
            arguments = fields(source, confidence)
            arguments.update(changes)
            result = await call('remember_tool_guidance', arguments)
            assert result.get('decision') == 'stored', 'creation_failed'
            card = result['card']
            assert card['version'] == 1
            assert card['content']['purpose'] == arguments['purpose'], 'author_text_changed'
            assert card['permission_authority'] == 'none'
            assert result['execution_performed'] is False
            assert 'repair_guidance' not in result
            assert card['content']['source_type'] == (source or 'ai_inferred')
            expected = 50 if confidence is None else confidence
            assert card['content']['claimed_confidence'] == expected
            assert card['content']['effective_confidence'] == expected
            return card

        def target(card, **changes):
            return {'card_id': card['card_id'], 'expected_card_version': card['version'], **changes}

        async def revise(card, **changes):
            result = await call('revise_tool_guidance', target(card, **changes))
            assert result.get('decision') == 'version_appended', 'revision_failed'
            assert result.get('submission_mode') == 'direct_revision'
            assert result.get('review_requires_later_wake') is False
            updated = result['card']
            assert updated['version'] == card['version'] + 1
            assert updated['permission_authority'] == 'none'
            assert result['execution_performed'] is False
            assert 'repair_guidance' not in result
            return updated

        if mode == 'permissions':
            over = fields('ai_inferred', 51)
            await reject('remember_tool_guidance', over, 'module_one_required')
            await reject('revise_tool_guidance', {
                'card_id': 'toolcard_synthetic_unknown', 'expected_card_version': 1,
                'confidence': 51,
            }, 'module_one_required')
            await reject('remember_tool_guidance', {**over, 'execution_ref': 'invented'},
                         'execution_binding_invalid_or_finished')
            assert rows('SELECT COUNT(*) FROM brain_wake_sessions')[0][0] == 0

        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        assert server.onboarding.state(owner_id=server.OWNER_ID,
                                       model_id=server.MODEL_ID)['module_one_unlocked'] is True
        core = rows('SELECT * FROM self_model_revisions ORDER BY revision_id')
        wakes = rows('SELECT wake_id FROM brain_wake_sessions ORDER BY wake_id')

        if mode == 'create_over':
            for source, ceiling in CEILINGS.items():
                await reject('remember_tool_guidance', fields(source, ceiling + 1),
                             'invalid_tool_arguments')
            assert rows('SELECT COUNT(*) FROM tool_cards')[0][0] == 0

        elif mode == 'revise_over':
            for source, ceiling in CEILINGS.items():
                card = await create(source, ceiling)
                old_versions = rows('SELECT * FROM tool_card_versions WHERE card_id=? ORDER BY version',
                                    (card['card_id'],))
                await reject('revise_tool_guidance', target(card, source_type=source,
                    confidence=ceiling + 1, purpose='SYNTHETIC_PRIVATE_REJECTED_REVISION'),
                    'invalid_tool_arguments')
                assert rows('SELECT * FROM tool_card_versions WHERE card_id=? ORDER BY version',
                            (card['card_id'],)) == old_versions

        elif mode == 'boundaries':
            for source, ceiling in CEILINGS.items():
                await create(source, ceiling)
                base = await create('ai_inferred', 40)
                arguments = {'source_type': source, 'confidence': ceiling,
                             'purpose': 'SYNTHETIC_PRIVATE_UPDATED_PURPOSE'}
                if source in ('external_document', 'human_reported'):
                    arguments['source_ref'] = 'Synthetic changed-source fixture for ' + source
                old_versions = rows('SELECT * FROM tool_card_versions WHERE card_id=? ORDER BY version',
                                    (base['card_id'],))
                updated = await revise(base, **arguments)
                assert updated['content']['source_type'] == source
                assert updated['content']['claimed_confidence'] == ceiling, 'confidence_automatically_changed'
                assert updated['content']['effective_confidence'] == ceiling
                assert updated['content']['purpose'] == arguments['purpose']
                versions = rows('SELECT * FROM tool_card_versions WHERE card_id=? ORDER BY version',
                                (base['card_id'],))
                assert len(versions) == 2 and versions[:1] == old_versions, 'old_version_changed'
            assert rows('SELECT COUNT(*) FROM tool_guidance_candidates')[0][0] == 0

        elif mode == 'defaults':
            await reject('remember_tool_guidance', fields(confidence=101),
                         'invalid_tool_arguments')
            default = await create()
            assert default['content']['source_type'] == 'ai_inferred'
            assert default['content']['claimed_confidence'] == 50
            assert default['content']['effective_confidence'] == 50
            for source, ceiling in CEILINGS.items():
                card = await create(source, ceiling - 1)
                await reject('revise_tool_guidance', target(card, confidence=ceiling + 1),
                             'invalid_tool_arguments')
                updated = await revise(card, confidence=ceiling)
                assert updated['content']['source_type'] == source, 'stored_source_not_inherited'
                assert updated['content']['claimed_confidence'] == ceiling
                assert updated['content']['effective_confidence'] == ceiling
                assert updated['content']['source_ref'] == card['content']['source_ref']
                again = await revise(updated, purpose='SYNTHETIC_PRIVATE_PURPOSE_ONLY_EDIT')
                assert again['content']['source_type'] == source
                assert again['content']['claimed_confidence'] == ceiling
                assert again['content']['effective_confidence'] == ceiling

        elif mode == 'error_priority':
            card = await create()
            for bad in ('51', True, 51.5, {}, [], 101, -1):
                await reject('remember_tool_guidance', {**fields(), 'confidence': bad},
                             'invalid_tool_arguments')
                await reject('revise_tool_guidance', target(card, confidence=bad),
                             'invalid_tool_arguments')
            for bad_source in ('SYNTHETIC_PRIVATE_UNKNOWN_SOURCE', 123, [], {}):
                await reject('remember_tool_guidance', {**fields(confidence=51), 'source_type': bad_source},
                             'invalid_tool_arguments')
                await reject('revise_tool_guidance', target(card, source_type=bad_source, confidence=51),
                             'invalid_tool_arguments')
            for secret in ('密码轮换为 SYNTHETIC_PRIVATE_PASSWORD_749',
                           'ｔｏｋｅｎ：SYNTHETIC_PRIVATE_TOKEN_751'):
                await reject('remember_tool_guidance', {**fields('ai_inferred', 51), 'purpose': secret},
                             'credential_or_secret_detected')
                await reject('revise_tool_guidance', target(card, confidence=51, purpose=secret),
                             'credential_or_secret_detected')
            for source in ('external_document', 'human_reported'):
                arguments = fields(source, CEILINGS[source])
                arguments.pop('source_ref')
                await reject('remember_tool_guidance', arguments, 'source_ref_required')
                await reject('revise_tool_guidance', target(card, source_type=source,
                    confidence=CEILINGS[source]), 'source_ref_required')

        elif mode == 'permissions':
            card = await create('ai_firsthand', 85)
            for execution_ref in ('invented', None, {}):
                await reject('remember_tool_guidance', {**fields('ai_inferred', 51),
                    'execution_ref': execution_ref}, 'execution_binding_invalid_or_finished')
                await reject('revise_tool_guidance', target(card, confidence=86,
                    execution_ref=execution_ref), 'execution_binding_invalid_or_finished')
            await reject('remember_tool_guidance', {**fields(), 'owner_id': 'SYNTHETIC_PRIVATE_OTHER_OWNER'},
                         'invalid_tool_arguments')
            updated = await revise(card, purpose='SYNTHETIC_PRIVATE_CAS_CONTROL')
            await reject('revise_tool_guidance', target(card, confidence=86), 'tool_card_version_conflict')
            await reject('revise_tool_guidance', target(updated, risk_level='low'), 'risk_below_runtime_floor')
            await reject('remember_tool_guidance', {**fields(), 'risk_level': 'low'},
                         'risk_below_runtime_floor')
            assert updated['content']['risk_level'] == 'high'
            assert updated['content']['confirmation_policy'] == 'explicit_each_time'

        elif mode == 'expiry':
            from jsonschema import Draft202012Validator, FormatChecker
            schema = json.loads((Path(__file__).resolve().parents[2] / 'schemas/tool-guidance.schema.json').read_text(encoding='utf-8'))
            absent = await create('ai_inferred', 100)
            assert absent['content']['expires_at'] is None
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(absent['content'])
            assert rows('SELECT expires_at FROM tool_card_versions WHERE card_id=?', (absent['card_id'],)) == [('',)]
            past = await create('ai_inferred', 100, expires_at='2000-01-01T00:00:00Z')
            assert past['effective_status'] == 'expired'
            future = await create('human_reported', 100, expires_at='2999-01-01T00:00:00+08:00')
            assert future['content']['expires_at'] == '2999-01-01T00:00:00+08:00'
            initial_versions = rows('SELECT * FROM tool_card_versions WHERE card_id=? ORDER BY version', (past['card_id'],))
            kept = await revise(past, purpose='SYNTHETIC_PRIVATE_KEEP_EXPIRY')
            assert kept['content']['expires_at'] == past['content']['expires_at']
            kept_null = await revise(kept, expires_at=None, purpose='SYNTHETIC_PRIVATE_NULL_KEEPS_EXPIRY')
            assert kept_null['content']['expires_at'] == past['content']['expires_at']
            await reject('revise_tool_guidance', target(kept_null, clear_fields=['expires_at'],
                expires_at='2999-02-01T00:00:00Z'), 'set_and_clear_conflict')
            cleared = await revise(kept_null, clear_fields=['expires_at'])
            assert cleared['content']['expires_at'] is None
            assert cleared['effective_status'] != 'expired'
            assert rows('SELECT expires_at FROM tool_card_versions WHERE card_id=? AND version=?',
                        (cleared['card_id'], cleared['version'])) == [('',)]
            restored = await revise(cleared, intent='restore', target_version=1)
            assert restored['content']['expires_at'] == past['content']['expires_at']
            assert restored['effective_status'] == 'expired'
            restored_null = await revise(restored, intent='restore', target_version=cleared['version'])
            assert restored_null['content']['expires_at'] is None
            assert rows('SELECT * FROM tool_card_versions WHERE card_id=? ORDER BY version',
                        (past['card_id'],))[:1] == initial_versions
            for value in ('2000-01-01', '2000-01-01T00:00:00', 'not-an-iso-date'):
                await reject('remember_tool_guidance', {**fields(), 'expires_at': value}, 'invalid_expires_at')
                await reject('revise_tool_guidance', target(restored_null, expires_at=value), 'invalid_expires_at')
            await reject('revise_tool_guidance', target(past, clear_fields=['expires_at']), 'tool_card_version_conflict')
            for item in (absent, past, future, cleared, restored, restored_null):
                assert item['permission_authority'] == 'none'
                Draft202012Validator(schema, format_checker=FormatChecker()).validate(item['content'])
            # An undated card no longer fails just for lacking an expiry;
            # exact current catalog, real authorization and confirmation still gate attempts.
            from tests.test_tool_guidance import catalog
            native_store = server.tool_guidance_service.store
            native = native_store.remember(owner_id=server.OWNER_ID, model_id=server.MODEL_ID,
                wake_id='synthetic-execution-check',
                expected_row_version=native_store.status(owner_id=server.OWNER_ID, model_id=server.MODEL_ID)['row_version'],
                reason='Synthetic optional expiry gate fixture.', catalog=catalog(),
                tool_name='HomeControl', purpose='Synthetic device advice.', confidence=100)['card']
            gate_args = dict(owner_id=server.OWNER_ID, model_id=server.MODEL_ID,
                card_id=native['card_id'], catalog=catalog(), current_user_intent=True,
                authorization_verified=True, current_confirmation=True)
            before_gate = snapshot()
            assert native_store.execution_gate(**gate_args)['decision'] == 'allowed_to_attempt'
            assert native_store.execution_gate(**{**gate_args, 'authorization_verified': False})['decision'] == 'denied'
            assert native_store.execution_gate(**{**gate_args, 'current_confirmation': False})['decision'] == 'confirmation_required'
            assert native_store.execution_gate(**{**gate_args, 'catalog': None})['decision'] == 'denied'
            assert snapshot() == before_gate

        assert rows('SELECT * FROM self_model_revisions ORDER BY revision_id') == core
        assert rows('SELECT wake_id FROM brain_wake_sessions ORDER BY wake_id') == wakes
        assert not attempts
        return {'decision': 'PASS', 'mode': mode, 'registered_calls': calls,
                'rejection_checks': rejections, 'network_attempts': len(attempts),
                'input_arguments_unchanged': True,
                'rejections_left_all_three_databases_unchanged': True,
                'core_revision_unchanged': True, 'real_wakes_unchanged': True,
                'live_instance_used': False, 'real_model_called': False,
                'http_authentication_tested': False, 'gateway_host_tested': False}


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        print(json.dumps(asyncio.run(probe(sys.argv[2])), ensure_ascii=False))
    else:
        unittest.main()
