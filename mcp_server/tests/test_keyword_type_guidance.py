"""Fixed keyword type guidance through real registered MCP; synthetic/offline."""
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


class KeywordTypeGuidanceTests(unittest.TestCase):
    def test_registered_strict_keyword_guidance_and_unchanged_other_contracts(self):
        system = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'OS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in system}
        with tempfile.TemporaryDirectory(prefix='synthetic-keyword-guidance-') as directory:
            root = Path(directory).resolve()
            env.update({
                'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-guidance-mcp-' + 'x' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-guidance-wake-' + 'y' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-guidance-owner', 'STBRAIN_MODEL_ID': 'synthetic-guidance-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1', 'STBRAIN_EXECUTION_EPOCH': 'synthetic-guidance-epoch',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONIOENCODING': 'utf-8',
            })
            completed = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_keyword_type_guidance', '--probe'],
                cwd=Path(__file__).resolve().parents[2], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=75)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(9, proof['supported_shape_writes'])
        self.assertEqual(6, proof['keyword_type_rejections'])
        self.assertEqual(0, proof['network_attempts'])
        self.assertTrue(proof['other_error_contracts_preserved'])


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
        raise AssertionError('network_or_child_service_forbidden')

    with ExitStack() as guards:
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.socket.sendto', 'socket.socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
            guards.enter_context(patch(target, side_effect=no_network))
        guards.enter_context(patch('sqlite3.connect', side_effect=only_synthetic))
        from mcp_server import server
        from tests.test_onboarding import ModuleOneOnboardingTests

        def db_hashes():
            return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in databases}

        async def call(name, arguments):
            original = copy.deepcopy(arguments)
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert json.loads(blocks[0].text) == result
            assert arguments == original, 'argument_was_changed'
            return result

        async def unchanged_rejection(name, arguments):
            before = db_hashes()
            result = await call(name, arguments)
            assert result['decision'] == 'reject' and result['state_changed'] is False
            assert db_hashes() == before, 'rejection_changed_database'
            return result

        guidance_keys = {'expected_type', 'example', 'next_action'}
        basic_error_keys = {'decision', 'reason_code', 'state_changed', 'message',
                            'allowed_fields', 'required_fields', 'field_errors'}
        base = {'module': 'learning_memory', 'content': 'Synthetic keyword guidance.'}
        blocked = await unchanged_rejection('remember_memory', {**base, 'keywords': 'synthetic-string'})
        assert blocked['reason_code'] == 'module_one_required'
        assert 'expected_type' not in blocked and 'example' not in blocked

        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        successes = failures = 0
        plan_ref = None
        secret_value = 'SYNTHETIC_PRIVATE_KEYWORD_VALUE_98A'
        for module in ('emotional_memory', 'learning_memory', 'planning_memory'):
            for shape in ('list', 'null', 'omitted'):
                arguments = {'module': module, 'content': f'Synthetic supported {module} {shape}.'}
                if shape != 'omitted':
                    arguments['keywords'] = ['合成标签', '课堂'] if shape == 'list' else None
                saved = await call('remember_memory', arguments)
                assert saved['decision'] == 'stored', 'supported_shape_failed'
                assert not (guidance_keys & set(saved)), 'guidance_changed_success'
                successes += 1
                if module == 'planning_memory':
                    plan_ref = saved['ref']
            for value in ('["' + secret_value + '",]', json.dumps({'label': secret_value})):
                rejected = await unchanged_rejection('remember_memory', {**base, 'module': module, 'keywords': value})
                assert rejected['reason_code'] == 'invalid_tool_arguments'
                assert rejected['field_errors'] == [{'field': 'keywords', 'issue': 'list_type'}]
                assert set(rejected) == basic_error_keys | guidance_keys
                assert rejected['expected_type'] == 'array<string> | null'
                assert rejected['example'] == ['标签一', '标签二']
                assert '["标签一", "标签二"]' in rejected['next_action']
                assert '省略' in rejected['next_action'] and 'null' in rejected['next_action']
                assert secret_value not in json.dumps(rejected, ensure_ascii=False), 'input_value_echoed'
                failures += 1

        unknown_key = 'SYNTHETIC_UNKNOWN_ARGUMENT_NAME_67B'
        extra = await unchanged_rejection('remember_memory', {**base, 'keywords': ['合成'], unknown_key: secret_value})
        assert set(extra) == basic_error_keys, 'other_error_contract_changed'
        assert extra['field_errors'] == [{'field': 'unrecognized_field', 'issue': 'extra_forbidden'}]
        assert unknown_key not in json.dumps(extra) and secret_value not in json.dumps(extra)

        mixed = await unchanged_rejection('remember_memory', {**base, 'keywords': '["' + secret_value + '",]', unknown_key: secret_value})
        assert mixed['expected_type'] == 'array<string> | null'
        assert unknown_key not in json.dumps(mixed) and secret_value not in json.dumps(mixed)
        assert {'field': 'unrecognized_field', 'issue': 'extra_forbidden'} in mixed['field_errors']

        element = await unchanged_rejection('remember_memory', {**base, 'keywords': [123]})
        assert set(element) == basic_error_keys, 'non_list_type_contract_changed'
        assert element['field_errors'] == [{'field': 'keywords', 'issue': 'string_type'}]

        other_list = await unchanged_rejection('advance_plan', {'target_ref': plan_ref, 'expected_event_seq': 0,
            'event_type': 'pause', 'note': 'Synthetic pause.', 'evidence': secret_value})
        assert set(other_list) == basic_error_keys | {'next_action'}
        assert other_list['field_errors'] == [{'field': 'evidence', 'issue': 'list_type'}]
        assert 'advance_plan 使用 target_ref/expected_event_seq/event_type/note' in other_list['next_action']
        assert secret_value not in json.dumps(other_list)

        bad_ref = await unchanged_rejection('remember_memory', {**base, 'keywords': secret_value, 'execution_ref': 'invented'})
        assert bad_ref['reason_code'] == 'execution_binding_invalid_or_finished'
        assert 'expected_type' not in bad_ref and 'example' not in bad_ref
        assert not attempts
        return {'decision': 'PASS', 'supported_shape_writes': successes,
                'keyword_type_rejections': failures, 'other_error_contracts_preserved': True,
                'strict_type_rejection_preserved': True, 'network_attempts': len(attempts),
                'live_instance_used': False, 'http_authentication_tested': False}


if __name__ == '__main__':
    if sys.argv[1:] == ['--probe']:
        print(json.dumps(asyncio.run(probe()), ensure_ascii=False))
    else:
        unittest.main()
