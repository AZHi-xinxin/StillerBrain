"""Selected static manuals through registered MCP, offline temporary stores only."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
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

from mcp_server.public_contract import BRAIN_MANUAL_MODULES
from mcp_server.usage_guide import module_usage_guide


class StaticModuleHelpTests(unittest.TestCase):
    def test_each_module_has_read_write_and_profile_instructions(self):
        for simple in (False, True):
            for module in BRAIN_MANUAL_MODULES:
                with self.subTest(simple=simple, module=module):
                    result = module_usage_guide(module, simple=simple)
                    self.assertEqual(module, result['module'])
                    self.assertTrue(result['purpose'] and result['read'] and result['workflow'])
                    self.assertFalse(result['state_changed'])
                    self.assertFalse(result['write_context_created'])
                    self.assertFalse(result['review_evidence_created'])
                    self.assertEqual('static_instructions', result['manual_scope'])
                    self.assertNotIn('write_context_ref', result)
                    self.assertNotIn('grant_ref', result)
                    self.assertIn('stbrain_health', result['status_source'])
                    result['workflow'].append('mutation')
                    self.assertNotIn('mutation', module_usage_guide(module, simple=simple)['workflow'])

    def test_learning_body_revision_is_documented_from_actual_whitelist(self):
        self.assertIn('current_understanding', module_usage_guide('learning_memory', simple=True)['revise_author_fields'])
        self.assertIn('original_text', module_usage_guide('emotional_memory', simple=True)['revise_author_fields'])
        self.assertNotIn('owner_id', module_usage_guide('learning_memory', simple=True)['revise_author_fields'])

    def test_unknown_module_is_not_silently_replaced(self):
        with self.assertRaisesRegex(ValueError, 'unknown_manual_module'):
            module_usage_guide('not-a-module', simple=True)

    def test_registered_simple_help_keeps_stores_candidates_and_gates_unchanged(self):
        self.registered_probe(True)

    def test_registered_legacy_help_keeps_stores_candidates_and_gates_unchanged(self):
        self.registered_probe(False)

    def registered_probe(self, simple):
        system = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'OS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in system}
        with tempfile.TemporaryDirectory(prefix='synthetic-static-help-') as directory:
            root = Path(directory).resolve()
            env.update({
                'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1' if simple else '',
                'STBRAIN_MCP_TOKEN': 'synthetic-static-help-mcp-' + 'a' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-static-help-wake-' + 'b' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-help-owner', 'STBRAIN_MODEL_ID': 'synthetic-help-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1', 'STBRAIN_EXECUTION_EPOCH': 'synthetic-help-epoch',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONIOENCODING': 'utf-8',
            })
            result = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_static_module_help', '--probe'],
                cwd=Path(__file__).resolve().parents[2], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=90)
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(3, proof['phases'])
        self.assertEqual(27, proof['selected_reads'])
        self.assertEqual(0, proof['network_attempts'])


async def probe():
    root = Path(os.environ['STBRAIN_DB_PATH']).resolve().parent
    databases = {root / name for name in ('main.db', 'ideas.db', 'vault.db')}
    real_connect = sqlite3.connect
    attempts = []

    def only_synthetic(path, *args, **kwargs):
        assert Path(path).resolve() in databases, 'outside_synthetic_database'
        return real_connect(path, *args, **kwargs)

    def no_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('network_or_service_forbidden')

    with ExitStack() as guards:
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.socket.sendto', 'socket.socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
            guards.enter_context(patch(target, side_effect=no_network))
        guards.enter_context(patch('sqlite3.connect', side_effect=only_synthetic))
        from mcp_server import server
        from tests.test_onboarding import ModuleOneOnboardingTests
        from jsonschema import validate

        def snapshot():
            return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in databases}

        async def call(name, arguments):
            original = copy.deepcopy(arguments)
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert json.loads(blocks[0].text) == result
            assert arguments == original
            return result

        catalog = {tool.name: tool for tool in server.mcp._tool_manager.list_tools()}
        assert len(catalog) == 44
        assert ('revise_tool_guidance' in catalog) is (not server.SIMPLE_MEMORY_ACCESS)
        assert ('authorize_self_model' in catalog) is server.SIMPLE_MEMORY_ACCESS
        help_tool = catalog['stbrain_help']
        assert help_tool.parameters.get('required', []) == []
        assert 'module' in help_tool.parameters['properties']
        assert 'execution_ref' not in help_tool.parameters['properties']
        assert 'stbrain_open' not in __import__('mcp_server.execution_guard', fromlist=['READ_ONLY_TOOLS']).READ_ONLY_TOOLS

        async def check_phase():
            before = snapshot()
            # Even an accidental status/manual/open call is forbidden for help.
            with ExitStack() as blocked:
                for obj, name in ((server.onboarding, 'open_brain_context'),
                                  (server.service, 'open_brain'), (server.service, 'health')):
                    blocked.enter_context(patch.object(obj, name, side_effect=AssertionError('help_accessed_state')))
                overview = await call('stbrain_help', {})
                assert overview['state_changed'] is False
                assert set(overview['module_help']['modules']) == set(BRAIN_MANUAL_MODULES)
                # The DIY entry must be executable through the real published
                # catalog, not just a label in a separately built help dict.
                for key in ('help', 'read_entry'):
                    recipe = overview['self_reminders'][key]
                    assert recipe['tool'] in catalog
                    validate(recipe['arguments'], catalog[recipe['tool']].parameters)
                assert overview['self_reminders']['read_entry']['arguments']['include_content'] is True
                for module in BRAIN_MANUAL_MODULES:
                    result = await call('stbrain_help', {'module': module})
                    assert result['module'] == module and result['state_changed'] is False
                    assert result['write_context_created'] is False and result['review_evidence_created'] is False
                    for item in result['read']:
                        assert item['tool'] in catalog
                        if 'arguments' in item:
                            validate(item['arguments'], catalog[item['tool']].parameters)
                    for tool_name in result['write_tools']:
                        assert tool_name in catalog
                    if 'example' in result:
                        example = result['example']
                        # The advertised schema reflects profile-specific
                        # host-filled bookkeeping; the low-level Python model
                        # intentionally still has legacy required parameters.
                        validate(example['arguments'], catalog[example['tool']].parameters)
                try:
                    await call('stbrain_help', {'module': 'unknown-module'})
                except Exception:
                    pass
                else:
                    raise AssertionError('invalid_module_accepted')
            # Reading instructions never legitimizes a fabricated/bare open.
            for view in ('summary', 'manual', 'review'):
                for execution in ({}, {'execution_ref': None}, {'execution_ref': 'invented'}):
                    denied = await call('stbrain_open', {'view': view, **execution})
                    assert denied['decision'] == 'reject' and denied['state_changed'] is False
                    assert 'stbrain_help' in denied['next_action']
                    assert 'stbrain_open_direct' in denied['next_action']
                    assert denied['reason_code'] in {'execution_binding_required', 'execution_binding_invalid_or_finished'}
            assert snapshot() == before, 'static_help_or_rejection_changed_database'

        await check_phase()
        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_to_wait()
        await check_phase()
        wake2, _ = fixture.wake('static-help-review')
        fixture.open_brain()
        assert fixture.advance(wake2, 'accept_candidate_review', {'ai_confirmation': True})['decision'] == 'review_accepted'
        wake3, _ = fixture.wake('static-help-activate')
        fixture.open_brain()
        activated = fixture.advance(wake3, 'activate_candidate',
                                    {'expected_active_revision': None, 'ai_confirmation': True})
        assert activated['decision'] == 'activate' and activated['pointer_changed'] is True
        await check_phase()
        assert not attempts
        return {'decision': 'PASS', 'phases': 3, 'selected_reads': 27,
                'network_attempts': len(attempts), 'real_model_requested': False}


if __name__ == '__main__':
    if sys.argv[1:] == ['--probe']:
        print(json.dumps(asyncio.run(probe()), ensure_ascii=False))
    else:
        unittest.main()
