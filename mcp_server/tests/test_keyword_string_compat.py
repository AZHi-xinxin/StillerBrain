"""Keywords-only compatibility through actual registered MCP and execution claims.

Every probe uses newly activated synthetic stores. Network/service startup and
non-fixture SQLite connections are blocked. Neither normalization nor the
credential detector, execution store or ordinary policy is mocked.
The signed probe tests the MCP post-claim boundary only, not the gateway host's
schema validation. Actual gateway callers continue to supply canonical arrays.
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


MODES = (
    'positive', 'nested_and_tools', 'invalid_formats', 'credentials',
    'priority', 'unrelated_fields', 'gateway_raw_claim', 'canonical_schema',
)


class KeywordStringCompatibilityTests(unittest.TestCase):
    def run_probe(self, mode):
        system = {'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE', 'PATH', 'PATHEXT', 'TEMP', 'TMP', 'OS'}
        env = {key: value for key, value in os.environ.items() if key.upper() in system}
        with tempfile.TemporaryDirectory(prefix='keyword-compat-synthetic-') as directory:
            root = Path(directory).resolve()
            env.update({
                'STBRAIN_ACCESS_PROFILE': 'simple-memory-v1',
                'STBRAIN_MCP_TOKEN': 'synthetic-keyword-mcp-' + 'm' * 40,
                'STBRAIN_WAKE_SECRET': 'synthetic-keyword-wake-' + 'w' * 40,
                'STBRAIN_OWNER_ID': 'synthetic-keyword-owner', 'STBRAIN_MODEL_ID': 'synthetic-keyword-model',
                'STBRAIN_DB_PATH': str(root / 'main.db'),
                'STBRAIN_LEARNING_IDEA_DB_PATH': str(root / 'ideas.db'),
                'STBRAIN_HALLUCINATION_VAULT_DB_PATH': str(root / 'vault.db'),
                'STBRAIN_REQUIRE_EXECUTION_BINDING': '1', 'STBRAIN_EXECUTION_EPOCH': 'synthetic-keyword-epoch',
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1', 'PYTHONIOENCODING': 'utf-8',
            })
            completed = subprocess.run([sys.executable, '-B', '-m',
                'mcp_server.tests.test_keyword_string_compat', '--probe', mode],
                cwd=Path(__file__).resolve().parents[2], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=90)
        self.assertEqual(0, completed.returncode, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual('PASS', proof['decision'])
        self.assertEqual(mode, proof['mode'])
        self.assertEqual(0, proof['network_attempts'])
        self.assertTrue(proof['input_arguments_unchanged'])
        self.assertFalse(proof['live_instance_used'])

    def test_registered_shapes_and_delimiters_preserve_keyword_meaning(self):
        self.run_probe('positive')

    def test_nested_revision_and_dedicated_tool_keywords_are_supported(self):
        self.run_probe('nested_and_tools')

    def test_malformed_or_oversized_strings_reject_without_persistence_or_echo(self):
        self.run_probe('invalid_formats')

    def test_credential_content_remains_blocked_before_and_after_decoding(self):
        self.run_probe('credentials')

    def test_unactivated_and_bad_execution_requests_fail_before_compatibility(self):
        self.run_probe('priority')

    def test_other_fields_unknown_keys_and_runtime_limits_remain_strict(self):
        self.run_probe('unrelated_fields')

    def test_signed_raw_arguments_are_claimed_before_normalization_and_cannot_replay(self):
        """MCP post-claim behavior only; no HostExecutionBoundary is exercised."""
        self.run_probe('gateway_raw_claim')

    def test_public_metadata_still_recommends_canonical_arrays(self):
        self.run_probe('canonical_schema')


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
        for target in ('socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.socket.sendto', 'socket.socket.bind', 'socket.getaddrinfo', 'subprocess.Popen'):
            guards.enter_context(patch(target, side_effect=no_network))
        guards.enter_context(patch('sqlite3.connect', side_effect=only_synthetic))
        from mcp_server import server
        from runtime.execution_binding import ExecutionStore, canonical_hash
        from tests.test_onboarding import ModuleOneOnboardingTests

        common = {'owner_id': server.OWNER_ID, 'model_id': server.MODEL_ID}
        calls = 0

        def snapshot():
            # Logical snapshots include all tables, including audits and execution
            # state. Signed successful calls deliberately use separate assertions.
            result = {}
            for path in sorted(databases):
                with closing(sqlite3.connect(path)) as connection:
                    rows = list(connection.iterdump())
                result[path.name] = hashlib.sha256('\n'.join(rows).encode()).hexdigest()
            return result

        def rows(sql, values=()):
            with closing(sqlite3.connect(root / 'main.db')) as connection:
                return connection.execute(sql, values).fetchall()

        async def call(name, arguments):
            nonlocal calls
            original = copy.deepcopy(arguments)
            blocks, result = await server.mcp.call_tool(name, arguments)
            assert json.loads(blocks[0].text) == result, 'mcp_conversion_mismatch'
            assert arguments == original, 'caller_arguments_mutated'
            calls += 1
            return result

        async def rejected(name, arguments, reason=None):
            before = snapshot()
            result = await call(name, arguments)
            assert result.get('decision') == 'reject' and result.get('state_changed') is False, 'expected_reject'
            assert snapshot() == before, 'rejection_persisted_state'
            if reason:
                assert result.get('reason_code') == reason, 'wrong_rejection_reason'
            return result

        def current_keywords(module, saved):
            if module == 'emotional_memory':
                return json.loads(rows('SELECT keywords_json FROM emotion_memories WHERE memory_id=?', (saved['id'],))[0][0])
            if module == 'learning_memory':
                return json.loads(rows('SELECT current_json FROM learning_items WHERE learning_id=?', (saved['id'],))[0][0])['keywords']
            return json.loads(rows('SELECT content_json FROM planning_versions WHERE plan_id=? ORDER BY version DESC LIMIT 1',
                                   (saved['id'],))[0][0])['keywords']

        async def save(module, keywords=None, *, omit=False):
            text = '  Synthetic authored text ' + str(calls) + '\nkept exactly.  '
            arguments = {'module': module, 'content': text}
            if not omit:
                arguments['keywords'] = keywords
            saved = await call('remember_memory', arguments)
            assert saved.get('decision') == 'stored', 'supported_keywords_not_saved'
            if module == 'emotional_memory':
                stored_text = rows('SELECT original_text FROM emotion_memories WHERE memory_id=?', (saved['id'],))[0][0]
            elif module == 'learning_memory':
                stored_text = json.loads(rows('SELECT current_json FROM learning_items WHERE learning_id=?', (saved['id'],))[0][0])['current_understanding']
            else:
                stored_text = json.loads(rows('SELECT content_json FROM planning_versions WHERE plan_id=? ORDER BY version DESC LIMIT 1',
                                             (saved['id'],))[0][0])['original_text']
            assert stored_text == text, 'author_original_changed'
            return saved

        base = {'module': 'learning_memory', 'content': 'Synthetic keyword compatibility.'}
        if mode == 'priority':
            for keywords in ('[malformed', 'token：SYNTHETIC_SECRET_9284', 'ordinary phrase'):
                blocked = await rejected('remember_memory', {**base, 'keywords': keywords}, 'module_one_required')
                assert 'expected_type' not in blocked and 'example' not in blocked
                bad = await rejected('remember_memory', {**base, 'keywords': keywords, 'execution_ref': 'invented'},
                                     'execution_binding_invalid_or_finished')
                assert 'expected_type' not in bad and 'example' not in bad
            assert rows('SELECT COUNT(*) FROM brain_wake_sessions')[0][0] == 0

        fixture = ModuleOneOnboardingTests()
        fixture.database, fixture.store = root / 'main.db', server.onboarding
        fixture.owner, fixture.model = server.OWNER_ID, server.MODEL_ID
        fixture.bootstrap_live()
        assert server.onboarding.state(**common)['module_one_unlocked'] is True
        original_core = rows('SELECT * FROM self_model_revisions ORDER BY revision_id')
        initial_wakes = rows('SELECT wake_id FROM brain_wake_sessions ORDER BY wake_id')

        if mode == 'positive':
            shapes = (
                (['甲', '乙'], ['甲', '乙']), (None, []),
                (' 单个标签 ', ['单个标签']), (' Alpha beta ', ['Alpha beta']),
                ('["甲,乙","丙；丁","戊、己"]', ['甲,乙', '丙；丁', '戊、己']),
                (' 甲,乙，丙、丁;戊；己\r\n庚\n辛\r壬 ', list('甲乙丙丁戊己庚辛壬')),
                ('[]', []),
            )
            for module in ('emotional_memory', 'learning_memory', 'planning_memory'):
                for value, expected in shapes:
                    stored = await save(module, value)
                    assert current_keywords(module, stored) == expected, 'keyword_meaning_changed'
                stored = await save(module, omit=True)
                assert current_keywords(module, stored) == []
            # Learning already preserves duplicates; compatibility must not remove
            # them. Other runtimes keep their own existing canonicalization rules.
            duplicate = await save('learning_memory', '甲,甲')
            assert current_keywords('learning_memory', duplicate) == ['甲', '甲']
            encoded = await save('learning_memory', '["\\u7532","emoji 星星"]')
            assert current_keywords('learning_memory', encoded) == ['甲', 'emoji 星星']
            for boundary in (' ' * 16383 + '[]' + ' ' * 16383,
                             '[' + ' ' * 32766 + ']'):
                assert len(boundary) == 32768
                saved = await save('learning_memory', boundary)
                assert current_keywords('learning_memory', saved) == []
                await rejected('remember_memory', {**base, 'keywords': boundary + ' '}, 'invalid_tool_arguments')

        elif mode == 'nested_and_tools':
            for module in ('emotional_memory', 'learning_memory', 'planning_memory'):
                stored = await save(module, ['旧标签'])
                result = await call('revise_memory', {'target_ref': stored['ref'],
                    'changes': {'keywords': '["新标签","含,标点"]'}, 'reason': 'Synthetic keyword revision.'})
                assert result.get('decision') == 'revised'
                assert current_keywords(module, stored) == ['新标签', '含,标点']
                malformed = await rejected('revise_memory', {'target_ref': result['ref'],
                    'changes': {'keywords': '["PRIVATE_SYNTHETIC_BAD_745",]'}}, 'invalid_tool_arguments')
                assert 'PRIVATE_SYNTHETIC_BAD_745' not in json.dumps(malformed)
                assert {'field': 'changes', 'issue': 'keywords_format'} in malformed.get('field_errors', [])
                assert malformed.get('expected_type') == 'array<string>'
                assert 'null' not in malformed.get('next_action', '')
                assert '省略' not in malformed.get('next_action', '')
            stored = await save('planning_memory', ['旧计划'])
            result = await call('revise_planning_memory', {'plan_id': stored['id'], 'expected_plan_version': 1,
                'intent': 'revise', 'reason': 'Synthetic dedicated keyword revision.', 'calm_check': {},
                'ai_confirmation': True, 'idempotency_key': 'synthetic-keyword-planning-edit',
                'changes': {'keywords': '计划甲；计划乙'}})
            assert result.get('decision') == 'revised'
            assert current_keywords('planning_memory', stored) == ['计划甲', '计划乙']
            card = await call('remember_tool_guidance', {'tool_name': '合成分类工具', 'purpose': '整理合成测试条目。',
                                                       'keywords': '分类、整理'})
            assert card.get('decision') == 'stored'
            assert card['card']['content']['keywords'] == ['分类', '整理']
            revised = await call('revise_tool_guidance', {'card_id': card['card']['card_id'],
                'expected_card_version': 1, 'keywords': '["分类,整理","测试"]'})
            assert revised.get('card', {}).get('content', {}).get('keywords') == ['分类,整理', '测试']

        elif mode == 'invalid_formats':
            values = ('', '  \t ', '[', '["SYNTHETIC_BAD_VALUE_746",]', '{"x":"SYNTHETIC_BAD_VALUE_746"}',
                      '["甲",1]', '[null]', '[["甲"]]', '[""]', '["   "]', '"甲"', "['甲']",
                      ',甲', '甲,', '甲,,乙', '甲\r\n\n乙', ']甲', '}甲', '[' + ' ' * 32768)
            for value in values:
                result = await rejected('remember_memory', {**base, 'keywords': value}, 'invalid_tool_arguments')
                assert result.get('expected_type') == 'array<string> | null'
                assert result.get('example') == ['标签一', '标签二']
                assert result.get('field_errors') == [{'field': 'keywords', 'issue': 'list_type'}]
                assert 'SYNTHETIC_BAD_VALUE_746' not in json.dumps(result)
            # Decode at most once: a JSON-quoted array string is not a string array.
            twice = json.dumps(json.dumps(['甲']))
            await rejected('remember_memory', {**base, 'keywords': twice}, 'invalid_tool_arguments')

        elif mode == 'credentials':
            values = ('token：SYNTHETIC_PRIVATE_VALUE_747',
                      '密码轮换：SYNTHETIC_OLD_747 → SYNTHETIC_NEW_747',
                      '密码："$DEMO_PASSWORD"，实际值为 SYNTHETIC_PRIVATE_VALUE_747',
                      '["普通标签","token\\uff1aSYNTHETIC_PRIVATE_VALUE_747"]')
            for value in values:
                result = await rejected('remember_memory', {**base, 'keywords': value}, 'credential_or_secret_detected')
                assert 'SYNTHETIC_PRIVATE_VALUE_747' not in json.dumps(result)
                assert 'SYNTHETIC_OLD_747' not in json.dumps(result)
            stored = await save('learning_memory', ['普通'])
            result = await rejected('revise_memory', {'target_ref': stored['ref'],
                'changes': {'keywords': values[-1]}}, 'credential_or_secret_detected')
            assert 'SYNTHETIC_PRIVATE_VALUE_747' not in json.dumps(result)
            for value in ('token用量2100', '["token预算3000","普通标签"]'):
                await save('learning_memory', value)

        elif mode == 'priority':
            for ref in ('invented', None, '', 1):
                result = await rejected('remember_memory', {**base, 'keywords': '[malformed', 'execution_ref': ref},
                                        'execution_binding_invalid_or_finished')
                assert 'expected_type' not in result and 'example' not in result

        elif mode == 'unrelated_fields':
            marker = 'SYNTHETIC_PRIVATE_VALUE_748'
            unknown = 'SYNTHETIC_UNKNOWN_KEY_748'
            result = await rejected('remember_memory', {**base, 'keywords': '甲,乙', unknown: marker}, 'invalid_tool_arguments')
            assert {'field': 'unrecognized_field', 'issue': 'extra_forbidden'} in result['field_errors']
            assert marker not in json.dumps(result) and unknown not in json.dumps(result)
            result = await rejected('remember_tool_guidance', {'tool_name': '合成严格类型', 'purpose': '测试其他字段。',
                'keywords': '甲,乙', 'aliases': '甲,乙'}, 'invalid_tool_arguments')
            assert {'field': 'aliases', 'issue': 'list_type'} in result['field_errors']
            assert 'expected_type' not in result
            result = await rejected('remember_memory', {**base, 'keywords': [123]}, 'invalid_tool_arguments')
            assert result['field_errors'] == [{'field': 'keywords', 'issue': 'string_type'}]
            required = await rejected('remember_planning_memory', {'keywords': '["SYNTHETIC_BAD_REQUIRED_749",]'},
                                      'invalid_tool_arguments')
            assert {'field': 'keywords', 'issue': 'list_type'} in required['field_errors']
            assert 'keywords' in required.get('required_fields', [])
            assert required.get('expected_type') == 'array<string>'
            assert required.get('example') == ['标签一', '标签二']
            assert 'null' not in required.get('next_action', '') and '省略' not in required.get('next_action', '')
            assert 'SYNTHETIC_BAD_REQUIRED_749' not in json.dumps(required)
            stored = await save('planning_memory', ['计划'])
            result = await rejected('advance_plan', {'target_ref': stored['ref'], 'expected_event_seq': 0,
                'event_type': 'pause', 'note': 'Synthetic pause.', 'evidence': marker}, 'invalid_tool_arguments')
            assert {'field': 'evidence', 'issue': 'list_type'} in result['field_errors']
            assert marker not in json.dumps(result)
            # Compatibility is not permission to exceed unchanged runtime bounds.
            for value in (','.join('tag' + str(i) for i in range(100)), 'x' * 400):
                await rejected('remember_memory', {**base, 'keywords': value})

        elif mode == 'gateway_raw_claim':
            # Issue a real ledger entry directly to isolate the receiving MCP's
            # raw-claim ordering. Gateway HostExecutionBoundary is intentionally
            # not called: its canonical schema rejects these string arguments.
            tools = server.mcp._tool_manager.list_tools()
            entries = [{'canonical_name': tool.name, 'schema_hash': canonical_hash(tool.parameters)} for tool in tools]
            catalog = {'contract': 'advertised-tools/1', 'catalog_complete': True,
                       'catalog_hash': canonical_hash(entries), 'entries': entries}
            wake = server.onboarding.issue_wake(**common, host_id='synthetic-keyword-host', thread_id='synthetic-keyword-thread',
                source_kind='human_message', source_event_id='synthetic-keyword-gateway')
            prepared = server.onboarding.build_pre_generation_context(**common, wake_id=wake['wake_id'],
                wake_capability=wake['wake_capability'], source_digest='synthetic-source', host_contract_digest='synthetic-host',
                advertised_tools=catalog, source_frame={'query_text': 'Synthetic keywords', 'lineage_stable': False,
                                                       'prior_assistant_present': False, 'capture_items': []})
            assert prepared['decision'] == 'context_prepared'
            server.onboarding.confirm_context_injected(**common, wake_id=wake['wake_id'],
                wake_capability=wake['wake_capability'], context_hash=prepared['context_hash'])
            registry = ExecutionStore(root / 'main.db', deployment_epoch=os.environ['STBRAIN_EXECUTION_EPOCH'],
                                      capability_secret=server.WAKE_SECRET)
            for index, value in enumerate(('甲,乙', '["甲","乙"]', 'Alpha beta')):
                raw = {**base, 'content': 'Synthetic raw signed keyword input ' + str(index), 'keywords': value}
                expected = ['Alpha beta'] if index == 2 else ['甲', '乙']
                call_id = 'synthetic-keyword-call-' + str(index)
                issued = registry.issue_batch(**common, wake_id=wake['wake_id'], wake_capability=wake['wake_capability'],
                    batch_id='synthetic-keyword-batch-' + str(index), revision=1, calls=[{
                        'call_id': call_id, 'advertised_name': 'remember_memory', 'canonical_tool': 'remember_memory',
                        'schema_hash': canonical_hash(server.mcp._tool_manager.get_tool('remember_memory').parameters),
                        'catalog_hash': catalog['catalog_hash'], 'arguments_hash': canonical_hash(raw)}])
                ref = issued['executions'][0]['execution_ref']
                await rejected('remember_memory', {**raw, 'keywords': expected, 'execution_ref': ref},
                               'execution_binding_invalid_or_finished')
                assert rows('SELECT status,arguments_hash FROM brain_execution_calls WHERE call_id=?', (call_id,)) == [('issued', canonical_hash(raw))]
                saved = await call('remember_memory', {**raw, 'execution_ref': ref})
                assert saved.get('decision') == 'stored', 'raw_signed_string_not_accepted'
                assert current_keywords('learning_memory', saved) == expected
                assert rows('SELECT status,arguments_hash FROM brain_execution_calls WHERE call_id=?', (call_id,)) == [('completed', canonical_hash(raw))]
                assert rows('SELECT created_wake_id FROM learning_items WHERE learning_id=?', (saved['id'],))[0][0] == wake['wake_id']
                await rejected('remember_memory', {**raw, 'execution_ref': ref}, 'execution_binding_invalid_or_finished')
            assert len(rows('SELECT wake_id FROM brain_wake_sessions')) == len(initial_wakes) + 1

        elif mode == 'canonical_schema':
            tools = server.mcp._tool_manager.list_tools()
            assert len(tools) == 46  # Legacy44 plus compact discovery/dispatch.
            def schema_types(value):
                if isinstance(value, dict):
                    return ({value['type']} if isinstance(value.get('type'), str) else set()).union(
                        *(schema_types(item) for item in value.values()))
                if isinstance(value, list):
                    return set().union(*(schema_types(item) for item in value))
                return set()
            for name in ('remember_memory', 'remember_emotional_memory', 'remember_learning_memory',
                         'remember_tool_guidance', 'revise_tool_guidance', 'remember_planning_memory'):
                schema = server.mcp._tool_manager.get_tool(name).parameters['properties']['keywords']
                assert 'array' in schema_types(schema), 'canonical_array_metadata_removed'
                serialized = json.dumps(schema, ensure_ascii=False)
                assert '优先填写字符串数组' in schema.get('description', '')
                assert '直连 MCP 接收端' in serialized and '网关调用请使用真实数组' in serialized
            assert 'keywords' not in server.mcp._tool_manager.get_tool('advance_plan').parameters['properties']

        assert rows('SELECT * FROM self_model_revisions ORDER BY revision_id') == original_core
        if mode != 'gateway_raw_claim':
            assert rows('SELECT wake_id FROM brain_wake_sessions ORDER BY wake_id') == initial_wakes
        assert not attempts
        return {'decision': 'PASS', 'mode': mode, 'registered_calls': calls,
                'input_arguments_unchanged': True, 'network_attempts': len(attempts),
                'live_instance_used': False, 'http_authentication_tested': False,
                'gateway_host_execution_boundary_exercised': False,
                'gateway_end_to_end_string_compatibility_tested': False,
                'real_model_called': False, 'core_revision_unchanged': True}


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--probe':
        print(json.dumps(asyncio.run(probe(sys.argv[2])), ensure_ascii=False))
    else:
        unittest.main()
