"""Offline response guidance: exact catalog matching is not scene-recall permission."""
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


class ToolCatalogDiagnosticGuidanceTests(unittest.TestCase):
    def test_guidance_is_fixed_and_returns_independent_nested_values(self):
        from runtime.tool_guidance import reminder_recall_guidance
        first, second = reminder_recall_guidance(), reminder_recall_guidance()
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertFalse(first['catalog_match_required_for_scene_recall'])
        self.assertFalse(first['automatic_injection_guaranteed'])
        self.assertEqual('exact_callable_name', first['catalog_diagnostic_scope'])
        self.assertIn('MCP 服务名', first['message'])
        first['selection_factors'].clear()
        first['automatic_injection_guaranteed'] = True
        self.assertEqual(second, reminder_recall_guidance())

    def run_probe(self, mode):
        whitelist = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'OS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in whitelist}
        with tempfile.TemporaryDirectory(prefix='tool-catalog-synthetic-') as directory:
            root = Path(directory)
            env.update({'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-catalog-mcp-' + 'm' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-catalog-wake-' + 'w' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-catalog-owner', 'STBRAIN_MODEL_ID': 'synthetic-catalog-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'), 'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1', 'STBRAIN_EXECUTION_EPOCH': 'synthetic-catalog-epoch',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONIOENCODING': 'utf-8'})
            result = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_tool_catalog_diagnostic_guidance', '--probe', mode],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True,
                text=True, encoding='utf-8', timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        proof = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(0, proof['network_attempts'])
        self.assertTrue(proof['caller_arguments_and_existing_versions_unchanged'])
        self.assertTrue(proof['read_guidance_three_databases_unchanged'])
        return proof

    def test_registered_write_revision_and_directory_keep_scene_and_execution_separate(self):
        self.assertTrue(self.run_probe('diagnostic')['stored_reminder_in_actual_dynamic_snapshot'])

    def test_registered_natural_tags_create_revise_recall_and_keep_safety_contracts(self):
        self.assertTrue(self.run_probe('natural_tags')['natural_tags_preserved_and_recalled'])


async def probe(mode):
    assert mode in {'diagnostic', 'natural_tags'}
    root = Path(os.environ['STBRAIN_DB_PATH']).resolve().parent
    allowed = {root / name for name in ('main.db', 'ideas.db', 'vault.db')}
    real_connect = sqlite3.connect
    attempts = []

    def only_synthetic(path, *args, **kwargs):
        assert Path(path).resolve() in allowed
        return real_connect(path, *args, **kwargs)

    def no_network(*args, **kwargs):
        attempts.append(True)
        raise AssertionError('external_io_forbidden')

    with ExitStack() as guards:
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.socket.sendto', 'socket.socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
            guards.enter_context(patch(target, side_effect=no_network))
        guards.enter_context(patch('sqlite3.connect', side_effect=only_synthetic))
        from mcp_server import server
        from runtime.tool_guidance import reminder_recall_guidance
        from tests.test_onboarding import ModuleOneOnboardingTests
        f = ModuleOneOnboardingTests()
        f.database, f.store = root / 'main.db', server.onboarding
        f.owner, f.model = server.OWNER_ID, server.MODEL_ID
        f.bootstrap_live()
        store = server.tool_guidance_service.store

        async def call(name, args):
            original = copy.deepcopy(args)
            blocks, response = await server.mcp.call_tool(name, args)
            assert args == original and json.loads(blocks[0].text) == response
            return response

        def snapshot():
            result = {}
            for p in sorted(allowed):
                with closing(sqlite3.connect(p)) as db:
                    result[p.name] = hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()
            return result

        if mode == 'natural_tags':
            from jsonschema import Draft202012Validator
            tags = ['回家了', 'home.arrival', 'quiet evening']
            for name in ('remember_tool_guidance', 'revise_tool_guidance'):
                parameters = server.mcp._tool_manager.get_tool(name).parameters
                assert Draft202012Validator(parameters['properties']['scenario_tags']).is_valid(tags)
            saved = await call('remember_tool_guidance', {'tool_name': 'SyntheticNaturalService',
                'purpose': '合成场景的工具认知。', 'reminder': 'SYNTHETIC_TAG_MARKER：我可以查看设备。',
                'scenario_tags': tags})
            assert saved['decision'] == 'stored'
            card = saved['card']
            assert card['content']['scenario_tags'] == tags
            surfaced = store.build_recall_envelopes(owner_id=f.owner, model_id=f.model,
                query='我回家了', catalog=None)['envelopes']
            assert surfaced and surfaced[0]['semantic_score'] == 0.95
            revised_tags = ['准备睡觉', 'quiet evening']
            updated = await call('revise_tool_guidance', {'card_id': card['card_id'], 'expected_card_version': 1,
                'scenario_tags': revised_tags})
            assert updated['decision'] == 'version_appended'
            assert updated['card']['content']['scenario_tags'] == revised_tags
            before_read = snapshot()
            precise = await call('recall_tool_guidance', {'view': 'card', 'card_id': card['card_id']})
            assert precise['results'][0]['content']['scenario_tags'] == revised_tags
            history = await call('recall_tool_guidance', {'view': 'history', 'card_id': card['card_id']})
            assert history['versions'][1]['content'] == card['content']
            assert snapshot() == before_read
            before = snapshot()
            stale = await call('revise_tool_guidance', {'card_id': card['card_id'], 'expected_card_version': 1,
                'scenario_tags': ['这个旧版本不得覆盖新版本']})
            assert 'tool_card_version_conflict' in stale['reason_codes'] and snapshot() == before
            for bad in (['密码轮换为 SYNTHETIC_TAG_SECRET_987'], ['到家\n开灯'], ['\x00'], ['\x85'], [''],
                        ['相同', '相同'], ['字' * 129], ['场景' + str(n) for n in range(17)]):
                for name, args in (
                    ('remember_tool_guidance', {'tool_name': 'BadSynthetic', 'purpose': '合成负控。'}),
                    ('revise_tool_guidance', {'card_id': card['card_id'], 'expected_card_version': 2})):
                    before = snapshot()
                    result = await call(name, {**args, 'scenario_tags': bad})
                    assert result['decision'] == 'reject' and snapshot() == before
                    assert 'SYNTHETIC_TAG_SECRET_987' not in json.dumps(result)
            before = snapshot()
            machine_key = await call('remember_tool_guidance', {'tool_name': 'BadMachineKey',
                'purpose': '合成负控。', 'operation_key': '回家了', 'scenario_tags': tags})
            assert machine_key['reason_codes'] == ['invalid_operation_key'] and snapshot() == before
            kept = await call('revise_tool_guidance', {'card_id': card['card_id'], 'expected_card_version': 2,
                'scenario_tags': None, 'purpose': '合成自然标签的当前认知。'})
            assert kept['decision'] == 'version_appended' and kept['card']['content']['scenario_tags'] == revised_tags
            cleared = await call('revise_tool_guidance', {'card_id': card['card_id'], 'expected_card_version': 3,
                'scenario_tags': []})
            assert cleared['decision'] == 'version_appended' and cleared['card']['content']['scenario_tags'] == []
            restored = await call('revise_tool_guidance', {'card_id': card['card_id'], 'expected_card_version': 4,
                'scenario_tags': revised_tags})
            assert restored['decision'] == 'version_appended'
            wake = f.store.issue_wake(owner_id=f.owner, model_id=f.model, host_id='host:natural-tags',
                thread_id='thread:natural-tags', source_kind='human_message', source_event_id='natural-tags')
            prepared = f.store.build_pre_generation_context(owner_id=f.owner, model_id=f.model,
                wake_id=wake['wake_id'], wake_capability=wake['wake_capability'], source_digest='synthetic-source',
                host_contract_digest='synthetic-contract', source_frame={'query_text': '我准备睡觉'})
            assert prepared['decision'] == 'context_prepared'
            with closing(sqlite3.connect(root / 'main.db')) as db:
                dynamic = json.loads(db.execute('SELECT dynamic_json FROM brain_context_snapshots WHERE wake_id=?',
                    (wake['wake_id'],)).fetchone()[0])
            assert dynamic['tool_guidance']['envelopes'][0]['content']['scene_summary'] == card['content']['reminder']
            assert not attempts
            return {'decision': 'PASS', 'network_attempts': 0, 'natural_tags_preserved_and_recalled': True,
                'caller_arguments_and_existing_versions_unchanged': True,
                'read_guidance_three_databases_unchanged': True}

        saved = await call('remember_tool_guidance', {'tool_name': 'SyntheticService',
            'purpose': '回家查看设备。', 'reminder': '回家可以查看合成设备。', 'keywords': ['回家'], 'confidence': 0})
        assert saved['decision'] == 'stored'
        card = saved['card']
        assert card['reminder_recall_guidance'] == reminder_recall_guidance()
        assert 'reminder_recall_guidance' not in card['content']
        updated = await call('revise_tool_guidance', {'card_id': card['card_id'], 'expected_card_version': 1,
            'reminder': 'SYNTHETIC_SCENE_MARKER：回家可以查看设备。'})
        assert updated['decision'] == 'version_appended'
        assert updated['card']['reminder_recall_guidance'] == reminder_recall_guidance()
        assert updated['card']['content']['reminder'] == 'SYNTHETIC_SCENE_MARKER：回家可以查看设备。'
        entries = [{'canonical_name': 'mcp__SyntheticService__status', 'schema_hash': 'a' * 64}]
        catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
            'catalog_hash': hashlib.sha256(json.dumps(entries, ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest(), 'entries': entries}
        before = snapshot()
        detail = store.recall(owner_id=f.owner, model_id=f.model, card_id=card['card_id'], view='card', catalog=catalog)['results'][0]
        assert detail['availability'] == detail['effective_status'] == 'not_advertised'
        assert detail['schema_status'] == 'unknown'
        assert detail['permission_authority'] == 'none'
        assert detail['reminder_recall_guidance'] == reminder_recall_guidance()
        directory = await call('recall_tool_guidance', {'view': 'directory'})
        assert directory['reminder_recall_guidance'] == reminder_recall_guidance()
        assert set(directory['results'][0]) == {'card_id', 'version', 'ref', 'display_label', 'lifecycle', 'reminder'}
        history = await call('recall_tool_guidance', {'view': 'history', 'card_id': card['card_id']})
        assert history['versions'][1]['content_hash'] == card['content_hash']
        assert history['versions'][1]['content'] == card['content']
        assert snapshot() == before
        wake = f.store.issue_wake(owner_id=f.owner, model_id=f.model, host_id='host:synthetic',
            thread_id='thread:synthetic', source_kind='human_message', source_event_id='catalog-reminder')
        prepared = f.store.build_pre_generation_context(owner_id=f.owner, model_id=f.model,
            wake_id=wake['wake_id'], wake_capability=wake['wake_capability'], source_digest='synthetic-source',
            host_contract_digest='synthetic-contract', source_frame={'query_text': '我刚回家'}, advertised_tools=catalog)
        assert prepared['decision'] == 'context_prepared'
        with closing(sqlite3.connect(root / 'main.db')) as db:
            dynamic = json.loads(db.execute('SELECT dynamic_json FROM brain_context_snapshots WHERE wake_id=?',
                (wake['wake_id'],)).fetchone()[0])
        surfaced = dynamic['tool_guidance']['envelopes']
        assert any(x['content']['scene_summary'] == updated['card']['content']['reminder'] for x in surfaced)
        assert 'SYNTHETIC_SCENE_MARKER' not in '我刚回家'
        assert 'reminder_recall_guidance' not in json.dumps(dynamic)
        gate = store.execution_gate(owner_id=f.owner, model_id=f.model, card_id=card['card_id'],
            catalog=catalog, current_user_intent=True, authorization_verified=True, current_confirmation=True)
        assert gate['decision'] == 'denied'
        unrelated = store.build_recall_envelopes(owner_id=f.owner, model_id=f.model,
            query='xyzunrelated987', catalog=catalog)
        assert unrelated['envelopes'] == []
        assert not attempts
        return {'decision': 'PASS', 'network_attempts': len(attempts),
            'stored_reminder_in_actual_dynamic_snapshot': True,
            'caller_arguments_and_existing_versions_unchanged': True,
            'read_guidance_three_databases_unchanged': True,
            'real_instance_used': False, 'real_model_called': False}


if __name__ == '__main__':
    if '--probe' in sys.argv:
        print(json.dumps(asyncio.run(probe(sys.argv[-1])), ensure_ascii=False))
    else:
        unittest.main()
